"""NexusX query transport and authenticated organization/project control tools."""

import asyncio
import base64
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
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

from tricycle_reaction_db.api.mcp_apps import calculation_log_workspace_app
from tricycle_reaction_db.api.mcp_payloads import decode_base64_payload
from tricycle_reaction_db.api.nexusx import config
from tricycle_reaction_db.api.query_guards import (
    project_scoped_use_case_methods,
    validate_graphql_project_scope,
)
from tricycle_reaction_db.application.dtos import (
    ArtifactMetadataUpdate,
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
from tricycle_reaction_db.application.services.artifact_content import (
    ArtifactContentService,
    ArtifactDownload,
    ArtifactNotFoundError,
    ArtifactObjectIntegrityError,
    ArtifactPreviewUnsupportedError,
    ArtifactUnavailableError,
    iter_artifact_download,
)
from tricycle_reaction_db.application.services.artifact_management import (
    ArtifactManagementService,
    ArtifactRemovalIntegrityError,
    ArtifactRemovalNotFoundError,
    ArtifactRemovalUnavailableError,
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
    ProjectPermission,
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
from tricycle_reaction_db.application.services.mapped_reaction_geometry_export import (
    iter_mapped_reaction_geometry_export,
)
from tricycle_reaction_db.application.services.organization_management import (
    OrganizationManagementAccessDeniedError,
    OrganizationManagementConflictError,
    OrganizationManagementNotFoundError,
    OrganizationManagementService,
)
from tricycle_reaction_db.application.services.project_data_removal import (
    ProjectDataRemovalConflictError,
    ProjectDataRemovalNotFoundError,
    ProjectDataRemovalService,
)
from tricycle_reaction_db.application.services.project_management import (
    ProjectManagementConflictError,
    ProjectManagementNotFoundError,
    ProjectManagementService,
)
from tricycle_reaction_db.application.services.reaction_thermodynamic_analytics import (
    ReactionThermodynamicAnalyticsService,
)
from tricycle_reaction_db.application.services.scientific_array_content import (
    ScientificArrayContentService,
    ScientificArrayNotFoundError,
    ScientificArrayPayloadTooLargeError,
)
from tricycle_reaction_db.application.services.units_ts_dataset_export import (
    UnitsDatasetExportNotFoundError,
    UnitsDatasetExportPendingError,
    UnitsTsDatasetExportService,
    iter_units_ts_dataset_jsonl,
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
MCP_TS_GEOMETRY_EXPORT_MAX_RECORDS = 100
MCP_TS_GEOMETRY_EXPORT_MAX_BYTES = 512 * 1024
MCP_EXPORT_MAX_RECORDS = 100
MCP_EXPORT_MAX_BYTES = 512 * 1024
MCP_ARTIFACT_PREVIEW_MAX_BYTES = 256 * 1024
MCP_BINARY_MAX_BYTES = 512 * 1024
MCP_BATCH_MAX_FILES = 100
MCP_ARRAY_PREVIEW_MAX_ELEMENTS = 4096


class MCPContentTooLargeError(RuntimeError):
    """A binary or streamed payload exceeds the MCP response budget."""


def _read_artifact_payload(download: ArtifactDownload, max_bytes: int) -> bytes:
    if download.size_bytes > max_bytes:
        raise MCPContentTooLargeError(
            f"artifact is {download.size_bytes} bytes; MCP limit is {max_bytes}"
        )
    chunks: list[bytes] = []
    digest = hashlib.sha256()
    total_bytes = 0
    for chunk in iter_artifact_download(download):
        total_bytes += len(chunk)
        if total_bytes > max_bytes:
            raise MCPContentTooLargeError(f"artifact exceeds the {max_bytes}-byte MCP limit")
        digest.update(chunk)
        chunks.append(chunk)
    if total_bytes != download.size_bytes or digest.hexdigest() != download.content_sha256:
        raise ArtifactObjectIntegrityError(f"downloaded artifact {download.id} failed verification")
    return b"".join(chunks)


async def _collect_jsonl_page(
    records_iterator: AsyncIterator[bytes],
    *,
    limit: int,
    cursor_field: str,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    response_bytes = 0
    has_more = False
    async for line in records_iterator:
        if len(records) >= limit:
            has_more = True
            break
        if response_bytes + len(line) > MCP_EXPORT_MAX_BYTES:
            if not records:
                raise MCPContentTooLargeError(
                    f"a single JSONL record exceeds the {MCP_EXPORT_MAX_BYTES}-byte MCP limit"
                )
            has_more = True
            break
        records.append(json.loads(line))
        response_bytes += len(line)
    return {
        "records": records,
        "next_after_binding_id": records[-1].get(cursor_field) if records else None,
        "has_more": has_more,
    }


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
    if isinstance(
        error,
        (MCPContentTooLargeError, ArtifactUploadLimitError, ScientificArrayPayloadTooLargeError),
    ):
        return _mcp_error("payload_too_large", str(error))
    if isinstance(error, ArtifactPreviewUnsupportedError):
        return _mcp_error("unsupported_media_type", str(error))
    if isinstance(error, ArtifactObjectIntegrityError):
        return _mcp_error("storage_integrity_error", str(error))
    if isinstance(error, ArtifactUnavailableError):
        return _mcp_error("storage_unavailable", str(error))
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
            ProjectDataRemovalNotFoundError,
            ArtifactRemovalNotFoundError,
            ArtifactNotFoundError,
            ScientificArrayNotFoundError,
            UnitsDatasetExportNotFoundError,
        ),
    ):
        return _mcp_error("not_found", str(error))
    if isinstance(
        error,
        (ProjectAccessDeniedError, OrganizationManagementAccessDeniedError, PermissionError),
    ):
        return _mcp_error("forbidden", str(error))
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
            ProjectDataRemovalConflictError,
            ArtifactRemovalIntegrityError,
            UnitsDatasetExportPendingError,
        ),
    ):
        return _mcp_error("conflict", str(error))
    if isinstance(error, ArtifactRemovalUnavailableError):
        return _mcp_error("storage_unavailable", str(error))
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
    return decode_base64_payload(
        content_base64,
        maximum_bytes=get_settings().max_upload_bytes,
        payload_description="calculation log",
    )


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


