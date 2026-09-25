import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID

import pytest

from tricycle_reaction_db.application.services import artifact_uploads as upload_module
from tricycle_reaction_db.application.services.artifact_upload_types import ParsedArtifactTask
from tricycle_reaction_db.application.services.artifact_uploads import ArtifactUploadService
from tricycle_reaction_db.application.services.upload_batches import (
    UploadBatchService,
    UploadProcessingJob,
)
from tricycle_reaction_db.core.config import Settings
from tricycle_reaction_db.dev import upload_worker as worker_module
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
    source_atom_order_authoritative: bool = False,
) -> UploadProcessingJob:
    return UploadProcessingJob(
        project_id=project_id,
        batch_id=UUID(f"00000000-0000-7000-0000-{batch_id:012d}"),
        item_id=UUID(f"00000000-0000-7000-0001-{item_id:012d}"),
        client_file_id=UUID(f"00000000-0000-7000-0002-{item_id:012d}"),
        artifact_file_id=UUID(f"00000000-0000-7000-0003-{artifact_id:012d}"),
        user_id=USER_ID,
        lease_id=UUID(f"00000000-0000-7000-0004-{item_id:012d}"),
        source_atom_order_authoritative=source_atom_order_authoritative,
    )


@pytest.mark.asyncio
async def test_worker_flushes_one_statistics_refresh_for_coalesced_projects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[set[UUID], str]] = []

    async def refresh_project_statistics(
        project_ids: tuple[UUID, ...],
        *,
        reason: str,
    ) -> bool:
        calls.append((set(project_ids), reason))
        return True

    monkeypatch.setattr(worker_module, "refresh_project_statistics", refresh_project_statistics)
    worker = UploadBatchWorker()
    worker._mark_statistics_dirty((PROJECT_A, PROJECT_B, PROJECT_A))

    await worker._flush_statistics()
    await worker._flush_statistics()

    assert calls == [({PROJECT_A, PROJECT_B}, "upload-worker-queue-drained")]


@pytest.mark.asyncio
async def test_streaming_worker_refills_parser_dispatcher_and_flushes_one_tail_microbatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = [
        _job(
            project_id=PROJECT_A,
            batch_id=10,
            item_id=10,
            artifact_id=101,
            source_atom_order_authoritative=True,
        ),
        _job(
            project_id=PROJECT_A,
            batch_id=11,
            item_id=11,
            artifact_id=102,
            source_atom_order_authoritative=True,
        ),
        _job(project_id=PROJECT_A, batch_id=12, item_id=12, artifact_id=103),
        _job(project_id=PROJECT_A, batch_id=13, item_id=13, artifact_id=104),
    ]
    claim_limits: list[int] = []
    clear_calls: list[list[UUID]] = []
    parsed_calls: list[UUID] = []
    persistence_calls: list[list[UUID]] = []
    authority_calls: list[bool] = []
    finalized_calls: list[list[UUID]] = []
    active_during_clear: list[set[UUID]] = []
    clearing_artifact_ids: set[UUID] = set()
    clear_observed_by_heartbeat = asyncio.Event()
    claim_offset = 0

    async def claim_stream_jobs(*, limit: int, prefer_processing: bool) -> list[object]:
        del prefer_processing
        nonlocal claim_offset
        claim_limits.append(limit)
        if claim_offset >= len(jobs):
            return []
        claimed = jobs[claim_offset : claim_offset + min(limit, 2)]
        claim_offset += len(claimed)
        return claimed

    async def clear_claimed_parse_state(
        claimed: list[object],
        *,
        project_write_locks: dict[UUID, asyncio.Lock],
    ) -> dict[UUID, Exception]:
        del project_write_locks
        claimed_ids = {job.artifact_file_id for job in claimed}  # type: ignore[attr-defined]
        clear_observed_by_heartbeat.clear()
        clearing_artifact_ids.update(claimed_ids)
        clear_calls.append(list(claimed_ids))
        await asyncio.wait_for(clear_observed_by_heartbeat.wait(), timeout=1)
        clearing_artifact_ids.clear()
        return {}

    async def observe_lease_jobs(
        active_jobs: dict[UUID, object],
        finished: asyncio.Event,
    ) -> None:
        while not finished.is_set():
            await asyncio.sleep(0)
            if clearing_artifact_ids:
                active_during_clear.append(set(active_jobs).intersection(clearing_artifact_ids))
                clear_observed_by_heartbeat.set()

    async def parse_staged_artifact(artifact_id: UUID) -> ParsedArtifactTask:
        parsed_calls.append(artifact_id)
        return ParsedArtifactTask(
            artifact_id=artifact_id,
            started_at=datetime.now(UTC),
            parsed=SimpleNamespace(frame_records=(), source_frame_count=1),  # type: ignore[arg-type]
        )

    async def persist_parsed_microbatch(
        tasks: list[ParsedArtifactTask],
        *,
        project_id: UUID,
        user_id: UUID,
        worker_lease_by_artifact_id: object,
        defer_thermodynamic_refresh: bool,
        source_atom_order_authoritative: bool,
    ) -> dict[UUID, object]:
        assert project_id == PROJECT_A
        assert user_id == USER_ID
        assert worker_lease_by_artifact_id
        assert defer_thermodynamic_refresh is True
        artifact_ids = [task.artifact_id for task in tasks]
        persistence_calls.append(artifact_ids)
        authority_calls.append(source_atom_order_authoritative)
        return {artifact_id: object() for artifact_id in artifact_ids}

    async def finish_processing_batch(
        claimed: list[UploadProcessingJob],
        results: dict[UUID, object],
    ) -> int:
        finalized_calls.append([job.artifact_file_id for job in claimed])
        assert all(job.artifact_file_id in results for job in claimed)
        return len(claimed)

    monkeypatch.setattr(worker_module, "molop_process_worker_count", lambda: 2)
    monkeypatch.setattr(
        UploadBatchWorker,
        "_renew_stream_leases",
        staticmethod(observe_lease_jobs),
    )
    monkeypatch.setattr(UploadBatchWorker, "_stream_prefetch_limit", staticmethod(lambda: 4))
    monkeypatch.setattr(
        UploadBatchWorker,
        "_claim_stream_jobs",
        staticmethod(claim_stream_jobs),
    )
    monkeypatch.setattr(
        UploadBatchWorker,
        "_clear_claimed_parse_state",
        staticmethod(clear_claimed_parse_state),
    )
    monkeypatch.setattr(ArtifactUploadService, "parse_staged_artifact", parse_staged_artifact)
    monkeypatch.setattr(
        ArtifactUploadService,
        "persist_parsed_microbatch",
        persist_parsed_microbatch,
    )
    monkeypatch.setattr(UploadBatchService, "finish_processing_batch", finish_processing_batch)

    had_work = await UploadBatchWorker()._run_streaming_cycle()

    assert had_work is True
    assert claim_limits == [2, 2, 2]
    assert len(clear_calls) == 2
    assert active_during_clear
    assert all(not active_ids for active_ids in active_during_clear)
    assert parsed_calls == [job.artifact_file_id for job in jobs]
    assert persistence_calls == [
        [job.artifact_file_id for job in jobs[:2]],
        [job.artifact_file_id for job in jobs[2:]],
    ]
    assert authority_calls == [True, False]
    assert finalized_calls == [
        [job.artifact_file_id for job in jobs[:2]],
        [job.artifact_file_id for job in jobs[2:]],
    ]


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
