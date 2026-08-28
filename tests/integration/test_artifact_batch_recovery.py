import os
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

from tricycle_reaction_db.application.services import artifact_uploads as uploads
from tricycle_reaction_db.application.services.authorization import AuthorizationService
from tricycle_reaction_db.db.models import ArtifactFile, ArtifactIngestion
from tricycle_reaction_db.db.session import engine
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactKind,
    ArtifactVisibility,
    StorageStatus,
)
from tricycle_reaction_db.domain.identity import DEVELOPMENT_USER_ID, SYSTEM_PROJECT_ID
from tricycle_reaction_db.storage.rustfs import RustFSSettings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run artifact recovery tests",
    ),
]


@pytest.mark.asyncio
async def test_aborted_batch_recovery_salvages_stored_object_and_marks_ingestion_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"batch recovery fixture"
    digest = sha256(content).hexdigest()
    object_key = f"uploads/2099/01/01/00/sha256/{digest[:2]}/{digest}"
    started_at = datetime.now(UTC)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(
            bind=connection,
            class_=AsyncSession,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        monkeypatch.setattr(uploads, "session_factory", factory)
        try:
            async with factory() as session:
                artifact = ArtifactFile(
                    project_id=SYSTEM_PROJECT_ID,
                    created_by_user_id=DEVELOPMENT_USER_ID,
                    visibility=ArtifactVisibility.PROJECT,
                    bucket=RustFSSettings().bucket,
                    object_key=object_key,
                    content_sha256=digest,
                    size_bytes=len(content),
                    original_filename="recovery.log",
                    media_type="text/plain",
                    artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                    storage_status=StorageStatus.PENDING,
                )
                session.add(artifact)
                await session.flush()
                assert artifact.id is not None
                ingestion = ArtifactIngestion(
                    artifact_file_id=artifact.id,
                    parser_version=uploads.MOLOP_VERSION,
                    started_at=started_at,
                )
                session.add(ingestion)
                await session.flush()
                assert ingestion.id is not None
                await session.commit()

            reservation = uploads._PreparedCalculationUpload(
                settings=RustFSSettings(),
                artifact_id=artifact.id,
                object_key=object_key,
                ingestion_id=ingestion.id,
                started_at=started_at,
                source=content,
                size_bytes=len(content),
                media_type="text/plain",
                content_sha256=digest,
            )
            stored = SimpleNamespace(
                size=len(content),
                sha256=digest,
                version_id="version-1",
                etag="etag-1",
                last_modified=datetime.now(UTC),
            )
            await uploads._recover_aborted_batch(
                prepared={0: reservation},
                stored={0: stored},
                error=RuntimeError("persistence failed"),
            )

            async with factory() as session:
                recovered_artifact = await session.get(ArtifactFile, artifact.id)
                recovered_ingestion = await session.get(ArtifactIngestion, ingestion.id)
                assert recovered_artifact is not None
                assert recovered_artifact.storage_status is StorageStatus.AVAILABLE
                assert recovered_artifact.version_id == "version-1"
                assert recovered_ingestion is not None
                assert recovered_ingestion.status is ArtifactIngestionStatus.FAILED
                assert recovered_ingestion.error_code == "artifact_batch_failed"
                assert recovered_ingestion.completed_at is not None
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_upload_batch_persistence_failure_recovers_pending_ingestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"production recovery fixture"
    digest = sha256(content).hexdigest()
    object_key = f"uploads/2099/01/01/00/sha256/{digest[:2]}/{digest}"
    started_at = datetime.now(UTC)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(
            bind=connection,
            class_=AsyncSession,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        monkeypatch.setattr(uploads, "session_factory", factory)
        monkeypatch.setattr(AuthorizationService, "require_project_permission", _allow_upload)
        try:
            async with factory() as session:
                artifact = ArtifactFile(
                    project_id=SYSTEM_PROJECT_ID,
                    created_by_user_id=DEVELOPMENT_USER_ID,
                    visibility=ArtifactVisibility.PROJECT,
                    bucket=RustFSSettings().bucket,
                    object_key=object_key,
                    content_sha256=digest,
                    size_bytes=len(content),
                    original_filename="production-recovery.log",
                    media_type="text/plain",
                    artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                    storage_status=StorageStatus.PENDING,
                )
                session.add(artifact)
                await session.flush()
                assert artifact.id is not None
                ingestion = ArtifactIngestion(
                    artifact_file_id=artifact.id,
                    parser_version=uploads.MOLOP_VERSION,
                    started_at=started_at,
                )
                session.add(ingestion)
                await session.flush()
                assert ingestion.id is not None
                await session.commit()

            reservation = uploads._PreparedCalculationUpload(
                settings=RustFSSettings(),
                artifact_id=artifact.id,
                object_key=object_key,
                ingestion_id=ingestion.id,
                started_at=started_at,
                source=content,
                size_bytes=len(content),
                media_type="text/plain",
                content_sha256=digest,
                needs_storage=False,
            )

            async def fake_prepare_batch(
                _cls: object,
                **_: object,
            ) -> tuple[dict[int, uploads._PreparedCalculationUpload], dict[int, object]]:
                return {0: reservation}, {}

            monkeypatch.setattr(
                uploads.ArtifactUploadService,
                "_prepare_upload_batch",
                classmethod(fake_prepare_batch),
            )
            monkeypatch.setattr(uploads, "_get_storage_process_pool", lambda *_: object())

            parsed = uploads._ParsedArtifact(
                chem_file=object(),
                frame_records=(),
                source_frame_count=1,
                source_format="test",
                source_compression=None,
                inferences=(),
                record_sha256=None,
                artifact_sha256=digest,
            )

            async def fake_parse(*_: object, **__: object) -> uploads._ParsedArtifact:
                return parsed

            def fail_persist(*_: object, **__: object) -> object:
                raise RuntimeError("persistence failed")

            monkeypatch.setattr(uploads, "_run_molop_file_pipeline", fake_parse)
            monkeypatch.setattr(uploads, "_run_persist_parsed_artifact", fail_persist)

            with pytest.raises(RuntimeError, match="persistence failed"):
                await uploads.ArtifactUploadService.upload_batch(
                    files=[
                        uploads.ArtifactUploadPayload(
                            "production-recovery.log",
                            "text/plain",
                            content,
                        )
                    ],
                    artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                    project_id=SYSTEM_PROJECT_ID,
                    user_id=DEVELOPMENT_USER_ID,
                )

            async with factory() as session:
                recovered_artifact = await session.get(ArtifactFile, artifact.id)
                recovered_ingestion = await session.get(ArtifactIngestion, ingestion.id)
                assert recovered_artifact is not None
                assert recovered_artifact.storage_status is StorageStatus.PENDING
                assert recovered_ingestion is not None
                assert recovered_ingestion.status is ArtifactIngestionStatus.FAILED
                assert recovered_ingestion.error_code == "artifact_batch_failed"
        finally:
            await transaction.rollback()


async def _allow_upload(*_: object) -> None:
    return None
