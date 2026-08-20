from datetime import UTC, datetime
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from tricycle_reaction_db.api.app import create_app
from tricycle_reaction_db.application.dtos import (
    ArtifactMetadataUpdate,
    ArtifactSummary,
    ProjectCreate,
    ProjectMemberRoleUpdate,
    ProjectMemberUpsert,
    ProjectMemberView,
    ProjectUpdate,
    ProjectView,
    UserPage,
    UserStatusUpdate,
    UserSummaryView,
)
from tricycle_reaction_db.application.services import (
    ArtifactManagementService,
    ProjectManagementService,
    UserManagementService,
)
from tricycle_reaction_db.application.services.authentication import AuthenticatedPrincipal
from tricycle_reaction_db.domain.enums import (
    ArtifactVisibility,
    ProjectRole,
    ProjectStatus,
    StorageStatus,
    UserStatus,
)
from tricycle_reaction_db.domain.identity import DEVELOPMENT_USER_ID, SYSTEM_ORGANIZATION_ID

PROJECT_ID = UUID("00000000-0000-7000-8000-000000000721")
MEMBER_ID = UUID("00000000-0000-7000-8000-000000000722")
ARTIFACT_ID = UUID("00000000-0000-7000-8000-000000000723")


def _project() -> ProjectView:
    return ProjectView(
        id=PROJECT_ID,
        organization_id=SYSTEM_ORGANIZATION_ID,
        organization_slug="tricycle-system",
        organization_name="TriCycle System",
        slug="managed-project",
        name="Managed Project",
        status=ProjectStatus.ACTIVE,
        role=ProjectRole.MANAGER,
        permissions=["artifact:delete", "project:manage"],
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
    )


def _user() -> UserSummaryView:
    return UserSummaryView(
        id=MEMBER_ID,
        display_name="Project Member",
        primary_email="member@example.test",
        status=UserStatus.ACTIVE,
        is_service_account=False,
    )


def _artifact() -> ArtifactSummary:
    return ArtifactSummary(
        id=ARTIFACT_ID,
        project_id=PROJECT_ID,
        created_by_user_id=DEVELOPMENT_USER_ID,
        visibility=ArtifactVisibility.PROJECT,
        original_filename="managed.log",
        content_sha256="a" * 64,
        size_bytes=42,
        media_type="text/plain",
        artifact_kind="auxiliary",
        storage_status=StorageStatus.AVAILABLE,
        preview_available=True,
    )


@pytest.mark.asyncio
async def test_project_management_routes_forward_authenticated_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    async def list_projects(
        principal: AuthenticatedPrincipal,
        *,
        include_archived: bool = False,
    ) -> list[ProjectView]:
        assert principal.user_id == DEVELOPMENT_USER_ID
        assert include_archived
        return [_project()]

    async def create_project(
        payload: ProjectCreate,
        principal: AuthenticatedPrincipal,
    ) -> ProjectView:
        calls.append(("create", payload))
        assert principal.user_id == DEVELOPMENT_USER_ID
        return _project()

    async def update_project(
        project_id: UUID,
        payload: ProjectUpdate,
        principal: AuthenticatedPrincipal,
    ) -> ProjectView:
        assert project_id == PROJECT_ID
        assert principal.user_id == DEVELOPMENT_USER_ID
        calls.append(("update", payload))
        return _project().model_copy(update={"name": payload.name or _project().name})

    async def add_member(
        project_id: UUID,
        payload: ProjectMemberUpsert,
        principal: AuthenticatedPrincipal,
    ) -> ProjectMemberView:
        assert project_id == PROJECT_ID
        assert principal.user_id == DEVELOPMENT_USER_ID
        calls.append(("add-member", payload))
        return ProjectMemberView(
            user_id=payload.user_id,
            display_name="Project Member",
            role=payload.role,
        )

    async def update_member(
        project_id: UUID,
        user_id: UUID,
        payload: ProjectMemberRoleUpdate,
        principal: AuthenticatedPrincipal,
    ) -> ProjectMemberView:
        assert (project_id, user_id) == (PROJECT_ID, MEMBER_ID)
        assert principal.user_id == DEVELOPMENT_USER_ID
        calls.append(("update-member", payload))
        return ProjectMemberView(
            user_id=user_id,
            display_name="Project Member",
            role=payload.role,
        )

    async def remove_member(
        project_id: UUID,
        user_id: UUID,
        principal: AuthenticatedPrincipal,
    ) -> None:
        assert (project_id, user_id) == (PROJECT_ID, MEMBER_ID)
        assert principal.user_id == DEVELOPMENT_USER_ID
        calls.append(("remove-member", user_id))

    monkeypatch.setattr(ProjectManagementService, "list_projects", staticmethod(list_projects))
    monkeypatch.setattr(ProjectManagementService, "create_project", staticmethod(create_project))
    monkeypatch.setattr(ProjectManagementService, "update_project", staticmethod(update_project))
    monkeypatch.setattr(ProjectManagementService, "upsert_member", staticmethod(add_member))
    monkeypatch.setattr(
        ProjectManagementService,
        "update_member_role",
        staticmethod(update_member),
    )
    monkeypatch.setattr(ProjectManagementService, "remove_member", staticmethod(remove_member))

    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        listed = await client.get("/api/projects?include_archived=true")
        created = await client.post(
            "/api/projects",
            json={
                "organization_id": str(SYSTEM_ORGANIZATION_ID),
                "slug": "managed-project",
                "name": "Managed Project",
            },
        )
        updated = await client.patch(
            f"/api/projects/{PROJECT_ID}",
            json={"name": "Renamed Project"},
        )
        added = await client.post(
            f"/api/projects/{PROJECT_ID}/members",
            json={"user_id": str(MEMBER_ID), "role": "viewer"},
        )
        changed = await client.patch(
            f"/api/projects/{PROJECT_ID}/members/{MEMBER_ID}",
            json={"role": "contributor"},
        )
        removed = await client.delete(f"/api/projects/{PROJECT_ID}/members/{MEMBER_ID}")

    assert listed.status_code == 200
    assert created.status_code == 201
    assert updated.json()["name"] == "Renamed Project"
    assert added.json()["role"] == "viewer"
    assert changed.json()["role"] == "contributor"
    assert removed.status_code == 204
    assert [name for name, _ in calls] == [
        "create",
        "update",
        "add-member",
        "update-member",
        "remove-member",
    ]


