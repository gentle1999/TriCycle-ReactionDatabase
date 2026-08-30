import os
from hashlib import sha256
from uuid import uuid4

import pytest

from tricycle_reaction_db.application.dtos import ArtifactMetadataUpdate
from tricycle_reaction_db.application.services import (
    ArtifactManagementService,
    ArtifactNotFoundError,
    ArtifactQueryService,
    ArtifactUploadService,
)
from tricycle_reaction_db.application.services.artifact_content import ArtifactContentService
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    Project,
    ProjectMembership,
    UserAccount,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    ArtifactVisibility,
    ProjectRole,
    StorageStatus,
)
from tricycle_reaction_db.domain.identity import (
    DEVELOPMENT_USER_ID,
    SYSTEM_ORGANIZATION_ID,
    SYSTEM_PROJECT_ID,
)
from tricycle_reaction_db.storage.rustfs import RustFSObjectStore, RustFSSettings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.rustfs,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1"
        or os.getenv("TRICYCLE_RUN_RUSTFS_TESTS") != "1",
        reason="set database and RustFS integration flags to run artifact removal tests",
    ),
]


@pytest.mark.asyncio
async def test_artifact_removal_retires_catalogue_and_deletes_object() -> None:
    payload = f"artifact removal {uuid4()}\n".encode()
    uploaded = await ArtifactUploadService.upload(
        payload=payload,
        filename="remove-me.txt",
        media_type="text/plain",
        artifact_kind=ArtifactKind.AUXILIARY,
        project_id=SYSTEM_PROJECT_ID,
        user_id=DEVELOPMENT_USER_ID,
    )
    artifact_id = uploaded.artifact_id
    object_key = None

    try:
        async with session_factory() as session:
            artifact = await session.get(ArtifactFile, artifact_id)
            assert artifact is not None
            object_key = artifact.object_key
            assert artifact.storage_status is StorageStatus.AVAILABLE

        updated = await ArtifactManagementService.update_metadata(
            artifact_id,
            ArtifactMetadataUpdate(
                original_filename="renamed-before-removal.txt",
                visibility=ArtifactVisibility.PUBLIC,
            ),
            user_id=DEVELOPMENT_USER_ID,
        )
        assert updated.original_filename == "renamed-before-removal.txt"
        assert updated.visibility == ArtifactVisibility.PUBLIC
        assert updated.content_sha256 == sha256(payload).hexdigest()

        await ArtifactManagementService.retire(artifact_id, user_id=DEVELOPMENT_USER_ID)

        async with session_factory() as session:
            artifact = await session.get(ArtifactFile, artifact_id)
            assert artifact is not None
            assert artifact.storage_status is StorageStatus.RETIRED
        with RustFSObjectStore(RustFSSettings()) as store:
            assert object_key is not None
            assert not store.exists(object_key)
        assert await ArtifactQueryService.get_artifact(artifact_id=artifact_id) is None
        with pytest.raises(ArtifactNotFoundError, match="artifact not found"):
            await ArtifactContentService.preview(artifact_id, max_bytes=4096)
        restored = await ArtifactUploadService.upload(
            payload=payload,
            filename="remove-me-again.txt",
            media_type="text/plain",
            artifact_kind=ArtifactKind.AUXILIARY,
            project_id=SYSTEM_PROJECT_ID,
            user_id=DEVELOPMENT_USER_ID,
        )
        assert restored.artifact_id == artifact_id
        assert restored.storage_status is StorageStatus.AVAILABLE
        restored_summary = await ArtifactQueryService.get_artifact(artifact_id=artifact_id)
        assert restored_summary is not None
        assert restored_summary.original_filename == "renamed-before-removal.txt"
        async with session_factory() as session:
            artifact = await session.get(ArtifactFile, artifact_id)
            assert artifact is not None
            object_key = artifact.object_key
        with RustFSObjectStore(RustFSSettings()) as store:
            assert store.exists(object_key)
    finally:
        async with session_factory() as session:
            artifact = await session.get(ArtifactFile, artifact_id)
            if artifact is not None:
                await session.delete(artifact)
                await session.commit()
        if object_key is not None:
            with RustFSObjectStore(RustFSSettings()) as store:
                store.delete(object_key)


