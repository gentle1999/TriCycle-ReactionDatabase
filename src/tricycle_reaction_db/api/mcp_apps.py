"""FastMCP Apps backed by the authenticated application services.

The app in this module is deliberately a thin interactive client. It only
collects files in the MCP Apps renderer; the backend still creates the same
durable RustFS-backed upload batch used by REST, browser, CLI, and direct MCP
uploads.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastmcp.apps import FastMCPApp
from prefab_ui.actions import SetState, ShowToast
from prefab_ui.actions.mcp import CallTool
from prefab_ui.app import PrefabApp
from prefab_ui.components import (
    H3,
    Badge,
    Button,
    Card,
    CardContent,
    CardDescription,
    CardFooter,
    CardHeader,
    CardTitle,
    DropZone,
    Muted,
    Row,
    Select,
    SelectOption,
    Separator,
    Small,
    Text,
)
from prefab_ui.components.control_flow import ForEach, If
from prefab_ui.rx import RESULT, STATE

from tricycle_reaction_db.api.mcp_payloads import decode_base64_payload
from tricycle_reaction_db.application.dtos import ProjectView, UploadBatchItemView
from tricycle_reaction_db.application.services.artifact_uploads import (
    ArtifactUploadPayload,
)
from tricycle_reaction_db.application.services.authentication import (
    AuthenticatedPrincipal,
    AuthenticationError,
    current_principal,
)
from tricycle_reaction_db.application.services.authorization import (
    ProjectAccessDeniedError,
    ProjectPermission,
)
from tricycle_reaction_db.application.services.project_management import (
    ProjectManagementService,
)
from tricycle_reaction_db.application.services.upload_batches import (
    UploadBatchService,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.domain.enums import ArtifactKind, ProjectStatus


def _require_principal() -> AuthenticatedPrincipal:
    principal = current_principal()
    if principal is None:
        raise AuthenticationError("authenticated MCP principal is required")
    return principal


def _require_upload_permission(project: ProjectView) -> None:
    if project.status is not ProjectStatus.ACTIVE:
        raise ProjectAccessDeniedError("project is not active")
    if ProjectPermission.ARTIFACT_UPLOAD.value not in project.permissions:
        raise ProjectAccessDeniedError("artifact upload permission required")


def _file_summary(item: UploadBatchItemView) -> dict[str, Any]:
    return {
        "batch_id": str(item.batch_id),
        "item_id": str(item.id),
        "filename": item.original_filename,
        "size_bytes": item.size_bytes,
        "status": item.status.value,
        "parse_status": item.parse_status.value,
        "ingestion_status": (
            item.ingestion_status.value if item.ingestion_status is not None else None
        ),
    }


class CalculationLogWorkspace(FastMCPApp):
    """Interactive project-scoped calculation-log staging workspace."""

    def __init__(self) -> None:
        super().__init__("Calculation Log Workspace")
        self._register_tools()

    def _register_tools(self) -> None:
        provider = self

        @self.ui(
            name="open_calculation_log_workspace",
            title="Calculation log workspace",
            description=(
                "Open an interactive project-scoped workspace for selecting and "
                "staging one or more quantum-chemistry calculation logs."
            ),
        )
        async def open_calculation_log_workspace(
            project_id: str | None = None,
        ) -> PrefabApp:
            return await provider._render_workspace(project_id)

        @self.tool(
            name="stage_calculation_logs",
            description=(
                "Stage selected calculation logs as one durable upload batch. "
                "The UI supplies file dictionaries with name, type, and Base64 data."
            ),
        )
        async def stage_calculation_logs(
            project_id: str,
            files: list[dict[str, Any]],
        ) -> list[dict[str, Any]]:
            return await provider._stage_calculation_logs(project_id, files)

    async def _render_workspace(self, requested_project_id: str | None) -> PrefabApp:
        principal = _require_principal()
        projects = await ProjectManagementService.list_projects(
            principal,
            include_archived=False,
        )
        uploadable_projects = [
            project
            for project in projects
            if project.status is ProjectStatus.ACTIVE
            and ProjectPermission.ARTIFACT_UPLOAD.value in project.permissions
        ]

        selected_project_id: str | None = None
        if requested_project_id is not None:
            try:
                selected_project = await ProjectManagementService.get_project(
                    UUID(requested_project_id),
                    principal,
                )
            except ValueError as error:
                raise ValueError("project_id must be a valid UUID") from error
            _require_upload_permission(selected_project)
            selected_project_id = str(selected_project.id)
            if all(str(project.id) != selected_project_id for project in uploadable_projects):
                uploadable_projects.append(selected_project)
        elif uploadable_projects:
            selected_project_id = str(uploadable_projects[0].id)

        if not uploadable_projects:
            return PrefabApp(
                title="Calculation log workspace",
                view=Card(
                    children=[
                        CardHeader(
                            children=[
                                CardTitle("Calculation log workspace"),
                                CardDescription(
                                    "No active project grants this account artifact upload "
                                    "permission."
                                ),
                            ]
                        ),
                        CardContent(
                            children=[
                                Muted("Ask a project manager or organization admin for access.")
                            ]
                        ),
                    ]
                ),
                state={"pending": [], "staged": []},
            )

        return PrefabApp(
            title="Calculation log workspace",
            view=self._workspace_view(uploadable_projects, selected_project_id or ""),
            state={
                "project_id": selected_project_id or "",
                "pending": [],
                "staged": [],
            },
        )

    def _workspace_view(self, projects: list[ProjectView], selected_project_id: str) -> Card:
        with Card() as card:
            with CardHeader():
                H3("Calculation log workspace")
                Muted(
                    "Select a project, choose calculation logs, then stage them in one "
                    "durable batch."
                )
            with CardContent():
                Text("Project", bold=True)
                with Select(
                    name="project_id",
                    value=selected_project_id,
                    placeholder="Select a project",
                    required=True,
                ):
                    for project in projects:
                        SelectOption(
                            value=str(project.id),
                            label=(
                                f"{project.organization_name} / {project.name} ({project.slug})"
                            ),
                        )
                drop_zone_options: dict[str, Any] = {
                    "name": "pending",
                    "label": "Drop calculation logs here",
                    "description": (
                        "Gaussian, ORCA, and other supported output files; "
                        "one or more files are staged as a single upload batch."
                    ),
                    "multiple": True,
                    "max_size": get_settings().max_upload_bytes,
                }
                DropZone(**drop_zone_options)
                Text(f"Selected files: {STATE.pending.length()}")
                with If(STATE.pending.length() > 0):
                    Small("The files remain in the browser until you press Stage files.")
                with If(STATE.staged.length() > 0):
                    Separator()
                    Text("Staged files", bold=True)
                    with ForEach("staged") as item, Row(gap=2, align="center"):
                        Text(f"{item.filename}")
                        Badge(f"{item.status}", variant="success")
            with CardFooter():
                Button(
                    "Stage files",
                    icon="cloud-upload",
                    disabled=STATE.pending.length() == 0,
                    on_click=CallTool(
                        "stage_calculation_logs",
                        arguments={
                            "project_id": STATE.project_id,
                            "files": STATE.pending,
                        },
                        on_success=[
                            SetState("staged", RESULT),
                            SetState("pending", []),
                            ShowToast(
                                "Files staged; shared upload-worker parsing will continue "
                                "asynchronously.",
                                variant="success",
                            ),
                        ],
                        on_error=ShowToast(
                            "Could not stage files: {{ $error }}",
                            variant="error",
                        ),
                    ),
                )
        return card

    async def _stage_calculation_logs(
        self,
        project_id: str,
        files: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        principal = _require_principal()
        try:
            parsed_project_id = UUID(project_id)
        except ValueError as error:
            raise ValueError("project_id must be a valid UUID") from error
        project = await ProjectManagementService.get_project(parsed_project_id, principal)
        _require_upload_permission(project)
        if not files:
            raise ValueError("select at least one calculation log")
        settings = get_settings()
        if len(files) > settings.max_batch_files:
            raise ValueError(f"select at most {settings.max_batch_files} files per batch")

        payloads: list[ArtifactUploadPayload] = []
        for index, file in enumerate(files):
            if not isinstance(file, dict):
                raise ValueError(f"file {index + 1} must be an object")
            filename = file.get("name")
            if not isinstance(filename, str) or not filename.strip():
                raise ValueError(f"file {index + 1} requires a filename")
            media_type = file.get("type", "application/octet-stream")
            if not isinstance(media_type, str) or not media_type.strip():
                media_type = "application/octet-stream"
            encoded = file.get("data")
            if not isinstance(encoded, str):
                raise ValueError(f"{filename} is missing Base64 content")
            payload = decode_base64_payload(
                encoded,
                maximum_bytes=settings.max_upload_bytes,
                payload_description=f"calculation log {filename!r}",
            )
            payloads.append(
                ArtifactUploadPayload(
                    filename=filename,
                    media_type=media_type,
                    payload=payload,
                )
            )

        submission = await UploadBatchService.create_and_stage(
            files=payloads,
            artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
            project_id=parsed_project_id,
            user_id=principal.user_id,
        )
        return [_file_summary(item) for item in submission.items]


calculation_log_workspace_app = CalculationLogWorkspace()


__all__ = ["CalculationLogWorkspace", "calculation_log_workspace_app"]
