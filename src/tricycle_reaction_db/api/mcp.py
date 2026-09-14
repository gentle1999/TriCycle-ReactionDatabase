"""NexusX query transport and authenticated import/project control tools."""

import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from enum import Enum
from typing import Any, cast
from uuid import UUID

from fastmcp.server.middleware import Middleware as FastMCPMiddleware
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools import ToolResult
from nexusx import create_use_case_graphql_mcp_server  # type: ignore[import-untyped]
from pydantic import BaseModel, ValidationError
from starlette.middleware import Middleware as ASGIMiddleware
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from tricycle_reaction_db.api.nexusx import config
from tricycle_reaction_db.api.query_guards import (
    project_scoped_use_case_methods,
    validate_graphql_project_scope,
)
from tricycle_reaction_db.application.dtos import ProjectCreate
from tricycle_reaction_db.application.query_cost import (
    QueryBudgetExceeded,
    QueryProjectScopeRequired,
    QueryRateLimitExceeded,
    graphql_error_result,
    normalize_graphql_query_errors,
    validate_graphql_query_budget,
)
from tricycle_reaction_db.application.rate_limits import (
    RateLimitBackendUnavailable,
    create_rate_limiter,
)
from tricycle_reaction_db.application.services.authentication import (
    AuthenticatedPrincipal,
    AuthenticationError,
    AuthenticationService,
    current_principal,
    request_context_active,
    reset_current_principal,
    reset_request_context_active,
    set_current_principal,
    set_request_context_active,
)
from tricycle_reaction_db.application.services.authorization import ProjectAccessDeniedError
from tricycle_reaction_db.application.services.import_jobs import (
    ImportJobConflictError,
    ImportJobNotFoundError,
    ImportJobService,
)
from tricycle_reaction_db.application.services.project_management import (
    ProjectManagementConflictError,
    ProjectManagementNotFoundError,
    ProjectManagementService,
)
from tricycle_reaction_db.application.services.upload_batches import (
    UploadBatchConflictError,
    UploadBatchLimitError,
    UploadBatchNotFoundError,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.core.observability import MCP_ACTIVE_CONNECTIONS, RATE_LIMIT_DECISIONS
from tricycle_reaction_db.domain.enums import ArtifactKind
from tricycle_reaction_db.ingestion.manifest import ArtifactManifest

ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
logger = logging.getLogger(__name__)


class MCPAuthenticationMiddleware:
    """Authenticate MCP HTTP requests and populate the shared principal context."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def _authorization_header(scope: Scope) -> str | None:
        headers = cast(list[tuple[bytes, bytes]], scope.get("headers", []))
        for name, value in headers:
            if name.lower() == b"authorization":
                return value.decode("latin-1")
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if current_principal() is not None and request_context_active():
            await self.app(scope, receive, send)
            return
        try:
            principal = await AuthenticationService.authenticate(self._authorization_header(scope))
        except AuthenticationError as error:
            response = JSONResponse(
                status_code=401,
                content={"detail": str(error)},
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        scope.setdefault("state", {})["principal"] = principal
        request_context_token = set_request_context_active()
        principal_token = set_current_principal(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_principal(principal_token)
            reset_request_context_active(request_context_token)


class MCPBrowserInfoMiddleware:
    """Explain the Streamable HTTP endpoint when opened as a browser URL.

    Streamable HTTP uses ``POST`` for JSON-RPC messages.  A browser navigation
    sends ``GET`` with an HTML-oriented ``Accept`` header, which FastMCP
    correctly rejects in stateless mode.  Returning a small JSON contract here
    makes the frontend entry point useful without changing protocol requests.
    """

    def __init__(self, app: ASGIApp, *, endpoint: str = "/mcp/") -> None:
        self.app = app
        self.endpoint = endpoint

    @staticmethod
    def _accepts_event_stream(scope: Scope) -> bool:
        headers = cast(list[tuple[bytes, bytes]], scope.get("headers", []))
        return any(
            name.lower() == b"accept" and b"text/event-stream" in value.lower()
            for name, value in headers
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") == "GET"
            and not self._accepts_event_stream(scope)
        ):
            response = JSONResponse(
                content={
                    "service": "UseCase MCP",
                    "transport": "Streamable HTTP",
                    "endpoint": self.endpoint,
                    "method": "POST",
                    "message": "This endpoint is for MCP clients, not a browser UI.",
                    "request": {
                        "accept": "application/json, text/event-stream",
                        "content_type": "application/json",
                    },
                },
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class MCPMetricsMiddleware:
    """Track active Streamable HTTP requests without user or session labels."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        MCP_ACTIVE_CONNECTIONS.inc()
        try:
            await self.app(scope, receive, send)
        finally:
            MCP_ACTIVE_CONNECTIONS.dec()


class QueryGuardMiddleware(FastMCPMiddleware):
    """Apply the same GraphQL and request budgets to MCP compose execution."""

    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._scoped_methods = project_scoped_use_case_methods(config)
        self._limiter = create_rate_limiter(
            policy="mcp-read",
            maximum_requests=settings.read_rate_limit_requests,
            window_seconds=settings.query_rate_limit_window_seconds,
            backend=settings.rate_limit_backend,
            redis_url=settings.rate_limit_redis_url,
            key_prefix=settings.rate_limit_key_prefix,
        )

    @staticmethod
    def _result(payload: dict[str, Any]) -> ToolResult:
        return ToolResult(content=payload, structured_content=payload)

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: Any,
    ) -> ToolResult:
        principal = current_principal()
        try:
            key = (
                f"user:{principal.user_id}"
                if principal is not None
                else (
                    f"session:{context.fastmcp_context.session_id}"
                    if context.fastmcp_context is not None
                    else "in-process"
                )
            )
        except RuntimeError:
            key = "in-process"
        try:
            decision = await self._limiter.check(key)
        except RateLimitBackendUnavailable as backend_error:
            RATE_LIMIT_DECISIONS.labels(policy="mcp-read", outcome="backend_error").inc()
            return self._result(
                {
                    "data": None,
                    "errors": [
                        {
                            "message": backend_error.message,
                            "extensions": {"code": backend_error.code},
                        }
                    ],
                }
            )
        if not decision.allowed:
            RATE_LIMIT_DECISIONS.labels(policy="mcp-read", outcome="rejected").inc()
            rate_limit_error = QueryRateLimitExceeded(
                retry_after_seconds=decision.retry_after_seconds
            )
            return self._result(graphql_error_result(rate_limit_error))

        RATE_LIMIT_DECISIONS.labels(policy="mcp-read", outcome="allowed").inc()
        message = context.message
        if getattr(message, "name", None) == "compose_query":
            arguments = getattr(message, "arguments", None) or {}
            query = arguments.get("query")
            if isinstance(query, str):
                try:
                    validate_graphql_query_budget(
                        query,
                        maximum_characters=self._settings.graphql_max_query_characters,
                        maximum_tokens=self._settings.graphql_max_tokens,
                        maximum_depth=self._settings.graphql_max_depth,
                        maximum_complexity=self._settings.graphql_max_complexity,
                    )
                except QueryBudgetExceeded as error:
                    return self._result(graphql_error_result(error))
                try:
                    validate_graphql_project_scope(query, self._scoped_methods)
                except QueryProjectScopeRequired as error:
                    return self._result(graphql_error_result(error))

        result: ToolResult = await call_next(context)
        if (
            getattr(message, "name", None) == "compose_query"
            and isinstance(result, ToolResult)
            and isinstance(result.structured_content, dict)
        ):
            normalized = normalize_graphql_query_errors(result.structured_content)
            return self._result(normalized)
        return result


