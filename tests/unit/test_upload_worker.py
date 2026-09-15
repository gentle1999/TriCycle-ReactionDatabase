import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID

import pytest

from tricycle_reaction_db.application.services import artifact_uploads as upload_module
from tricycle_reaction_db.application.services.artifact_uploads import ArtifactUploadService
from tricycle_reaction_db.application.services.upload_batches import (
    PendingIngestionJob,
    UploadBatchService,
    UploadProcessingJob,
)
from tricycle_reaction_db.core.config import Settings
from tricycle_reaction_db.dev.upload_worker import UploadBatchWorker
from tricycle_reaction_db.domain.enums import ArtifactKind, StorageStatus

USER_ID = UUID("00000000-0000-7000-8000-000000000001")
PROJECT_A = UUID("00000000-0000-7000-0000-000000000001")
PROJECT_B = UUID("00000000-0000-7000-0000-000000000002")


def _job(
    *,
    project_id: UUID,
    batch_id: int,
    item_id: int,
    artifact_id: int,
) -> UploadProcessingJob:
    return UploadProcessingJob(
        project_id=project_id,
        batch_id=UUID(f"00000000-0000-7000-0000-{batch_id:012d}"),
        item_id=UUID(f"00000000-0000-7000-0001-{item_id:012d}"),
        client_file_id=UUID(f"00000000-0000-7000-0002-{item_id:012d}"),
        artifact_file_id=UUID(f"00000000-0000-7000-0003-{artifact_id:012d}"),
        user_id=USER_ID,
        lease_id=UUID(f"00000000-0000-7000-0004-{item_id:012d}"),
    )


def _pending_job(*, project_id: UUID, ingestion_id: int, artifact_id: int) -> PendingIngestionJob:
    return PendingIngestionJob(
        project_id=project_id,
        ingestion_id=UUID(f"00000000-0000-7000-0000-{ingestion_id:012d}"),
        artifact_file_id=UUID(f"00000000-0000-7000-0003-{artifact_id:012d}"),
        user_id=USER_ID,
        lease_id=UUID(f"00000000-0000-7000-0004-{ingestion_id:012d}"),
    )


@pytest.mark.asyncio
async def test_worker_merges_compatibility_ingestions_into_one_project_microbatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = [
        _pending_job(project_id=PROJECT_A, ingestion_id=1, artifact_id=11),
        _pending_job(project_id=PROJECT_A, ingestion_id=2, artifact_id=12),
    ]
    reparse_calls: list[tuple[tuple[UUID, ...], UUID, bool]] = []
    failed: list[tuple[UUID, UUID, Exception]] = []

    async def reparse_batch(
        *,
        artifact_ids: list[UUID],
        user_id: UUID,
        force_reparse: bool,
    ) -> dict[UUID, object]:
        reparse_calls.append((tuple(artifact_ids), user_id, force_reparse))
        return {artifact_id: object() for artifact_id in artifact_ids}

    async def fail_pending_ingestion(
        *,
        ingestion_id: UUID,
        lease_id: UUID,
        error: Exception,
    ) -> None:
        failed.append((ingestion_id, lease_id, error))

    monkeypatch.setattr(ArtifactUploadService, "reparse_batch", reparse_batch)
    monkeypatch.setattr(
        ArtifactUploadService,
        "fail_pending_ingestion",
        fail_pending_ingestion,
    )

    await UploadBatchWorker()._process_pending_jobs(jobs)

    assert reparse_calls == [((jobs[0].artifact_file_id, jobs[1].artifact_file_id), USER_ID, True)]
    assert failed == []


@pytest.mark.asyncio
async def test_worker_merges_single_file_batches_into_one_project_microbatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = [
        _job(project_id=PROJECT_A, batch_id=1, item_id=1, artifact_id=11),
        _job(project_id=PROJECT_A, batch_id=2, item_id=2, artifact_id=12),
    ]
    reparse_calls: list[tuple[tuple[UUID, ...], UUID, bool]] = []
    finished: list[tuple[UUID, object | None, Exception | None]] = []

    async def reparse_batch(
        *,
        artifact_ids: list[UUID],
        user_id: UUID,
        force_reparse: bool,
    ) -> dict[UUID, object]:
        reparse_calls.append((tuple(artifact_ids), user_id, force_reparse))
        return {artifact_id: object() for artifact_id in artifact_ids}

    async def finish_processing(
        job: UploadProcessingJob,
        *,
        result: object | None = None,
        error: Exception | None = None,
    ) -> None:
        finished.append((job.artifact_file_id, result, error))

    monkeypatch.setattr(ArtifactUploadService, "reparse_batch", reparse_batch)
    monkeypatch.setattr(UploadBatchService, "finish_processing", finish_processing)

    await UploadBatchWorker()._process_jobs(jobs)

    assert reparse_calls == [((jobs[0].artifact_file_id, jobs[1].artifact_file_id), USER_ID, True)]
    assert [artifact_id for artifact_id, _result, _error in finished] == [
        jobs[0].artifact_file_id,
        jobs[1].artifact_file_id,
    ]
    assert all(error is None for _artifact_id, _result, error in finished)