@pytest.mark.asyncio
async def test_artifact_update_and_delete_routes_use_management_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[UUID, UUID]] = []

    async def update_metadata(
        artifact_id: UUID,
        payload: ArtifactMetadataUpdate,
        *,
        user_id: UUID,
    ) -> ArtifactSummary:
        assert artifact_id == ARTIFACT_ID
        assert user_id == DEVELOPMENT_USER_ID
        assert payload.original_filename == "renamed.log"
        assert payload.visibility is ArtifactVisibility.PUBLIC
        return _artifact().model_copy(
            update={
                "original_filename": payload.original_filename,
                "visibility": payload.visibility,
            }
        )

    async def retire(artifact_id: UUID, *, user_id: UUID) -> None:
        observed.append((artifact_id, user_id))

    monkeypatch.setattr(
        ArtifactManagementService,
        "update_metadata",
        staticmethod(update_metadata),
    )
    monkeypatch.setattr(ArtifactManagementService, "retire", staticmethod(retire))

    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        updated = await client.patch(
            f"/api/artifacts/{ARTIFACT_ID}",
            json={"original_filename": "renamed.log", "visibility": "public"},
        )
        removed = await client.delete(f"/api/artifacts/{ARTIFACT_ID}")

    assert updated.status_code == 200
    assert updated.json()["original_filename"] == "renamed.log"
    assert updated.json()["visibility"] == "public"
    assert removed.status_code == 204
    assert observed == [(ARTIFACT_ID, DEVELOPMENT_USER_ID)]


@pytest.mark.asyncio
async def test_user_management_routes_forward_directory_and_status_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def list_users(
        principal: AuthenticatedPrincipal,
        **values: object,
    ) -> UserPage:
        assert principal.user_id == DEVELOPMENT_USER_ID
        assert values["query"] == "member"
        assert values["project_id"] == PROJECT_ID
        return UserPage(items=[_user()], total=1, limit=50, offset=0)

    async def update_status(
        user_id: UUID,
        payload: UserStatusUpdate,
        principal: AuthenticatedPrincipal,
    ) -> UserSummaryView:
        assert user_id == MEMBER_ID
        assert principal.user_id == DEVELOPMENT_USER_ID
        return _user().model_copy(update={"status": payload.status})

    monkeypatch.setattr(UserManagementService, "list_users", staticmethod(list_users))
    monkeypatch.setattr(UserManagementService, "update_status", staticmethod(update_status))

    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        listed = await client.get(f"/api/users?q=member&project_id={PROJECT_ID}")
        suspended = await client.patch(
            f"/api/users/{MEMBER_ID}/status",
            json={"status": "suspended"},
        )

    assert listed.status_code == 200
    assert listed.json()["items"][0]["project_role"] is None
    assert suspended.status_code == 200
    assert suspended.json()["status"] == "suspended"