@mcp_server.tool(name="preview_project_cleanup")  # type: ignore[untyped-decorator]
async def preview_project_cleanup(project_id: str) -> dict[str, Any]:
    """Preview the project-owned records and RustFS objects a cleanup would remove."""

    try:
        principal = _require_mcp_principal()
        return _mcp_success(
            await ProjectDataRemovalService.preview(
                UUID(project_id),
                user_id=principal.user_id,
            )
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="delete_project_data")  # type: ignore[untyped-decorator]
async def delete_project_data(project_id: str, confirmation: str) -> dict[str, Any]:
    """Permanently delete all project-owned scientific data after slug confirmation.

    The project, its memberships, and its audit trail remain.  The operation
    requires project-management permission and ``confirmation`` must exactly
    equal the project slug.
    """

    try:
        principal = _require_mcp_principal()
        return _mcp_success(
            await ProjectDataRemovalService.clear(
                UUID(project_id),
                user_id=principal.user_id,
                confirmation=confirmation,
            )
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="delete_artifact")  # type: ignore[untyped-decorator]
async def delete_artifact(artifact_id: str) -> dict[str, Any]:
    """Retire one artifact and remove its RustFS object when unshared."""

    try:
        principal = _require_mcp_principal()
        await ArtifactManagementService.retire(UUID(artifact_id), user_id=principal.user_id)
        return _mcp_success({"removed": True, "artifact_id": artifact_id})
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="update_artifact_notes")  # type: ignore[untyped-decorator]
async def update_artifact_notes(artifact_id: str, notes: str | None) -> dict[str, Any]:
    """Set or clear the user-maintained notes for one artifact.

    The nullable argument allows callers to explicitly clear an existing note.
    The authenticated user must have artifact-management permission in the
    artifact's project.
    """

    try:
        principal = _require_mcp_principal()
        result = await ArtifactManagementService.update_metadata(
            UUID(artifact_id),
            ArtifactMetadataUpdate(notes=notes),
            user_id=principal.user_id,
        )
        return _mcp_success(result)
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