@pytest.mark.asyncio
async def test_worker_processes_project_microbatches_sequentially(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = [
        _job(project_id=PROJECT_A, batch_id=3, item_id=3, artifact_id=13),
        _job(project_id=PROJECT_B, batch_id=4, item_id=4, artifact_id=14),
    ]
    active_calls = 0
    maximum_active_calls = 0
    call_projects: list[UUID] = []

    async def reparse_batch(
        *,
        artifact_ids: list[UUID],
        user_id: UUID,
        force_reparse: bool,
    ) -> dict[UUID, object]:
        nonlocal active_calls, maximum_active_calls
        assert user_id == USER_ID
        assert force_reparse is True
        active_calls += 1
        maximum_active_calls = max(maximum_active_calls, active_calls)
        call_projects.append(PROJECT_A if artifact_ids == [jobs[0].artifact_file_id] else PROJECT_B)
        await asyncio.sleep(0)
        active_calls -= 1
        return {artifact_ids[0]: object()}

    async def finish_processing(
        _job: UploadProcessingJob,
        *,
        result: object | None = None,
        error: Exception | None = None,
    ) -> None:
        assert result is not None
        assert error is None

    monkeypatch.setattr(ArtifactUploadService, "reparse_batch", reparse_batch)
    monkeypatch.setattr(UploadBatchService, "finish_processing", finish_processing)

    await UploadBatchWorker()._process_jobs(jobs)

    assert call_projects == [PROJECT_A, PROJECT_B]
    assert maximum_active_calls == 1


@pytest.mark.asyncio
async def test_reparse_batch_uses_fixed_persistence_microbatch_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"payload"
    artifact = SimpleNamespace(
        id=UUID("00000000-0000-7000-0000-000000000011"),
        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
        storage_status=StorageStatus.AVAILABLE,
        project_id=PROJECT_A,
        size_bytes=len(payload),
        original_filename="calculation.log",
        media_type="text/plain",
        source_relative_path="calculation.log",
        content_sha256=sha256(payload).hexdigest(),
        bucket="artifacts",
        object_key="uploads/calculation.log",
    )

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def exec(self, _statement: object) -> SimpleNamespace:
            return SimpleNamespace(all=lambda: [artifact])

    @asynccontextmanager
    async def fake_session_factory() -> object:
        yield FakeSession()

    persistence_boundaries: list[int] = []

    async def fake_upload_batch(**kwargs: object) -> SimpleNamespace:
        persistence_boundaries.append(int(kwargs["persistence_batch_files"]))
        files = kwargs["files"]
        return SimpleNamespace(
            total_count=len(files),
            succeeded_count=len(files),
            failed_count=0,
            timings_ms={},
            items=[
                SimpleNamespace(result=object(), error_code=None, error_message=None) for _ in files
            ],
        )

    monkeypatch.setattr(
        upload_module,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            max_upload_bytes=1024,
            max_batch_files=64,
            max_batch_bytes=1024,
        ),
    )
    monkeypatch.setattr(upload_module, "session_factory", fake_session_factory)
    monkeypatch.setattr(
        upload_module,
        "_rustfs_download_submission_slots",
        lambda: asyncio.Semaphore(1),
    )
    monkeypatch.setattr(ArtifactUploadService, "_load_payload", staticmethod(lambda *_: payload))
    monkeypatch.setattr(ArtifactUploadService, "upload_batch", fake_upload_batch)

    results = await ArtifactUploadService.reparse_batch(
        artifact_ids=[artifact.id],
        user_id=USER_ID,
        force_reparse=True,
        previous_results_cleared=True,
    )

    assert persistence_boundaries == [upload_module.PERSISTENCE_PRELOAD_BATCH_SIZE]
    assert artifact.id in results
