import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from mcp.types import TextContent

from tricycle_reaction_db.api import mcp as mcp_module
from tricycle_reaction_db.api import mcp_apps
from tricycle_reaction_db.api.mcp import QueryGuardMiddleware, _mcp_success, mcp_server
from tricycle_reaction_db.application.dtos import (
    OrganizationAccessView,
    OrganizationMemberView,
    ProjectView,
    UploadBatchItemView,
    UploadBatchView,
)
from tricycle_reaction_db.application.rate_limits import RateLimitBackendUnavailable
from tricycle_reaction_db.application.services.artifact_upload_types import (
    ArtifactUploadPayload,
)
from tricycle_reaction_db.application.services.authentication import (
    AuthenticatedPrincipal,
    reset_current_principal,
    set_current_principal,
)
from tricycle_reaction_db.application.services.upload_batches import StagedUploadSubmission
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactKind,
    ImportMaterializationStatus,
    ImportParseStatus,
    OrganizationRole,
    OrganizationStatus,
    ProjectStatus,
    UploadBatchItemStatus,
    UploadBatchStatus,
)
from tricycle_reaction_db.domain.identity import (
    DEVELOPMENT_IDENTITY_ISSUER,
    DEVELOPMENT_IDENTITY_SUBJECT,
    DEVELOPMENT_USER_ID,
)


