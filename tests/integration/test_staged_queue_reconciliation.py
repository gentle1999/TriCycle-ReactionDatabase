"""A completed artifact must not lose its materialization to a stale queue item."""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from tricycle_reaction_db.application.dtos import ArtifactUploadResult
from tricycle_reaction_db.application.services import upload_batches as module
from tricycle_reaction_db.application.services.upload_batches import (
    PendingIngestionJob,
    UploadBatchService,
    _queue_ingestion_for_reparse,
)
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    ArtifactIngestion,
    ParseRevision,
    UploadBatch,
    UploadBatchItem,
)
from tricycle_reaction_db.db.session import engine
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactKind,
    ParseStatus,
    SourceFormat,
    StorageStatus,
    UploadBatchItemStatus,
    UploadBatchStatus,
)
from tricycle_reaction_db.domain.identity import DEVELOPMENT_USER_ID, SYSTEM_PROJECT_ID

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1", reason="database tests disabled"
    ),
]


@asynccontextmanager
async def queue_case(
    monkeypatch, *, status, copies=1, with_revision=True, request_after=False, committed=False
):
    async with engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(
            bind=connection,
            class_=AsyncSession,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        monkeypatch.setattr(module, "session_factory", factory)
        now = datetime.now(UTC)
        digest = sha256(str(uuid4()).encode()).hexdigest()
        async with factory() as session:
            artifact = ArtifactFile(
                project_id=SYSTEM_PROJECT_ID,
                created_by_user_id=DEVELOPMENT_USER_ID,
                bucket="test",
                object_key=f"queue-reconciliation/{digest}",
                content_sha256=digest,
                size_bytes=10,
                original_filename="source.log",
                media_type="text/plain",
                artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                storage_status=StorageStatus.AVAILABLE,
            )
            session.add(artifact)
            await session.flush()
            ingestion = ArtifactIngestion(
                artifact_file_id=artifact.id,
                parser_version="test",
                status=status,
                started_at=now - timedelta(minutes=2),
                completed_at=None
                if status is ArtifactIngestionStatus.PENDING
                else now - timedelta(minutes=1),
                processing_attempt_count=1,
                error_code="retained_error"
                if status in (ArtifactIngestionStatus.FAILED, ArtifactIngestionStatus.FILTERED)
                else None,
            )
            session.add(ingestion)
            revision = None
            if with_revision:
                revision = ParseRevision(
                    artifact_file_id=artifact.id,
                    revision_number=1,
                    export_schema_version="test",
                    parser_version="test",
                    parser_id="test",
                    molop_version="test",
                    rdkit_version="test",
                    parser_provenance={},
                    parser_provenance_hash=digest,
                    parser_config_hash=digest,
                    reconstruction_config_hash=digest,
                    source_format=SourceFormat.GAUSSIAN_LOG,
                    source_encoding="utf-8",
                    status=ParseStatus.SUCCEEDED,
                    record_sha256=digest,
                    started_at=now - timedelta(minutes=2),
                    completed_at=now - timedelta(minutes=1),
                )
                session.add(revision)
            batch = UploadBatch(
                project_id=SYSTEM_PROJECT_ID,
                created_by_user_id=DEVELOPMENT_USER_ID,
                artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                status=UploadBatchStatus.ACTIVE,
                total_count=copies,
                total_bytes=10 * copies,
                staged_count=copies,
                updated_at=now,
            )
            session.add(batch)
            await session.flush()
            items = []
            for position in range(copies):
                item = UploadBatchItem(
                    batch_id=batch.id,
                    client_file_id=uuid4(),
                    position=position,
                    original_filename="source.log",
                    relative_path="source.log",
                    size_bytes=10,
                    media_type="text/plain",
                    status=UploadBatchItemStatus.STAGED,
                    artifact_file_id=artifact.id,
                    metadata_json={"retained": True},
                    updated_at=now if request_after else now - timedelta(minutes=3),
                )
                session.add(item)
                items.append(item)
            await session.commit()
        if committed:
            await transaction.commit()
            factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
            monkeypatch.setattr(module, "session_factory", factory)
        try:
            yield factory, artifact, ingestion, batch, items, revision
        finally:
            if committed:
                async with factory() as session:
                    await session.exec(
                        delete(UploadBatchItem).where(col(UploadBatchItem.batch_id) == batch.id)
                    )
                    await session.exec(delete(UploadBatch).where(col(UploadBatch.id) == batch.id))
                    await session.exec(
                        delete(ParseRevision).where(
                            col(ParseRevision.artifact_file_id) == artifact.id
                        )
                    )
                    await session.exec(
                        delete(ArtifactIngestion).where(
                            col(ArtifactIngestion.artifact_file_id) == artifact.id
                        )
                    )
                    await session.exec(
                        delete(ArtifactFile).where(col(ArtifactFile.id) == artifact.id)
                    )
                    await session.commit()
            else:
                await transaction.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        ArtifactIngestionStatus.SUCCEEDED,
        ArtifactIngestionStatus.PARTIAL,
        ArtifactIngestionStatus.FAILED,
        ArtifactIngestionStatus.FILTERED,
    ],
)
async def test_completed_staged_work_is_acknowledged_without_reparsing(monkeypatch, status):
    successful = status in (ArtifactIngestionStatus.SUCCEEDED, ArtifactIngestionStatus.PARTIAL)
    async with queue_case(monkeypatch, status=status, copies=2, with_revision=successful) as case:
        factory, artifact, ingestion, batch, items, revision = case
        previous_completion = ingestion.completed_at
        # Startup acknowledges one bounded page and recounts the batch before
        # the normal refill completes its remaining alias.
        assert (
            await UploadBatchService.reconcile_completed_staged_items(limit=1, scan_all=True) == 1
        )
        async with factory() as session:
            page_batch = await session.get(UploadBatch, batch.id)
            assert page_batch.staged_count == 1
            assert page_batch.status is UploadBatchStatus.ACTIVE
        assert await UploadBatchService.claim_processing(limit=2) == []
        async with factory() as session:
            current = await session.get(ArtifactIngestion, ingestion.id)
            assert current.status is status and current.completed_at == previous_completion
            assert current.processing_attempt_count == 1 and current.worker_lease_id is None
            current_batch = await session.get(UploadBatch, batch.id)
            assert current_batch.status is UploadBatchStatus.COMPLETED
            assert current_batch.staged_count == current_batch.processing_count == 0
            assert current_batch.succeeded_count == (2 if successful else 0)
            assert current_batch.failed_count == (0 if successful else 2)
            for item in items:
                current_item = await session.get(UploadBatchItem, item.id)
                assert current_item.status is (
                    UploadBatchItemStatus.SUCCEEDED if successful else UploadBatchItemStatus.FAILED
                )
                assert current_item.parse_status == status.value
                assert current_item.parse_revision_id == (revision.id if revision else None)
                assert current_item.metadata_json["retained"]
            if revision:
                assert await session.get(ParseRevision, revision.id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["new_request", "explicit_reparse", "missing_revision"])