@mcp_server.tool(name="create_units_ts_dataset")  # type: ignore[untyped-decorator]
async def create_units_ts_dataset(project_id: str) -> dict[str, Any]:
    """Queue a UniTS NPY export from mapped transition-state geometries in a project."""

    try:
        principal = _require_mcp_principal()
        result = await UnitsTsDatasetExportService.create(UUID(project_id), principal.user_id)
        result["status_url"] = result.pop("status_url_path")
        result["download_url"] = result.pop("download_url_path")
        return _mcp_success(result)
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="get_units_ts_dataset_status")  # type: ignore[untyped-decorator]
async def get_units_ts_dataset_status(job_id: str) -> dict[str, Any]:
    """Check a UniTS NPY export job before downloading its dataset link."""

    try:
        principal = _require_mcp_principal()
        return _mcp_success(
            await UnitsTsDatasetExportService.status(UUID(job_id), principal.user_id)
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="export_units_ts_dataset_jsonl")  # type: ignore[untyped-decorator]
async def export_units_ts_dataset_jsonl(
    project_id: str,
    after_binding_id: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Read a bounded page of UniTS TS feature records as JSONL-decoded objects."""

    try:
        principal = _require_mcp_principal()
        requested_project_id = UUID(project_id)
        cursor = UUID(after_binding_id) if after_binding_id is not None else None
        if not 1 <= limit <= MCP_EXPORT_MAX_RECORDS:
            raise ValueError(f"limit must be between 1 and {MCP_EXPORT_MAX_RECORDS}")
        await AuthorizationService.require_project_permission(
            principal.user_id,
            requested_project_id,
            ProjectPermission.ARTIFACT_DOWNLOAD,
        )
        page = await _collect_jsonl_page(
            iter_units_ts_dataset_jsonl(
                requested_project_id,
                after_binding_id=cursor,
                max_records=limit + 1,
            ),
            limit=limit,
            cursor_field="geometry_binding_id",
        )
        return _mcp_success({"project_id": requested_project_id, **page})
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="export_mapped_reaction_thermodynamics_csv")  # type: ignore[untyped-decorator]
async def export_mapped_reaction_thermodynamics_csv(
    project_id: str,
    filter_expression: str | None = None,
    has_activation_gibbs_free_energy: bool | None = None,
    has_reaction_gibbs_free_energy: bool | None = None,
    limit: int = 20,
    offset: int = 0,
    max_bytes: int = MCP_EXPORT_MAX_BYTES,
) -> dict[str, Any]:
    """Return one bounded page of the cleaned mapped-reaction thermodynamics CSV."""

    try:
        principal = _require_mcp_principal()
        requested_project_id = UUID(project_id)
        if not 1 <= limit <= MCP_EXPORT_MAX_RECORDS:
            raise ValueError(f"limit must be between 1 and {MCP_EXPORT_MAX_RECORDS}")
        if not 0 <= offset <= 1_000_000:
            raise ValueError("offset must be between 0 and 1000000")
        if not 1024 <= max_bytes <= MCP_EXPORT_MAX_BYTES:
            raise ValueError(f"max_bytes must be between 1024 and {MCP_EXPORT_MAX_BYTES}")
        await AuthorizationService.require_project_permission(
            principal.user_id,
            requested_project_id,
            ProjectPermission.ARTIFACT_READ,
        )
        stream = await ReactionThermodynamicAnalyticsService.export_csv(
            requested_project_id,
            filter_expression=filter_expression,
            has_activation_gibbs_free_energy=has_activation_gibbs_free_energy,
            has_reaction_gibbs_free_energy=has_reaction_gibbs_free_energy,
            limit=limit + 1,
            offset=offset,
        )
        header: str | None = None
        rows: list[str] = []
        response_bytes = 0
        has_more = False
        async for line in stream:
            if header is None:
                header = line
                if offset == 0:
                    response_bytes += len(line.encode("utf-8"))
                continue
            if len(rows) >= limit:
                has_more = True
                break
            line_bytes = len(line.encode("utf-8"))
            if response_bytes + line_bytes > max_bytes:
                if not rows:
                    raise MCPContentTooLargeError(
                        f"a CSV row exceeds the {max_bytes}-byte MCP page limit"
                    )
                has_more = True
                break
            rows.append(line)
            response_bytes += line_bytes

        csv_text = (header or "") + ("".join(rows))
        next_offset = offset + len(rows)
        return _mcp_success(
            {
                "project_id": requested_project_id,
                "csv": csv_text,
                "media_type": "text/csv; charset=utf-8",
                "header_included": offset == 0,
                "row_count": len(rows),
                "next_offset": next_offset,
                "has_more": has_more,
            }
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="preview_artifact_content")  # type: ignore[untyped-decorator]
async def preview_artifact_content(
    artifact_id: str,
    project_id: str,
    max_bytes: int = 64 * 1024,
) -> dict[str, Any]:
    """Return a bounded text preview for an authorized project artifact."""

    try:
        principal = _require_mcp_principal()
        if not 1024 <= max_bytes <= MCP_ARTIFACT_PREVIEW_MAX_BYTES:
            raise ValueError(f"max_bytes must be between 1024 and {MCP_ARTIFACT_PREVIEW_MAX_BYTES}")
        preview = await ArtifactContentService.preview(
            UUID(artifact_id),
            max_bytes=max_bytes,
            user_id=principal.user_id,
            project_id=UUID(project_id),
        )
        return _mcp_success(preview)
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="download_artifact_content")  # type: ignore[untyped-decorator]
async def download_artifact_content(
    artifact_id: str,
    project_id: str,
    max_bytes: int = 128 * 1024,
) -> dict[str, Any]:
    """Download one bounded artifact and return its verified bytes as Base64."""

    try:
        principal = _require_mcp_principal()
        if not 1 <= max_bytes <= MCP_BINARY_MAX_BYTES:
            raise ValueError(f"max_bytes must be between 1 and {MCP_BINARY_MAX_BYTES}")
        download = await ArtifactContentService.download(
            UUID(artifact_id),
            user_id=principal.user_id,
            project_id=UUID(project_id),
        )
        payload = await asyncio.to_thread(_read_artifact_payload, download, max_bytes)
        return _mcp_success(
            {
                "artifact_id": download.id,
                "filename": download.original_filename,
                "media_type": download.media_type,
                "size_bytes": download.size_bytes,
                "content_sha256": download.content_sha256,
                "content_base64": base64.b64encode(payload).decode("ascii"),
            }
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="download_artifacts_batch")  # type: ignore[untyped-decorator]
async def download_artifacts_batch(
    artifact_ids: list[str],
    project_id: str,
    max_total_bytes: int = 256 * 1024,
) -> dict[str, Any]:
    """Download a bounded batch of artifact contents as individually encoded Base64 files."""

    try:
        principal = _require_mcp_principal()
        if not 1 <= len(artifact_ids) <= MCP_BATCH_MAX_FILES:
            raise ValueError(f"artifact_ids must contain 1 to {MCP_BATCH_MAX_FILES} items")
        requested_ids = [UUID(artifact_id) for artifact_id in artifact_ids]
        if len(set(requested_ids)) != len(requested_ids):
            raise ValueError("artifact_ids must be unique")
        if not 1 <= max_total_bytes <= MCP_BINARY_MAX_BYTES:
            raise ValueError(f"max_total_bytes must be between 1 and {MCP_BINARY_MAX_BYTES}")
        requested_project_id = UUID(project_id)
        slots = asyncio.Semaphore(16)

        async def resolve(artifact_id: UUID) -> ArtifactDownload:
            async with slots:
                return await ArtifactContentService.download(
                    artifact_id,
                    user_id=principal.user_id,
                    project_id=requested_project_id,
                )

        downloads = list(
            await asyncio.gather(*(resolve(artifact_id) for artifact_id in requested_ids))
        )
        total_bytes = sum(download.size_bytes for download in downloads)
        if total_bytes > max_total_bytes:
            raise MCPContentTooLargeError(
                f"artifact batch is {total_bytes} bytes; MCP limit is {max_total_bytes}"
            )

        files: list[dict[str, Any]] = []
        for download in downloads:
            payload = await asyncio.to_thread(
                _read_artifact_payload,
                download,
                max_total_bytes,
            )
            files.append(
                {
                    "artifact_id": download.id,
                    "filename": download.original_filename,
                    "media_type": download.media_type,
                    "size_bytes": download.size_bytes,
                    "content_sha256": download.content_sha256,
                    "content_base64": base64.b64encode(payload).decode("ascii"),
                }
            )
        return _mcp_success(
            {
                "project_id": requested_project_id,
                "total_bytes": total_bytes,
                "files": files,
            }
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="preview_scientific_array")  # type: ignore[untyped-decorator]
async def preview_scientific_array(
    array_id: str,
    project_id: str,
    max_elements: int = 512,
) -> dict[str, Any]:
    """Return a bounded preview of a project-scoped scientific array."""

    try:
        principal = _require_mcp_principal()
        requested_project_id = UUID(project_id)
        if not 1 <= max_elements <= MCP_ARRAY_PREVIEW_MAX_ELEMENTS:
            raise ValueError(f"max_elements must be between 1 and {MCP_ARRAY_PREVIEW_MAX_ELEMENTS}")
        await AuthorizationService.require_project_permission(
            principal.user_id,
            requested_project_id,
            ProjectPermission.ARTIFACT_DOWNLOAD,
        )
        preview = await ScientificArrayContentService.preview(
            UUID(array_id),
            max_elements=max_elements,
            project_id=requested_project_id,
        )
        return _mcp_success(
            {
                "array_id": preview.array_id,
                "kind": preview.kind,
                "unit": preview.unit,
                "dtype": preview.dtype,
                "shape": list(preview.shape),
                "total_elements": preview.total_elements,
                "values": preview.values,
                "truncated": preview.truncated,
            }
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="download_scientific_array_npy")  # type: ignore[untyped-decorator]
async def download_scientific_array_npy(
    array_id: str,
    project_id: str,
    max_bytes: int = 128 * 1024,
) -> dict[str, Any]:
    """Download one bounded scientific array as a Base64-encoded NPY payload."""

    try:
        principal = _require_mcp_principal()
        requested_project_id = UUID(project_id)
        if not 1 <= max_bytes <= MCP_BINARY_MAX_BYTES:
            raise ValueError(f"max_bytes must be between 1 and {MCP_BINARY_MAX_BYTES}")
        await AuthorizationService.require_project_permission(
            principal.user_id,
            requested_project_id,
            ProjectPermission.ARTIFACT_DOWNLOAD,
        )
        download = await ScientificArrayContentService.load_npy(
            UUID(array_id),
            max_bytes=max_bytes,
            project_id=requested_project_id,
        )
        if len(download.content) > max_bytes:
            raise MCPContentTooLargeError(
                f"serialized NPY is {len(download.content)} bytes; MCP limit is {max_bytes}"
            )
        return _mcp_success(
            {
                "array_id": download.array_id,
                "filename": download.filename,
                "media_type": "application/x-npy",
                "size_bytes": len(download.content),
                "payload_sha256": download.payload_sha256,
                "unit": download.unit,
                "dtype": download.dtype,
                "shape": list(download.shape),
                "content_base64": base64.b64encode(download.content).decode("ascii"),
            }
        )
    except Exception as error:
        return _mcp_exception(error)


@mcp_server.tool(name="export_mapped_reaction_transition_state_geometries")  # type: ignore[untyped-decorator]
async def export_mapped_reaction_transition_state_geometries(
    project_id: str,
    after_binding_id: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Read a bounded page of mapped-reaction TS geometry and RDKit Mol records."""

    try:
        principal = _require_mcp_principal()
        requested_project_id = UUID(project_id)
        cursor = UUID(after_binding_id) if after_binding_id is not None else None
        if not 1 <= limit <= MCP_TS_GEOMETRY_EXPORT_MAX_RECORDS:
            raise ValueError(f"limit must be between 1 and {MCP_TS_GEOMETRY_EXPORT_MAX_RECORDS}")
        await AuthorizationService.require_project_permission(
            principal.user_id,
            requested_project_id,
            ProjectPermission.ARTIFACT_DOWNLOAD,
        )

        records: list[dict[str, Any]] = []
        response_bytes = 0
        has_more = False
        async for line in iter_mapped_reaction_geometry_export(
            requested_project_id,
            after_binding_id=cursor,
            max_records=limit + 1,
        ):
            if len(records) >= limit:
                has_more = True
                break
            if response_bytes + len(line) > MCP_TS_GEOMETRY_EXPORT_MAX_BYTES:
                if not records:
                    return _mcp_error(
                        "payload_too_large",
                        "A single geometry record exceeds the MCP page size; "
                        "use the REST JSONL export.",
                    )
                has_more = True
                break
            records.append(json.loads(line))
            response_bytes += len(line)

        next_cursor = None
        if records:
            next_cursor = records[-1]["value"]["geometry"]["geometry_binding_id"]
        return _mcp_success(
            {
                "project_id": requested_project_id,
                "records": records,
                "next_after_binding_id": next_cursor,
                "has_more": has_more,
            }
        )
    except Exception as error:
        return _mcp_exception(error)


mcp_server.add_middleware(QueryGuardMiddleware())
mcp_server.add_provider(calculation_log_workspace_app)
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