mcp_server = create_use_case_graphql_mcp_server(
    apps=[config],
    name=get_settings().mcp_server_name,
)


def _mcp_success(data: Any) -> dict[str, Any]:
    def to_json(value: Any) -> Any:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, dict):
            return {str(key): to_json(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [to_json(item) for item in value]
        if isinstance(value, (UUID, datetime, date, Enum)):
            return value.value if isinstance(value, Enum) else str(value)
        return value

    return {"success": True, "data": to_json(data)}


def _mcp_error(code: str, message: str) -> dict[str, Any]:
    return {"success": False, "error": {"code": code, "message": message}}


def _mcp_principal() -> AuthenticatedPrincipal | None:
    principal = current_principal()
    if principal is None:
        return None
    return principal


def _mcp_exception(error: Exception) -> dict[str, Any]:
    """Map control-plane failures to a stable MCP response envelope."""

    if isinstance(error, AuthenticationError):
        return _mcp_error("authentication_required", str(error))
    if isinstance(error, ValidationError):
        return _mcp_error("invalid_argument", str(error))
    if isinstance(error, ValueError):
        return _mcp_error("invalid_argument", str(error))
    if isinstance(
        error, (ProjectManagementNotFoundError, ImportJobNotFoundError, UploadBatchNotFoundError)
    ):
        return _mcp_error("not_found", str(error))
    if isinstance(error, ProjectAccessDeniedError):
        return _mcp_error("forbidden", str(error))
    if isinstance(
        error,
        (
            ProjectManagementConflictError,
            ImportJobConflictError,
            UploadBatchConflictError,
            UploadBatchLimitError,
        ),
    ):
        return _mcp_error("conflict", str(error))
    logger.exception("MCP control operation failed", exc_info=error)
    return _mcp_error("internal_error", "MCP control operation failed")


def _require_mcp_principal() -> AuthenticatedPrincipal:
    principal = _mcp_principal()
    if principal is None:
        raise AuthenticationError("authenticated MCP principal is required")
    return principal


@mcp_server.tool(name="create_project")  # type: ignore[untyped-decorator]
async def create_project(
    organization_id: str,
    slug: str,
    name: str,
    data_source: dict[str, Any] | None = None,
    model_checkpoint: dict[str, Any] | None = None,
    calculation_protocol: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a canonical project owned by the authenticated organization user."""

    try:
        principal = _require_mcp_principal()
        payload = ProjectCreate(
            organization_id=UUID(organization_id),
            slug=slug,
            name=name,
            data_source=data_source or {},
            model_checkpoint=model_checkpoint or {},
            calculation_protocol=calculation_protocol or {},
        )
        return _mcp_success(await ProjectManagementService.create_project(payload, principal))
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="register_import_manifest")  # type: ignore[untyped-decorator]
async def register_import_manifest(
    project_id: str,
    manifest: dict[str, Any],
    artifact_kind: ArtifactKind = ArtifactKind.CALCULATION_OUTPUT,
) -> dict[str, Any]:
    """Register an operator-staged manifest; file bytes never cross MCP."""

    try:
        principal = _require_mcp_principal()
        parsed_manifest = ArtifactManifest.model_validate(manifest)
        return _mcp_success(
            await ImportJobService.register_manifest(
                parsed_manifest,
                project_id=UUID(project_id),
                user_id=principal.user_id,
                artifact_kind=artifact_kind,
            )
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="start_import_job")  # type: ignore[untyped-decorator]
async def start_import_job(import_job_id: str) -> dict[str, Any]:
    """Start or continue a registered import job from its configured staging root."""

    try:
        principal = _require_mcp_principal()
        return _mcp_success(
            await ImportJobService.start(UUID(import_job_id), user_id=principal.user_id)
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="get_import_status")  # type: ignore[untyped-decorator]
async def get_import_status(import_job_id: str) -> dict[str, Any]:
    """Return durable job and item state for the authenticated owner."""

    try:
        principal = _require_mcp_principal()
        return _mcp_success(
            await ImportJobService.status(UUID(import_job_id), user_id=principal.user_id)
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="list_import_failures")  # type: ignore[untyped-decorator]
async def list_import_failures(import_job_id: str) -> dict[str, Any]:
    """List only failed items, including durable error codes and details."""

    try:
        principal = _require_mcp_principal()
        return _mcp_success(
            await ImportJobService.failures(UUID(import_job_id), user_id=principal.user_id)
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="retry_import_items")  # type: ignore[untyped-decorator]
async def retry_import_items(
    import_job_id: str,
    item_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Retry selected failed items, or all failed items when item_ids is omitted."""

    try:
        principal = _require_mcp_principal()
        parsed_item_ids = None if item_ids is None else [UUID(item_id) for item_id in item_ids]
        return _mcp_success(
            await ImportJobService.retry_items(
                UUID(import_job_id),
                item_ids=parsed_item_ids,
                user_id=principal.user_id,
            )
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="pause_import")  # type: ignore[untyped-decorator]
async def pause_import(import_job_id: str) -> dict[str, Any]:
    """Pause new file staging for an import job."""

    try:
        principal = _require_mcp_principal()
        return _mcp_success(
            await ImportJobService.pause(UUID(import_job_id), user_id=principal.user_id)
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="resume_import")  # type: ignore[untyped-decorator]
async def resume_import(import_job_id: str) -> dict[str, Any]:
    """Resume a paused import job and continue staging queued files."""

    try:
        principal = _require_mcp_principal()
        return _mcp_success(
            await ImportJobService.resume(UUID(import_job_id), user_id=principal.user_id)
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="cancel_import")  # type: ignore[untyped-decorator]
async def cancel_import(import_job_id: str) -> dict[str, Any]:
    """Cancel an import job while preserving its audit trail."""

    try:
        principal = _require_mcp_principal()
        return _mcp_success(
            await ImportJobService.cancel(UUID(import_job_id), user_id=principal.user_id)
        )
    except Exception as error:
        return _mcp_exception(error)


mcp_server.add_middleware(QueryGuardMiddleware())
mcp_http_app = mcp_server.http_app(
    path="/",
    middleware=[
        ASGIMiddleware(MCPAuthenticationMiddleware),
        ASGIMiddleware(MCPMetricsMiddleware),
        ASGIMiddleware(MCPBrowserInfoMiddleware),
    ],
    transport="streamable-http",
    stateless_http=True,
)
mcp_dedicated_app = mcp_server.http_app(
    path="/mcp",
    middleware=[
        ASGIMiddleware(MCPAuthenticationMiddleware),
        ASGIMiddleware(MCPMetricsMiddleware),
        ASGIMiddleware(MCPBrowserInfoMiddleware),
    ],
    transport="streamable-http",
    stateless_http=True,
)

__all__ = [
    "MCPAuthenticationMiddleware",
    "MCPBrowserInfoMiddleware",
    "MCPMetricsMiddleware",
    "QueryGuardMiddleware",
    "mcp_dedicated_app",
    "mcp_http_app",
    "mcp_server",
]
