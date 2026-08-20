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
from tricycle_reaction_db.db.models import ArtifactFile
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import ArtifactKind, ArtifactVisibility, StorageStatus
from tricycle_reaction_db.domain.identity import DEVELOPMENT_USER_ID, SYSTEM_PROJECT_ID
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