@pytest.mark.asyncio
async def test_cross_project_uploads_share_object_until_last_reference_is_retired() -> None:
    payload = f"cross-project artifact {uuid4()}\n".encode()
    project_id = uuid4()
    user_id = uuid4()
    membership_id = uuid4()
    first_artifact_id = None
    second_artifact_id = None
    object_key = None

    async with session_factory() as session:
        session.add_all(
            [
                UserAccount(id=user_id, display_name="Cross-project uploader"),
                Project(
                    id=project_id,
                    organization_id=SYSTEM_ORGANIZATION_ID,
                    slug=f"shared-artifact-{project_id.hex}",
                    name="Shared artifact integration test",
                ),
                ProjectMembership(
                    id=membership_id,
                    project_id=project_id,
                    user_id=user_id,
                    role=ProjectRole.MANAGER,
                ),
            ]
        )
        await session.commit()

    try:
        first = await ArtifactUploadService.upload(
            payload=payload,
            filename="project-a.txt",
            media_type="text/plain",
            artifact_kind=ArtifactKind.AUXILIARY,
            project_id=SYSTEM_PROJECT_ID,
            user_id=DEVELOPMENT_USER_ID,
        )
        second = await ArtifactUploadService.upload(
            payload=payload,
            filename="project-b.txt",
            media_type="text/plain",
            artifact_kind=ArtifactKind.AUXILIARY,
            project_id=project_id,
            user_id=user_id,
        )
        first_artifact_id = first.artifact_id
        second_artifact_id = second.artifact_id
        assert first_artifact_id != second_artifact_id

        async with session_factory() as session:
            first_artifact = await session.get(ArtifactFile, first_artifact_id)
            second_artifact = await session.get(ArtifactFile, second_artifact_id)
            assert first_artifact is not None and second_artifact is not None
            assert first_artifact.project_id == SYSTEM_PROJECT_ID
            assert second_artifact.project_id == project_id
            assert first_artifact.created_by_user_id == DEVELOPMENT_USER_ID
            assert second_artifact.created_by_user_id == user_id
            assert first_artifact.object_key == second_artifact.object_key
            object_key = first_artifact.object_key

        await ArtifactManagementService.retire(
            first_artifact_id,
            user_id=DEVELOPMENT_USER_ID,
        )
        with RustFSObjectStore(RustFSSettings()) as store:
            assert object_key is not None
            assert store.exists(object_key)

        async with session_factory() as session:
            second_artifact = await session.get(ArtifactFile, second_artifact_id)
            assert second_artifact is not None
            assert second_artifact.project_id == project_id
            assert second_artifact.storage_status is StorageStatus.AVAILABLE

        await ArtifactManagementService.retire(second_artifact_id, user_id=user_id)
        with RustFSObjectStore(RustFSSettings()) as store:
            assert object_key is not None
            assert not store.exists(object_key)
    finally:
        async with session_factory() as session:
            for artifact_id in (first_artifact_id, second_artifact_id):
                if artifact_id is not None:
                    artifact = await session.get(ArtifactFile, artifact_id)
                    if artifact is not None:
                        await session.delete(artifact)
            membership = await session.get(ProjectMembership, membership_id)
            if membership is not None:
                await session.delete(membership)
            project = await session.get(Project, project_id)
            if project is not None:
                await session.delete(project)
            user = await session.get(UserAccount, user_id)
            if user is not None:
                await session.delete(user)
            await session.commit()
        if object_key is not None:
            with RustFSObjectStore(RustFSSettings()) as store:
                if store.exists(object_key):
                    store.delete(object_key)