async def test_a_completed_old_result_does_not_suppress_required_reparse(monkeypatch, scenario):
    async with queue_case(
        monkeypatch,
        status=ArtifactIngestionStatus.SUCCEEDED,
        with_revision=scenario != "missing_revision",
        request_after=scenario == "new_request",
    ) as case:
        factory, artifact, ingestion, batch, items, revision = case
        if scenario == "explicit_reparse":
            async with factory() as session:
                current = await session.get(ArtifactIngestion, ingestion.id)
                _queue_ingestion_for_reparse(current, queued_at=datetime.now(UTC))
                session.add(current)
                await session.commit()
        assert await UploadBatchService.reconcile_completed_staged_items(limit=512) == 0
        jobs = await UploadBatchService.claim_processing(limit=1)
        assert len(jobs) == 1 and jobs[0].artifact_file_id == artifact.id
        async with factory() as session:
            current = await session.get(ArtifactIngestion, ingestion.id)
            assert current.status is ArtifactIngestionStatus.PROCESSING
            assert current.worker_lease_id == jobs[0].lease_id


@pytest.mark.asyncio
async def test_alias_items_share_one_parse_and_never_steal_an_active_lease(monkeypatch):
    async with queue_case(monkeypatch, status=ArtifactIngestionStatus.PENDING, copies=2) as case:
        factory, artifact, ingestion, batch, items, revision = case
        jobs = await UploadBatchService.claim_processing(limit=2)
        assert len(jobs) == 1
        assert await UploadBatchService.claim_processing(limit=2) == []
        async with factory() as session:
            current = await session.get(ArtifactIngestion, ingestion.id)
            assert (
                current.worker_lease_id == jobs[0].lease_id
                and current.processing_attempt_count == 2
            )
            current.status = ArtifactIngestionStatus.SUCCEEDED
            current.completed_at = datetime.now(UTC)
            session.add(current)
            await session.commit()
        result = ArtifactUploadResult(
            artifact_id=artifact.id,
            artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
            storage_status=StorageStatus.AVAILABLE,
            ingestion_id=ingestion.id,
            parse_revision_id=revision.id,
            ingestion_status=ArtifactIngestionStatus.SUCCEEDED,
            inferred_reaction_count=0,
            inferences=[],
        )
        assert await UploadBatchService.finish_processing_batch(jobs, {artifact.id: result}) == 1
        assert await UploadBatchService.claim_processing(limit=2) == []
        async with factory() as session:
            current_batch = await session.get(UploadBatch, batch.id)
            assert current_batch.succeeded_count == 2 and current_batch.failed_count == 0
            assert current_batch.status is UploadBatchStatus.COMPLETED
            assert (
                await session.get(ArtifactIngestion, ingestion.id)
            ).processing_attempt_count == 2


