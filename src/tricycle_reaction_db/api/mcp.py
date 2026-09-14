"""NexusX query transport and authenticated organization/project control tools."""

import base64
import binascii
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
from tricycle_reaction_db.application.dtos import (
    OrganizationCreate,
    OrganizationMemberUpsert,
    ProjectCreate,
    ProjectInvitationCreate,
    ProjectMemberUpsert,
    ProjectUpdate,
)
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
from tricycle_reaction_db.application.services.artifact_uploads import (
    ArtifactUploadConflictError,
    ArtifactUploadError,
    ArtifactUploadLimitError,
    ArtifactUploadPayload,
)
from tricycle_reaction_db.application.services.audit import AuditService
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
from tricycle_reaction_db.application.services.authorization import (
    AuthorizationService,
    ProjectAccessDeniedError,
)
from tricycle_reaction_db.application.services.import_jobs import (
    ImportJobConflictError,
    ImportJobNotFoundError,
    ImportJobService,
)
from tricycle_reaction_db.application.services.invitations import (
    InvitationConflictError,
    InvitationError,
    InvitationNotFoundError,
    InvitationService,
)
from tricycle_reaction_db.application.services.organization_management import (
    OrganizationManagementAccessDeniedError,
    OrganizationManagementConflictError,
    OrganizationManagementNotFoundError,
    OrganizationManagementService,
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
    UploadBatchService,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.core.observability import MCP_ACTIVE_CONNECTIONS, RATE_LIMIT_DECISIONS
from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    OrganizationRole,
    ProjectRole,
    ProjectStatus,
)
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
        error,
        (
            ProjectManagementNotFoundError,
            ImportJobNotFoundError,
            UploadBatchNotFoundError,
            InvitationNotFoundError,
            OrganizationManagementNotFoundError,
        ),
    ):
        return _mcp_error("not_found", str(error))
    if isinstance(
        error,
        (ProjectAccessDeniedError, OrganizationManagementAccessDeniedError, PermissionError),
    ):
        return _mcp_error("forbidden", str(error))
    if isinstance(error, ArtifactUploadLimitError):
        return _mcp_error("payload_too_large", str(error))
    if isinstance(
        error,
        (
            OrganizationManagementConflictError,
            ProjectManagementConflictError,
            InvitationConflictError,
            ImportJobConflictError,
            UploadBatchConflictError,
            UploadBatchLimitError,
            ArtifactUploadConflictError,
        ),
    ):
        return _mcp_error("conflict", str(error))
    if isinstance(error, (ArtifactUploadError, InvitationError)):
        return _mcp_error("invalid_argument", str(error))
    logger.exception("MCP control operation failed", exc_info=error)
    return _mcp_error("internal_error", "MCP control operation failed")


def _require_mcp_principal() -> AuthenticatedPrincipal:
    principal = _mcp_principal()
    if principal is None:
        raise AuthenticationError("authenticated MCP principal is required")
    return principal


def _decode_mcp_payload(content_base64: str) -> bytes:
    """Decode one MCP file payload while enforcing the configured byte budget."""

    encoded = content_base64.strip()
    if not encoded:
        raise ValueError("content_base64 must not be empty")
    maximum_encoded_length = 4 * ((get_settings().max_upload_bytes + 2) // 3)
    if len(encoded) > maximum_encoded_length:
        raise ArtifactUploadLimitError(
            f"encoded calculation log exceeds the {get_settings().max_upload_bytes}-byte limit"
        )
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, UnicodeError, ValueError) as error:
        raise ValueError("content_base64 must be valid standard base64") from error
    if not payload:
        raise ValueError("uploaded calculation log is empty")
    return payload


