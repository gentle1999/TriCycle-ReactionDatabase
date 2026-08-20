import asyncio
import os
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session

from tricycle_reaction_db.application.services.transition_state_uploads import (
    _compensate_upload,
    _RetiredArtifactReservation,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import ArtifactFile, ArtifactIngestion
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactKind,
    StorageStatus,
)
from tricycle_reaction_db.domain.identity import DEVELOPMENT_USER_ID, SYSTEM_PROJECT_ID
from tricycle_reaction_db.storage.rustfs import (
    RustFSObjectStore,
    RustFSSettings,
    time_partitioned_content_addressed_key,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.rustfs,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1"
        or os.getenv("TRICYCLE_RUN_RUSTFS_TESTS") != "1",
        reason="set database and RustFS integration flags to run upload compensation tests",
    ),
]


def _artifact(
    *,
    settings: RustFSSettings,
    object_key: str,
    payload: bytes,
    status: StorageStatus,
) -> ArtifactFile:
    return ArtifactFile(
        project_id=SYSTEM_PROJECT_ID,
        created_by_user_id=DEVELOPMENT_USER_ID,
        bucket=settings.bucket,
        object_key=object_key,
        content_sha256=sha256(payload).hexdigest(),
        size_bytes=len(payload),
        original_filename="compensation.log",
        media_type="text/plain",
        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
        storage_status=status,
    )


@pytest.mark.parametrize(
    (
        "initial_status",
        "has_history",
        "expected_status",
        "expected_object_exists",
    ),
    [
        (StorageStatus.PENDING, False, None, False),
        (StorageStatus.PENDING, True, None, False),
        (StorageStatus.AVAILABLE, False, StorageStatus.AVAILABLE, True),
    ],
)
def test_upload_compensation_only_deletes_unavailable_reserved_objects(
    initial_status: StorageStatus,
    has_history: bool,
    expected_status: StorageStatus | None,
    expected_object_exists: bool,
) -> None:
    settings = RustFSSettings()
    payload = f"upload-compensation-{uuid4()}".encode()
    digest = sha256(payload).hexdigest()
    object_key = time_partitioned_content_addressed_key(
        payload,
        uploaded_at=datetime.now(UTC),
    )
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    artifact_id = None
    try:
        with RustFSObjectStore(settings) as store:
            store.ensure_bucket()
            store.put_bytes(key=object_key, payload=payload, content_type="text/plain")
        with Session(engine) as session:
            artifact = _artifact(
                settings=settings,
                object_key=object_key,
                payload=payload,
                status=initial_status,
            )
            session.add(artifact)
            session.commit()
            session.refresh(artifact)
            artifact_id = artifact.id
            if has_history:
                session.add(
                    ArtifactIngestion(
                        artifact_file_id=artifact.id,
                        status=ArtifactIngestionStatus.FAILED,
                        parser_version="test",
                        started_at=datetime.now(UTC),
                        completed_at=datetime.now(UTC),
                    )
                )
                session.commit()
        assert artifact_id is not None

        asyncio.run(
            _compensate_upload(
                settings=settings,
                artifact_id=artifact_id,
                object_key=object_key,
                content_sha256=digest,
            )
        )

        with Session(engine) as session:
            artifact = session.get(ArtifactFile, artifact_id)
            if expected_status is None:
                assert artifact is None
            else:
                assert artifact is not None
                assert artifact.storage_status is expected_status
        with RustFSObjectStore(settings) as store:
            assert store.exists(object_key) is expected_object_exists
    finally:
        with RustFSObjectStore(settings) as store:
            if store.exists(object_key):
                store.delete(object_key)
        if artifact_id is not None:
            with Session(engine) as session:
                artifact = session.get(ArtifactFile, artifact_id)
                if artifact is not None:
                    session.delete(artifact)
                    session.commit()
        engine.dispose()


def test_upload_compensation_restores_retired_tombstone() -> None:
    settings = RustFSSettings()
    payload = f"retired-upload-compensation-{uuid4()}".encode()
    digest = sha256(payload).hexdigest()
    pending_key = time_partitioned_content_addressed_key(
        payload,
        uploaded_at=datetime.now(UTC),
    )
    retired_key = f"retired/sha256/{digest[:2]}/{digest}"
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    artifact_id = None
    try:
        with RustFSObjectStore(settings) as store:
            store.ensure_bucket()
            store.put_bytes(key=pending_key, payload=payload, content_type="text/plain")
        with Session(engine) as session:
            artifact = _artifact(
                settings=settings,
                object_key=pending_key,
                payload=payload,
                status=StorageStatus.PENDING,
            )
            session.add(artifact)
            session.commit()
            session.refresh(artifact)
            artifact_id = artifact.id
        assert artifact_id is not None

        asyncio.run(
            _compensate_upload(
                settings=settings,
                artifact_id=artifact_id,
                object_key=pending_key,
                content_sha256=digest,
                retired_reservation=_RetiredArtifactReservation(
                    bucket=settings.bucket,
                    object_key=retired_key,
                    version_id="retired-version-id",
                    etag="retired-etag",
                    storage_verified_at=None,
                ),
            )
        )

        with Session(engine) as session:
            artifact = session.get(ArtifactFile, artifact_id)
            assert artifact is not None
            assert artifact.storage_status is StorageStatus.RETIRED
            assert artifact.object_key == retired_key
            assert artifact.version_id == "retired-version-id"
            assert artifact.etag == "retired-etag"
        with RustFSObjectStore(settings) as store:
            assert not store.exists(pending_key)
    finally:
        with RustFSObjectStore(settings) as store:
            if store.exists(pending_key):
                store.delete(pending_key)
        if artifact_id is not None:
            with Session(engine) as session:
                artifact = session.get(ArtifactFile, artifact_id)
                if artifact is not None:
                    session.delete(artifact)
                    session.commit()
        engine.dispose()