@pytest.mark.asyncio
async def test_explicit_reparse_after_acknowledgment_remains_pending(monkeypatch):
    async with queue_case(monkeypatch, status=ArtifactIngestionStatus.SUCCEEDED) as case:
        factory, artifact, ingestion, batch, items, revision = case
        assert await UploadBatchService.reconcile_completed_staged_items() == 1
        submission = await UploadBatchService.enqueue_reparse(
            artifact.id, user_id=DEVELOPMENT_USER_ID
        )
        assert submission.batch.id != batch.id
        async with factory() as session:
            current = await session.get(ArtifactIngestion, ingestion.id)
            assert current.status is ArtifactIngestionStatus.PENDING
            assert current.completed_at is None
            assert await session.get(ParseRevision, revision.id) is not None
        jobs = await UploadBatchService.claim_processing(limit=1)
        assert len(jobs) == 1 and jobs[0].batch_id == submission.batch.id


@pytest.mark.asyncio
@pytest.mark.parametrize("locked_table", ["ingestion", "queue_item"])
async def test_upload_heartbeat_skips_persistence_locks_and_renews_after_release(
    monkeypatch, locked_table
):
    async with queue_case(
        monkeypatch, status=ArtifactIngestionStatus.PENDING, committed=True
    ) as case:
        factory, artifact, ingestion, batch, items, revision = case
        jobs = await UploadBatchService.claim_processing(limit=1)
        async with factory() as blocker:
            if locked_table == "ingestion":
                statement = select(ArtifactIngestion).where(
                    col(ArtifactIngestion.id) == ingestion.id
                )
            else:
                statement = select(UploadBatchItem).where(col(UploadBatchItem.id) == items[0].id)
            await blocker.exec(statement.with_for_update())
            assert await asyncio.wait_for(
                UploadBatchService.renew_processing_leases(jobs), timeout=1
            ) == (1 if locked_table == "ingestion" else 0)
            async with factory() as session:
                locked = await session.get(
                    ArtifactIngestion if locked_table == "ingestion" else UploadBatchItem,
                    ingestion.id if locked_table == "ingestion" else items[0].id,
                )
                assert locked.worker_lease_expires_at == jobs[0].lease_expires_at
            await blocker.rollback()
        assert await UploadBatchService.renew_processing_leases(jobs) == 1
        async with factory() as session:
            current = await session.get(ArtifactIngestion, ingestion.id)
            assert current.worker_lease_expires_at > jobs[0].lease_expires_at
            assert current.worker_lease_id == jobs[0].lease_id
            current.status = ArtifactIngestionStatus.SUCCEEDED
            current.completed_at = datetime.now(UTC)
            current.worker_lease_id = None
            current.worker_lease_expires_at = None
            session.add(current)
            await session.commit()
        await UploadBatchService.renew_processing_leases(jobs)
        async with factory() as session:
            current = await session.get(ArtifactIngestion, ingestion.id)
            assert current.status is ArtifactIngestionStatus.SUCCEEDED
            assert current.worker_lease_id is current.worker_lease_expires_at is None


@pytest.mark.asyncio
async def test_compatibility_heartbeat_does_not_wait_for_persistence(monkeypatch):
    async with queue_case(
        monkeypatch, status=ArtifactIngestionStatus.PENDING, committed=True
    ) as case:
        factory, artifact, ingestion, batch, items, revision = case
        job = (await UploadBatchService.claim_processing(limit=1))[0]
        pending = PendingIngestionJob(
            project_id=job.project_id,
            artifact_file_id=job.artifact_file_id,
            ingestion_id=ingestion.id,
            user_id=job.user_id,
            lease_id=job.lease_id,
            lease_expires_at=job.lease_expires_at,
        )
        async with factory() as blocker:
            await blocker.exec(
                select(ArtifactIngestion)
                .where(col(ArtifactIngestion.id) == ingestion.id)
                .with_for_update()
            )
            assert (
                await asyncio.wait_for(
                    UploadBatchService.renew_pending_ingestion_leases([pending]), timeout=1
                )
                == 0
            )
            await blocker.rollback()
        assert await UploadBatchService.renew_pending_ingestion_leases([pending]) == 1