@mcp_server.tool(name="list_organizations")  # type: ignore[untyped-decorator]
async def list_organizations() -> dict[str, Any]:
    """List active organizations visible to the authenticated user."""

    try:
        principal = _require_mcp_principal()
        return _mcp_success(await AuthorizationService.organization_accesses(principal.user_id))
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="create_organization")  # type: ignore[untyped-decorator]
async def create_organization(slug: str, name: str) -> dict[str, Any]:
    """Create an organization and make the authenticated user its owner."""

    try:
        principal = _require_mcp_principal()
        payload = OrganizationCreate(slug=slug, name=name)
        return _mcp_success(
            await OrganizationManagementService.create_organization(payload, principal)
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="list_organization_members")  # type: ignore[untyped-decorator]
async def list_organization_members(organization_id: str) -> dict[str, Any]:
    """List members of an organization visible to the authenticated user."""

    try:
        principal = _require_mcp_principal()
        return _mcp_success(
            await OrganizationManagementService.list_members(UUID(organization_id), principal)
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="upsert_organization_member")  # type: ignore[untyped-decorator]
async def upsert_organization_member(
    organization_id: str,
    user_id: str,
    role: OrganizationRole = OrganizationRole.MEMBER,
) -> dict[str, Any]:
    """Add an organization member or change its role; owner/admin access is required."""

    try:
        principal = _require_mcp_principal()
        payload = OrganizationMemberUpsert(user_id=UUID(user_id), role=role)
        return _mcp_success(
            await OrganizationManagementService.upsert_member(
                UUID(organization_id),
                payload,
                principal,
            )
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="remove_organization_member")  # type: ignore[untyped-decorator]
async def remove_organization_member(organization_id: str, user_id: str) -> dict[str, Any]:
    """Remove an organization member while preserving the last-owner safeguard."""

    try:
        principal = _require_mcp_principal()
        await OrganizationManagementService.remove_member(
            UUID(organization_id),
            UUID(user_id),
            principal,
        )
        return _mcp_success(
            {"removed": True, "organization_id": organization_id, "user_id": user_id}
        )
    except Exception as error:
        return _mcp_exception(error)


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


