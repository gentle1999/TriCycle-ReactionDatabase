import os
from hashlib import sha256
from uuid import uuid4

import pytest

from tricycle_reaction_db.application.services import (
    ArtifactContentService,
    ArtifactNotFoundError,
    ArtifactQueryService,
    AuthenticatedPrincipal,
    iter_artifact_download,
)
from tricycle_reaction_db.application.services.authentication import (
    reset_current_principal,
    set_current_principal,
)
from tricycle_reaction_db.db.models import ArtifactFile
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    ArtifactVisibility,
    StorageStatus,
)
from tricycle_reaction_db.domain.identity import (
    DEVELOPMENT_IDENTITY_ISSUER,
    DEVELOPMENT_IDENTITY_SUBJECT,
    DEVELOPMENT_USER_ID,
    SYSTEM_PROJECT_ID,
)
from tricycle_reaction_db.storage.rustfs import RustFSObjectStore, RustFSSettings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.rustfs,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1"
        or os.getenv("TRICYCLE_RUN_RUSTFS_TESTS") != "1",
        reason="set database and RustFS integration flags to run artifact access tests",
    ),
]


@pytest.mark.asyncio
async def test_public_artifact_is_anonymous_and_project_artifact_requires_membership() -> None:
    payload = f"Gaussian artifact access {uuid4()}\n".encode()
    digest = sha256(payload).hexdigest()
    object_key = f"integration/access/{digest}.log"
    settings = RustFSSettings()
    artifact_id = None

    with RustFSObjectStore(settings) as store:
        store.ensure_bucket()
        stored = store.put_bytes(
            key=object_key,
            payload=payload,
            content_type="text/plain",
            metadata={"source": "artifact-access-test"},
        )

    try:
        async with session_factory() as session:
            artifact = ArtifactFile(
                project_id=SYSTEM_PROJECT_ID,
                created_by_user_id=DEVELOPMENT_USER_ID,
                visibility=ArtifactVisibility.PUBLIC,
                bucket=settings.bucket,
                object_key=object_key,
                content_sha256=digest,
                size_bytes=len(payload),
                original_filename="anonymous-public.log",
                media_type="text/plain",
                artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                storage_status=StorageStatus.AVAILABLE,
                etag=stored.etag,
            )
            session.add(artifact)
            await session.commit()
            await session.refresh(artifact)
            artifact_id = artifact.id
        assert artifact_id is not None

        public_page = await ArtifactQueryService.list_artifacts(limit=200, offset=0)
        assert artifact_id in {item.id for item in public_page.items}
        direct_page = await ArtifactQueryService.list_artifacts(
            artifact_id=artifact_id,
            limit=200,
            offset=0,
        )
        assert [item.id for item in direct_page.items] == [artifact_id]
        preview = await ArtifactContentService.preview(artifact_id, max_bytes=4096)
        assert preview.preview_text.encode() == payload
        download = await ArtifactContentService.download(artifact_id)
        assert b"".join(iter_artifact_download(download)) == payload

        async with session_factory() as session:
            persisted = await session.get(ArtifactFile, artifact_id)
            assert persisted is not None
            persisted.visibility = ArtifactVisibility.PROJECT
            await session.commit()

        anonymous_page = await ArtifactQueryService.list_artifacts(limit=200, offset=0)
        assert artifact_id not in {item.id for item in anonymous_page.items}
        with pytest.raises(ArtifactNotFoundError, match="artifact not found"):
            await ArtifactContentService.preview(artifact_id, max_bytes=4096)
        with pytest.raises(ArtifactNotFoundError, match="artifact not found"):
            await ArtifactContentService.download(artifact_id)

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
            authenticated_page = await ArtifactQueryService.list_artifacts(limit=200, offset=0)
        finally:
            reset_current_principal(token)
        assert artifact_id in {item.id for item in authenticated_page.items}
        member_preview = await ArtifactContentService.preview(
            artifact_id,
            max_bytes=4096,
            user_id=DEVELOPMENT_USER_ID,
        )
        assert member_preview.preview_text.encode() == payload
    finally:
        if artifact_id is not None:
            async with session_factory() as session:
                persisted = await session.get(ArtifactFile, artifact_id)
                if persisted is not None:
                    await session.delete(persisted)
                    await session.commit()
        with RustFSObjectStore(settings) as store:
            store.delete(object_key)