async def _call(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await mcp_server.call_tool(tool_name, arguments)
    content = result.content[0]
    assert isinstance(content, TextContent)
    return json.loads(content.text)


async def _call_as_development_user(
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    principal = AuthenticatedPrincipal(
        user_id=DEVELOPMENT_USER_ID,
        display_name="Development User",
        primary_email="developer@localhost",
        is_service_account=False,
        issuer=DEVELOPMENT_IDENTITY_ISSUER,
        subject=DEVELOPMENT_IDENTITY_SUBJECT,
    )
    token = set_current_principal(principal)
    try:
        return await _call(tool_name, arguments)
    finally:
        reset_current_principal(token)


def _staged_submission(
    *,
    project_id: UUID,
    filename: str,
    payload: bytes,
    artifact_id: UUID,
) -> StagedUploadSubmission:
    now = datetime.now(UTC)
    batch_id = UUID("00000000-0000-7000-8000-000000000720")
    batch = UploadBatchView(
        id=batch_id,
        created_at=now,
        updated_at=now,
        project_id=project_id,
        created_by_user_id=DEVELOPMENT_USER_ID,
        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
        status=UploadBatchStatus.ACTIVE,
        shared_metadata={},
        total_count=1,
        total_bytes=len(payload),
        succeeded_count=0,
        failed_count=0,
        cancelled_count=0,
        uploading_count=0,
        staged_count=1,
        processing_count=0,
    )
    item = UploadBatchItemView(
        id=UUID("00000000-0000-7000-8000-000000000721"),
        batch_id=batch_id,
        created_at=now,
        updated_at=now,
        client_file_id=UUID("00000000-0000-7000-8000-000000000722"),
        position=0,
        original_filename=filename,
        relative_path=filename,
        size_bytes=len(payload),
        media_type="text/plain",
        status=UploadBatchItemStatus.STAGED,
        attempt_count=1,
        processing_attempt_count=0,
        content_sha256=None,
        expected_file_sha256=None,
        parse_status=ImportParseStatus.PENDING,
        materialization_status=ImportMaterializationStatus.SUCCEEDED,
        artifact_file_id=artifact_id,
        ingestion_id=UUID("00000000-0000-7000-8000-000000000723"),
        ingestion_status=ArtifactIngestionStatus.PENDING,
        metadata={},
    )
    return StagedUploadSubmission(batch=batch, items=(item,))


@pytest.mark.asyncio
async def test_mcp_exposes_query_management_and_import_tools() -> None:
    tools = await mcp_server.list_tools()

    assert {tool.name for tool in tools} == {
        "list_apps",
        "describe_compose_schema",
        "describe_compose_method",
        "compose_query",
        "list_organizations",
        "create_organization",
        "list_organization_members",
        "upsert_organization_member",
        "remove_organization_member",
        "create_project",
        "list_projects",
        "get_project",
        "preview_project_cleanup",
        "delete_project_data",
        "delete_artifact",
        "update_artifact_notes",
        "update_project",
        "list_project_members",
        "upsert_project_member",
        "remove_project_member",
        "list_project_invitations",
        "create_project_invitation",
        "revoke_project_invitation",
        "resend_project_invitation",
        "accept_project_invitation",
        "list_project_audit",
        "upload_calculation_log",
        "register_import_manifest",
        "start_import_job",
        "get_import_status",
        "list_import_failures",
        "retry_import_items",
        "pause_import",
        "resume_import",
        "cancel_import",
        "open_calculation_log_workspace",
    }


@pytest.mark.asyncio
async def test_mcp_control_tools_require_transport_authentication() -> None:
    result = await _call(
        "start_import_job", {"import_job_id": "00000000-0000-7000-8000-000000000701"}
    )

    assert result == {
        "success": False,
        "error": {
            "code": "authentication_required",
            "message": "authenticated MCP principal is required",
        },
    }


@pytest.mark.asyncio
async def test_mcp_project_cleanup_tools_use_authenticated_user_and_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = UUID("00000000-0000-7000-8000-000000000714")
    artifact_id = UUID("00000000-0000-7000-8000-000000000715")
    observed: dict[str, Any] = {}

    async def preview(requested_project_id: UUID, *, user_id: UUID) -> dict[str, Any]:
        observed["preview"] = (requested_project_id, user_id)
        return {"project_id": requested_project_id, "artifact_count": 2}

    async def clear(
        requested_project_id: UUID,
        *,
        user_id: UUID,
        confirmation: str,
    ) -> dict[str, Any]:
        observed["clear"] = (requested_project_id, user_id, confirmation)
        return {"project_id": requested_project_id, "rustfs_objects_deleted": 2}

    async def retire(requested_artifact_id: UUID, *, user_id: UUID) -> None:
        observed["artifact"] = (requested_artifact_id, user_id)

    monkeypatch.setattr(
        mcp_module.ProjectDataRemovalService,
        "preview",
        staticmethod(preview),
    )
    monkeypatch.setattr(
        mcp_module.ProjectDataRemovalService,
        "clear",
        staticmethod(clear),
    )
    monkeypatch.setattr(
        mcp_module.ArtifactManagementService,
        "retire",
        staticmethod(retire),
    )

    preview_result = await _call_as_development_user(
        "preview_project_cleanup",
        {"project_id": str(project_id)},
    )
    clear_result = await _call_as_development_user(
        "delete_project_data",
        {"project_id": str(project_id), "confirmation": "rits-zero-shot-da"},
    )
    artifact_result = await _call_as_development_user(
        "delete_artifact",
        {"artifact_id": str(artifact_id)},
    )

    assert preview_result["data"]["artifact_count"] == 2
    assert clear_result["data"]["rustfs_objects_deleted"] == 2
    assert artifact_result["data"] == {"removed": True, "artifact_id": str(artifact_id)}
    assert observed["preview"] == (project_id, DEVELOPMENT_USER_ID)
    assert observed["clear"] == (project_id, DEVELOPMENT_USER_ID, "rits-zero-shot-da")
    assert observed["artifact"] == (artifact_id, DEVELOPMENT_USER_ID)


@pytest.mark.asyncio
async def test_mcp_artifact_notes_use_authenticated_management_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id = UUID("00000000-0000-7000-8000-000000000716")
    observed: list[tuple[UUID, UUID, str | None]] = []

    async def update_metadata(
        requested_artifact_id: UUID,
        payload: Any,
        *,
        user_id: UUID,
    ) -> dict[str, Any]:
        observed.append((requested_artifact_id, user_id, payload.notes))
        return {"artifact_id": requested_artifact_id, "notes": payload.notes}

    monkeypatch.setattr(
        mcp_module.ArtifactManagementService,
        "update_metadata",
        staticmethod(update_metadata),
    )

    updated = await _call_as_development_user(
        "update_artifact_notes",
        {"artifact_id": str(artifact_id), "notes": "external experiment context"},
    )
    cleared = await _call_as_development_user(
        "update_artifact_notes",
        {"artifact_id": str(artifact_id), "notes": None},
    )

    assert updated["data"] == {
        "artifact_id": str(artifact_id),
        "notes": "external experiment context",
    }
    assert cleared["data"] == {"artifact_id": str(artifact_id), "notes": None}
    assert observed == [
        (artifact_id, DEVELOPMENT_USER_ID, "external experiment context"),
        (artifact_id, DEVELOPMENT_USER_ID, None),
    ]


@pytest.mark.asyncio
async def test_mcp_apps_render_a_project_scoped_calculation_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = ProjectView.model_validate(
        {
            "id": "00000000-0000-7000-8000-000000000730",
            "organization_id": "00000000-0000-7000-8000-000000000731",
            "organization_slug": "research",
            "organization_name": "Research",
            "slug": "cycloaddition",
            "name": "Cycloaddition",
            "status": ProjectStatus.ACTIVE,
            "permissions": ["artifact:upload"],
        }
    )

    async def list_projects(
        _principal: AuthenticatedPrincipal,
        *,
        include_archived: bool = False,
    ) -> list[ProjectView]:
        assert include_archived is False
        return [project]

    monkeypatch.setattr(
        mcp_apps.ProjectManagementService,
        "list_projects",
        staticmethod(list_projects),
    )

    principal = AuthenticatedPrincipal(
        user_id=DEVELOPMENT_USER_ID,
        display_name="Development User",
        primary_email="developer@localhost",
        is_service_account=False,
        issuer=DEVELOPMENT_IDENTITY_ISSUER,
        subject=DEVELOPMENT_IDENTITY_SUBJECT,
    )
    token = set_current_principal(principal)
    try:
        result = await mcp_server.call_tool("open_calculation_log_workspace", {})
    finally:
        reset_current_principal(token)

    assert result.structured_content is not None
    assert result.structured_content["$prefab"]["version"] == "0.3"
    assert result.structured_content["state"] == {
        "project_id": "00000000-0000-7000-8000-000000000730",
        "pending": [],
        "staged": [],
    }
    serialized = json.dumps(result.structured_content)
    assert "Drop calculation logs here" in serialized
    assert "stage_calculation_logs" in serialized
    assert "{{ pending }}" in serialized


@pytest.mark.asyncio
async def test_mcp_app_stages_all_selected_files_in_one_upload_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = UUID("00000000-0000-7000-8000-000000000732")
    project = ProjectView.model_validate(
        {
            "id": str(project_id),
            "organization_id": "00000000-0000-7000-8000-000000000733",
            "organization_slug": "research",
            "organization_name": "Research",
            "slug": "cycloaddition",
            "name": "Cycloaddition",
            "status": ProjectStatus.ACTIVE,
            "permissions": ["artifact:upload"],
        }
    )
    observed: dict[str, Any] = {}

    async def get_project(
        requested_project_id: UUID,
        _principal: AuthenticatedPrincipal,
    ) -> ProjectView:
        assert requested_project_id == project_id
        return project

    async def create_and_stage(**kwargs: Any) -> Any:
        observed.update(kwargs)
        items = tuple(
            SimpleNamespace(
                batch_id=UUID("00000000-0000-7000-8000-000000000734"),
                id=UUID(f"00000000-0000-7000-8000-00000000073{5 + index}"),
                original_filename=upload.filename,
                size_bytes=len(upload.payload or b""),
                status=UploadBatchItemStatus.STAGED,
                parse_status=ImportParseStatus.PENDING,
                ingestion_status=ArtifactIngestionStatus.PENDING,
            )
            for index, upload in enumerate(kwargs["files"])
        )
        return SimpleNamespace(items=items)

    monkeypatch.setattr(
        mcp_apps.ProjectManagementService,
        "get_project",
        staticmethod(get_project),
    )
    monkeypatch.setattr(
        mcp_apps.UploadBatchService,
        "create_and_stage",
        staticmethod(create_and_stage),
    )

    principal = AuthenticatedPrincipal(
        user_id=DEVELOPMENT_USER_ID,
        display_name="Development User",
        primary_email="developer@localhost",
        is_service_account=False,
        issuer=DEVELOPMENT_IDENTITY_ISSUER,
        subject=DEVELOPMENT_IDENTITY_SUBJECT,
    )
    tool = await mcp_apps.calculation_log_workspace_app._get_tool("stage_calculation_logs")
    assert tool is not None
    hash_prefix = tool.meta["fastmcp"]["_tool_hash"]
    token = set_current_principal(principal)
    try:
        result = await mcp_server.call_tool(
            f"{hash_prefix}_{tool.name}",
            {
                "project_id": str(project_id),
                "files": [
                    {
                        "name": "reactant.log",
                        "type": "text/plain",
                        "data": base64.b64encode(b"reactant").decode("ascii"),
                    },
                    {
                        "name": "product.log",
                        "type": "text/plain",
                        "data": base64.b64encode(b"product").decode("ascii"),
                    },
                ],
            },
        )
    finally:
        reset_current_principal(token)

    assert result.structured_content is not None
    assert len(result.structured_content["result"]) == 2
    assert observed["project_id"] == project_id
    assert observed["user_id"] == DEVELOPMENT_USER_ID
    assert observed["artifact_kind"] is ArtifactKind.CALCULATION_OUTPUT
    assert [upload.filename for upload in observed["files"]] == [
        "reactant.log",
        "product.log",
    ]
    assert [upload.payload for upload in observed["files"]] == [b"reactant", b"product"]


@pytest.mark.asyncio
async def test_mcp_organization_tools_use_the_authenticated_user_and_org_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = UUID("00000000-0000-7000-8000-000000000710")
    member_id = UUID("00000000-0000-7000-8000-000000000711")
    observed: dict[str, Any] = {}

    async def list_organizations(user_id: UUID) -> list[OrganizationAccessView]:
        observed["list_user_id"] = user_id
        return [
            OrganizationAccessView(
                id=organization_id,
                slug="research",
                name="Research",
                status=OrganizationStatus.ACTIVE,
                role=OrganizationRole.OWNER,
                can_create_projects=True,
            )
        ]

    async def create_organization(payload: Any, principal: AuthenticatedPrincipal) -> Any:
        observed["create_payload"] = payload
        observed["create_principal"] = principal
        return OrganizationAccessView(
            id=organization_id,
            slug=payload.slug,
            name=payload.name,
            status=OrganizationStatus.ACTIVE,
            role=OrganizationRole.OWNER,
            can_create_projects=True,
        )

    async def upsert_member(
        requested_organization_id: UUID,
        payload: Any,
        principal: AuthenticatedPrincipal,
    ) -> OrganizationMemberView:
        observed["member_organization_id"] = requested_organization_id
        observed["member_payload"] = payload
        observed["member_principal"] = principal
        return OrganizationMemberView(
            user_id=payload.user_id,
            display_name="New Member",
            primary_email="member@example.test",
            role=payload.role,
        )

    async def remove_member(
        requested_organization_id: UUID,
        user_id: UUID,
        principal: AuthenticatedPrincipal,
    ) -> None:
        observed["remove"] = (requested_organization_id, user_id, principal)

    monkeypatch.setattr(
        mcp_module.AuthorizationService,
        "organization_accesses",
        staticmethod(list_organizations),
    )
    monkeypatch.setattr(
        mcp_module.OrganizationManagementService,
        "create_organization",
        staticmethod(create_organization),
    )
    monkeypatch.setattr(
        mcp_module.OrganizationManagementService,
        "upsert_member",
        staticmethod(upsert_member),
    )
    monkeypatch.setattr(
        mcp_module.OrganizationManagementService,
        "remove_member",
        staticmethod(remove_member),
    )

    listed = await _call_as_development_user("list_organizations", {})
    created = await _call_as_development_user(
        "create_organization",
        {"slug": "research", "name": "Research"},
    )
    member = await _call_as_development_user(
        "upsert_organization_member",
        {
            "organization_id": str(organization_id),
            "user_id": str(member_id),
            "role": "admin",
        },
    )
    removed = await _call_as_development_user(
        "remove_organization_member",
        {"organization_id": str(organization_id), "user_id": str(member_id)},
    )

    assert listed["data"][0]["id"] == str(organization_id)
    assert created["data"]["role"] == "owner"
    assert observed["list_user_id"] == DEVELOPMENT_USER_ID
    assert observed["create_payload"].slug == "research"
    assert observed["create_principal"].user_id == DEVELOPMENT_USER_ID
    assert observed["member_organization_id"] == organization_id
    assert observed["member_payload"].role is OrganizationRole.ADMIN
    assert observed["member_principal"].user_id == DEVELOPMENT_USER_ID
    assert member["data"]["user_id"] == str(member_id)
    assert removed["data"]["removed"] is True
    assert observed["remove"][:2] == (organization_id, member_id)


@pytest.mark.asyncio
async def test_mcp_calculation_upload_decodes_base64_and_delegates_project_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = UUID("00000000-0000-7000-8000-000000000712")
    payload = b"%chk=calculation\n#p wb97xd/def2-svp\n"
    observed: dict[str, Any] = {}

    async def create_and_stage(**kwargs: Any) -> StagedUploadSubmission:
        observed.update(kwargs)
        files = kwargs["files"]
        assert isinstance(files, list)
        assert len(files) == 1
        upload = files[0]
        assert isinstance(upload, ArtifactUploadPayload)
        return _staged_submission(
            project_id=project_id,
            filename=upload.filename,
            payload=upload.payload or b"",
            artifact_id=UUID("00000000-0000-7000-8000-000000000713"),
        )

    monkeypatch.setattr(
        mcp_module.UploadBatchService,
        "create_and_stage",
        staticmethod(create_and_stage),
    )

    result = await _call_as_development_user(
        "upload_calculation_log",
        {
            "project_id": str(project_id),
            "filename": "calculation.log",
            "content_base64": base64.b64encode(payload).decode("ascii"),
            "media_type": "text/plain",
        },
    )

    assert result == {
        "success": True,
        "data": {
            "batch": {
                "id": "00000000-0000-7000-8000-000000000720",
                "created_at": result["data"]["batch"]["created_at"],
                "updated_at": result["data"]["batch"]["updated_at"],
                "project_id": str(project_id),
                "created_by_user_id": str(DEVELOPMENT_USER_ID),
                "artifact_kind": "calculation_output",
                "status": "active",
                "shared_metadata": {},
                "archive_sha256": None,
                "manifest_sha256": None,
                "manifest_schema_version": None,
                "total_count": 1,
                "total_bytes": len(payload),
                "succeeded_count": 0,
                "failed_count": 0,
                "cancelled_count": 0,
                "uploading_count": 0,
                "staged_count": 1,
                "processing_count": 0,
            },
            "item": {
                "id": "00000000-0000-7000-8000-000000000721",
                "batch_id": "00000000-0000-7000-8000-000000000720",
                "created_at": result["data"]["item"]["created_at"],
                "updated_at": result["data"]["item"]["updated_at"],
                "client_file_id": "00000000-0000-7000-8000-000000000722",
                "position": 0,
                "original_filename": "calculation.log",
                "relative_path": "calculation.log",
                "size_bytes": len(payload),
                "media_type": "text/plain",
                "status": "staged",
                "attempt_count": 1,
                "processing_attempt_count": 0,
                "content_sha256": None,
                "expected_file_sha256": None,
                "is_gaussian_log": False,
                "selection_status": "selected",
                "parse_status": "pending",
                "materialization_status": "succeeded",
                "parse_revision_id": None,
                "artifact_file_id": "00000000-0000-7000-8000-000000000713",
                "ingestion_id": "00000000-0000-7000-8000-000000000723",
                "ingestion_status": "pending",
                "ingestion_error_message": None,
                "error_code": None,
                "error_message": None,
                "metadata": {},
            },
        },
    }
    assert observed == {
        "files": [
            ArtifactUploadPayload(
                filename="calculation.log",
                media_type="text/plain",
                payload=payload,
                relative_path=None,
                expected_sha256=None,
                expected_size_bytes=None,
            )
        ],
        "artifact_kind": ArtifactKind.CALCULATION_OUTPUT,
        "project_id": project_id,
        "user_id": DEVELOPMENT_USER_ID,
    }


@pytest.mark.asyncio
async def test_mcp_calculation_upload_rejects_invalid_base64_before_service_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def create_and_stage(**_: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(
        mcp_module.UploadBatchService,
        "create_and_stage",
        staticmethod(create_and_stage),
    )

    result = await _call_as_development_user(
        "upload_calculation_log",
        {
            "project_id": "00000000-0000-7000-8000-000000000714",
            "filename": "calculation.log",
            "content_base64": "not-base64",
        },
    )

    assert result["success"] is False
    assert result["error"]["code"] == "invalid_argument"
    assert called is False


def test_mcp_success_serializes_nested_pydantic_payloads() -> None:
    project_id = "00000000-0000-7000-8000-000000000702"
    project = ProjectView.model_validate(
        {
            "id": project_id,
            "organization_id": "00000000-0000-7000-8000-000000000703",
            "organization_slug": "example-org",
            "organization_name": "Example Organization",
            "slug": "example-project",
            "name": "Example Project",
            "status": ProjectStatus.ACTIVE,
        }
    )

    result = _mcp_success({"job": project, "items": [project]})

    assert result["data"] == {
        "job": {
            "id": project_id,
            "organization_id": "00000000-0000-7000-8000-000000000703",
            "organization_slug": "example-org",
            "organization_name": "Example Organization",
            "owner_user_id": None,
            "created_by_user_id": None,
            "slug": "example-project",
            "name": "Example Project",
            "data_source": {},
            "model_checkpoint": {},
            "calculation_protocol": {},
            "status": "active",
            "role": None,
            "organization_role": None,
            "permissions": [],
            "created_at": None,
        },
        "items": [
            {
                "id": project_id,
                "organization_id": "00000000-0000-7000-8000-000000000703",
                "organization_slug": "example-org",
                "organization_name": "Example Organization",
                "owner_user_id": None,
                "created_by_user_id": None,
                "slug": "example-project",
                "name": "Example Project",
                "data_source": {},
                "model_checkpoint": {},
                "calculation_protocol": {},
                "status": "active",
                "role": None,
                "organization_role": None,
                "permissions": [],
                "created_at": None,
            }
        ],
    }


@pytest.mark.asyncio
async def test_mcp_progressive_disclosure_describes_query_service() -> None:
    apps = await _call("list_apps", {})
    schema = await _call(
        "describe_compose_schema",
        {"app_name": "example-chemistry-database"},
    )
    method = await _call(
        "describe_compose_method",
        {
            "app_name": "example-chemistry-database",
            "service_name": "CalculationQueryService",
            "method_name": "get_calculation_frame",
        },
    )

    assert apps["success"] is True
    assert apps["data"]["apps"][0]["name"] == "example-chemistry-database"
    assert schema["success"] is True
    assert "CalculationQueryService" in {service["name"] for service in schema["data"]["services"]}
    assert method["success"] is True
    assert method["data"]["method"]["return_type"] == "CalculationFrameDetail"
    assert "ScientificArraySummary" in method["data"]["sdl"]


@pytest.mark.asyncio
async def test_mcp_describes_automatically_registered_chemistry_searches() -> None:
    schema = await _call(
        "describe_compose_schema",
        {"app_name": "example-chemistry-database"},
    )
    topology_method = await _call(
        "describe_compose_method",
        {
            "app_name": "example-chemistry-database",
            "service_name": "MolecularTopologyQueryService",
            "method_name": "search_topologies",
        },
    )

    service_names = {service["name"] for service in schema["data"]["services"]}
    assert "MolecularFormulaQueryService" in service_names
    assert "MolecularTopologyQueryService" in service_names
    assert topology_method["success"] is True
    assert topology_method["data"]["method"]["return_type"] == "MolecularTopologySearchPage!"
    assert "SimilarityMetric" in topology_method["data"]["sdl"]


@pytest.mark.asyncio
async def test_mcp_describes_advanced_and_operational_queries() -> None:
    schema = await _call(
        "describe_compose_schema",
        {"app_name": "example-chemistry-database"},
    )
    result_method = await _call(
        "describe_compose_method",
        {
            "app_name": "example-chemistry-database",
            "service_name": "CalculationResultQueryService",
            "method_name": "get_calculation_results",
        },
    )
    service_names = {service["name"] for service in schema["data"]["services"]}
    assert {
        "CalculationResultQueryService",
        "WorkflowManifestQueryService",
        "StorageGarbageCollectionQueryService",
        "MolecularTopologyDerivationQueryService",
    } <= service_names
    assert result_method["success"] is True
    assert result_method["data"]["method"]["return_type"] == "CalculationResultDetail"
    assert "ElectronicStateSetView" in result_method["data"]["sdl"]


@pytest.mark.asyncio
async def test_mcp_compose_query_uses_graphql_envelope() -> None:
    result = await _call(
        "compose_query",
        {
            "app_name": "example-chemistry-database",
            "query": "{ SystemService { info { name version } } }",
        },
    )

    assert result["errors"] == []
    assert result["data"]["SystemService"]["info"]["name"] == ("Example Chemistry Database")


@pytest.mark.asyncio
async def test_mcp_structure_budget_uses_same_error_code() -> None:
    structure = "C" * 16_385
    query = (
        "{ MappedReactionQueryService { list_mapped_reactions(project_id: "
        '"00000000-0000-7000-8000-000000000201", '
        f'similarity_reaction_smiles: "{structure}", limit: 1) '
        "{ page { total } } } }"
    )
    result = await _call(
        "compose_query",
        {"app_name": "example-chemistry-database", "query": query},
    )

    assert result["data"] is None
    assert result["errors"][0]["extensions"]["code"] == "query_budget_exceeded"


@pytest.mark.asyncio
async def test_mcp_requires_project_scope_for_project_owned_queries() -> None:
    result = await _call(
        "compose_query",
        {
            "app_name": "example-chemistry-database",
            "query": (
                "{ MappedReactionQueryService { "
                "list_mapped_reactions(limit: 1) { items { id } } } }"
            ),
        },
    )

    assert result["data"] is None
    assert result["errors"][0]["extensions"]["code"] == "project_scope_required"


@pytest.mark.asyncio
async def test_mcp_rate_limit_backend_failure_returns_error_without_fallback() -> None:
    middleware = QueryGuardMiddleware()

    class _UnavailableRateLimiter:
        maximum_requests = 10

        async def check(self, _key: str) -> None:
            raise RateLimitBackendUnavailable

    middleware._limiter = _UnavailableRateLimiter()  # type: ignore[assignment]
    context = SimpleNamespace(
        message=SimpleNamespace(name="compose_query", arguments={}),
        fastmcp_context=None,
    )

    async def call_next(_context: Any) -> None:
        pytest.fail("MCP execution must not continue when Redis is unavailable")

    result = await middleware.on_call_tool(context, call_next)

    assert result.structured_content == {
        "data": None,
        "errors": [
            {
                "message": "shared rate-limit backend is unavailable",
                "extensions": {"code": "rate_limit_backend_unavailable"},
            }
        ],
    }