@mcp_server.tool(name="list_projects")  # type: ignore[untyped-decorator]
async def list_projects(
    organization_id: str | None = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    """List projects visible to the authenticated user, optionally by organization."""

    try:
        principal = _require_mcp_principal()
        requested_organization_id = UUID(organization_id) if organization_id is not None else None
        projects = await ProjectManagementService.list_projects(
            principal,
            include_archived=include_archived,
        )
        if requested_organization_id is not None:
            projects = [
                project
                for project in projects
                if project.organization_id == requested_organization_id
            ]
        return _mcp_success(projects)
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="get_project")  # type: ignore[untyped-decorator]
async def get_project(project_id: str) -> dict[str, Any]:
    """Get one project visible to the authenticated user."""

    try:
        principal = _require_mcp_principal()
        return _mcp_success(await ProjectManagementService.get_project(UUID(project_id), principal))
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="update_project")  # type: ignore[untyped-decorator]
async def update_project(
    project_id: str,
    slug: str | None = None,
    name: str | None = None,
    status: ProjectStatus | None = None,
) -> dict[str, Any]:
    """Update a project; the caller must be its manager or organization admin."""

    try:
        principal = _require_mcp_principal()
        payload = ProjectUpdate(slug=slug, name=name, status=status)
        return _mcp_success(
            await ProjectManagementService.update_project(UUID(project_id), payload, principal)
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="list_project_members")  # type: ignore[untyped-decorator]
async def list_project_members(project_id: str) -> dict[str, Any]:
    """List members of a project for a project manager or organization admin."""

    try:
        principal = _require_mcp_principal()
        return _mcp_success(
            await ProjectManagementService.list_members(UUID(project_id), principal)
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="upsert_project_member")  # type: ignore[untyped-decorator]
async def upsert_project_member(
    project_id: str,
    user_id: str,
    role: ProjectRole = ProjectRole.VIEWER,
) -> dict[str, Any]:
    """Add a project member or change its role."""

    try:
        principal = _require_mcp_principal()
        payload = ProjectMemberUpsert(user_id=UUID(user_id), role=role)
        return _mcp_success(
            await ProjectManagementService.upsert_member(UUID(project_id), payload, principal)
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="remove_project_member")  # type: ignore[untyped-decorator]
async def remove_project_member(project_id: str, user_id: str) -> dict[str, Any]:
    """Remove a project member while preserving the last-manager safeguard."""

    try:
        principal = _require_mcp_principal()
        await ProjectManagementService.remove_member(
            UUID(project_id),
            UUID(user_id),
            principal,
        )
        return _mcp_success({"removed": True, "project_id": project_id, "user_id": user_id})
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="list_project_invitations")  # type: ignore[untyped-decorator]
async def list_project_invitations(project_id: str) -> dict[str, Any]:
    """List invitations for a project manager or organization admin."""

    try:
        principal = _require_mcp_principal()
        return _mcp_success(await InvitationService.list(UUID(project_id), principal))
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="create_project_invitation")  # type: ignore[untyped-decorator]
async def create_project_invitation(
    project_id: str,
    email: str,
    role: ProjectRole = ProjectRole.VIEWER,
    expires_in_days: int = 7,
) -> dict[str, Any]:
    """Invite a user to a project and return its one-time acceptance token and URL."""

    try:
        principal = _require_mcp_principal()
        payload = ProjectInvitationCreate(
            email=email,
            role=role,
            expires_in_days=expires_in_days,
        )
        return _mcp_success(
            await InvitationService.create(
                UUID(project_id),
                payload,
                principal,
                frontend_url=get_settings().oidc_frontend_url,
            )
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="revoke_project_invitation")  # type: ignore[untyped-decorator]
async def revoke_project_invitation(project_id: str, invitation_id: str) -> dict[str, Any]:
    """Revoke an unaccepted project invitation."""

    try:
        principal = _require_mcp_principal()
        await InvitationService.revoke(UUID(project_id), UUID(invitation_id), principal)
        return _mcp_success({"revoked": True, "invitation_id": invitation_id})
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="resend_project_invitation")  # type: ignore[untyped-decorator]
async def resend_project_invitation(project_id: str, invitation_id: str) -> dict[str, Any]:
    """Regenerate and resend an unaccepted project invitation."""

    try:
        principal = _require_mcp_principal()
        return _mcp_success(
            await InvitationService.resend(
                UUID(project_id),
                UUID(invitation_id),
                principal,
                frontend_url=get_settings().oidc_frontend_url,
            )
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="accept_project_invitation")  # type: ignore[untyped-decorator]
async def accept_project_invitation(invitation_token: str) -> dict[str, Any]:
    """Accept an invitation when its email matches the authenticated user."""

    try:
        principal = _require_mcp_principal()
        return _mcp_success(await InvitationService.accept(invitation_token, principal))
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="list_project_audit")  # type: ignore[untyped-decorator]
async def list_project_audit(
    project_id: str,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List the audit trail for a project manager or organization admin."""

    try:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        principal = _require_mcp_principal()
        return _mcp_success(
            await AuditService.list_events(
                principal,
                project_id=UUID(project_id),
                limit=limit,
                offset=offset,
            )
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="upload_calculation_log")  # type: ignore[untyped-decorator]
async def upload_calculation_log(
    project_id: str,
    filename: str,
    content_base64: str,
    media_type: str = "application/octet-stream",
    relative_path: str | None = None,
    expected_sha256: str | None = None,
    expected_size_bytes: int | None = None,
) -> dict[str, Any]:
    """Stage one Gaussian/ORCA calculation log for durable worker parsing."""

    try:
        principal = _require_mcp_principal()
        payload = _decode_mcp_payload(content_base64)
        submission = await UploadBatchService.create_and_stage(
            files=[
                ArtifactUploadPayload(
                    filename=filename,
                    media_type=media_type,
                    payload=payload,
                    relative_path=relative_path,
                    expected_sha256=expected_sha256,
                    expected_size_bytes=expected_size_bytes,
                )
            ],
            artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
            project_id=UUID(project_id),
            user_id=principal.user_id,
        )
        return _mcp_success(
            {
                "batch": submission.batch,
                "item": submission.items[0],
            }
        )
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
