from types import SimpleNamespace
from uuid import UUID

import pytest

from tricycle_reaction_db.application.services.artifact_content import (
    ArtifactContentService,
    ArtifactNotFoundError,
)
from tricycle_reaction_db.application.services.authorization import (
    _PROJECT_ROLE_PERMISSIONS,
    AuthorizationService,
    ProjectAccessDeniedError,
    ProjectPermission,
)
from tricycle_reaction_db.domain.enums import ArtifactVisibility, ProjectRole

USER_ID = UUID("00000000-0000-7000-8000-000000000701")
PROJECT_ID = UUID("00000000-0000-7000-8000-000000000702")


def test_project_role_permission_matrix() -> None:
    assert _PROJECT_ROLE_PERMISSIONS[ProjectRole.VIEWER] == {
        ProjectPermission.ARTIFACT_READ,
        ProjectPermission.ARTIFACT_DOWNLOAD,
    }
    assert _PROJECT_ROLE_PERMISSIONS[ProjectRole.CONTRIBUTOR] == {
        ProjectPermission.ARTIFACT_READ,
        ProjectPermission.ARTIFACT_DOWNLOAD,
        ProjectPermission.ARTIFACT_UPLOAD,
    }
    assert _PROJECT_ROLE_PERMISSIONS[ProjectRole.MANAGER] == frozenset(ProjectPermission)


@pytest.mark.asyncio
async def test_public_artifact_does_not_require_a_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected(*_: object) -> None:
        raise AssertionError("public artifact must not perform a project membership lookup")

    monkeypatch.setattr(AuthorizationService, "require_project_permission", unexpected)
    reference = SimpleNamespace(
        visibility=ArtifactVisibility.PUBLIC,
        project_id=PROJECT_ID,
    )

    await ArtifactContentService._authorize(
        reference,  # type: ignore[arg-type]
        None,
        ProjectPermission.ARTIFACT_READ,
    )


@pytest.mark.asyncio
async def test_project_artifact_requires_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = SimpleNamespace(
        visibility=ArtifactVisibility.PROJECT,
        project_id=PROJECT_ID,
    )
    with pytest.raises(ArtifactNotFoundError, match="artifact not found"):
        await ArtifactContentService._authorize(
            reference,  # type: ignore[arg-type]
            None,
            ProjectPermission.ARTIFACT_DOWNLOAD,
        )

    observed: list[tuple[UUID, UUID, ProjectPermission]] = []

    async def allowed(user_id: UUID, project_id: UUID, permission: ProjectPermission) -> None:
        observed.append((user_id, project_id, permission))

    monkeypatch.setattr(AuthorizationService, "require_project_permission", allowed)
    await ArtifactContentService._authorize(
        reference,  # type: ignore[arg-type]
        USER_ID,
        ProjectPermission.ARTIFACT_DOWNLOAD,
    )
    assert observed == [(USER_ID, PROJECT_ID, ProjectPermission.ARTIFACT_DOWNLOAD)]


@pytest.mark.asyncio
async def test_project_membership_denial_uses_not_found_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def denied(*_: object) -> None:
        raise ProjectAccessDeniedError("project permission denied")

    monkeypatch.setattr(AuthorizationService, "require_project_permission", denied)
    reference = SimpleNamespace(
        visibility=ArtifactVisibility.PROJECT,
        project_id=PROJECT_ID,
    )

    with pytest.raises(ArtifactNotFoundError, match="artifact not found"):
        await ArtifactContentService._authorize(
            reference,  # type: ignore[arg-type]
            USER_ID,
            ProjectPermission.ARTIFACT_READ,
        )
