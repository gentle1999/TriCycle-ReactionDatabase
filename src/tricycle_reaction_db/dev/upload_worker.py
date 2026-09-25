"""Run the durable server-owned artifact upload queue."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from time import perf_counter
from uuid import UUID

from tricycle_reaction_db.application.dtos import ArtifactUploadResult
from tricycle_reaction_db.application.services.artifact_upload_types import ParsedArtifactTask
from tricycle_reaction_db.application.services.artifact_uploads import (
    ArtifactUploadService,
    close_molop_process_pool,
    molop_process_worker_count,
    warm_molop_process_pool,
    warm_storage_process_pool,
)
from tricycle_reaction_db.application.services.database_statistics import (
    refresh_project_statistics,
)
from tricycle_reaction_db.application.services.upload_batches import (
    PendingIngestionJob,
    UploadBatchService,
    UploadProcessingJob,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.session import dispose_engine

logger = logging.getLogger(__name__)

WorkerJob = UploadProcessingJob | PendingIngestionJob
QueuedParseResult = tuple[WorkerJob, ParsedArtifactTask]


class UploadBatchWorker:
    """Lease staged objects, parse them, and publish terminal queue states."""

    def __init__(self) -> None:
        # A worker poll can contain several project/user groups and several
        # client-side one-file batches.  Coalesce their affected projects and
        # analyze once when both queues become empty, instead of analyzing
        # once per file or per persistence microbatch.
        self._statistics_dirty_project_ids: set[UUID] = set()
        self._statistics_refresh_failed = False
        self._stream_active = False

    def _mark_statistics_dirty(self, project_ids: Iterable[UUID]) -> None:
        self._statistics_dirty_project_ids.update(
            project_id for project_id in project_ids if isinstance(project_id, UUID)
        )
        self._statistics_refresh_failed = False

    async def _flush_statistics(self) -> None:
        if not self._statistics_dirty_project_ids or self._statistics_refresh_failed:
            return
        project_ids = tuple(self._statistics_dirty_project_ids)
        refreshed = await refresh_project_statistics(
            project_ids,
            reason="upload-worker-queue-drained",
        )
        if refreshed:
            self._statistics_dirty_project_ids.clear()
        else:
            # Do not retry a failed maintenance operation on every idle poll.
            # A new claimed job resets this flag and creates another natural
            # retry boundary.
            self._statistics_refresh_failed = True

    @staticmethod
    def _stream_prefetch_limit() -> int:
        """Return the bounded number of parser tasks admitted ahead of writes."""

        settings = get_settings()
        configured = settings.upload_worker_prefetch_files
        if configured > 0:
            return configured
        return max(4, molop_process_worker_count() * 2)

    @staticmethod
    async def _claim_stream_jobs(
        *,
        limit: int,
        prefer_processing: bool,
    ) -> list[WorkerJob]:
        """Claim a small refill page while alternating compatibility work."""

        if limit < 1:
            return []
        jobs: list[WorkerJob] = []
        if prefer_processing:
            jobs.extend(await UploadBatchService.claim_processing(limit=limit))
            remaining = limit - len(jobs)
            if remaining:
                jobs.extend(await UploadBatchService.claim_pending_ingestions(limit=remaining))
        else:
            jobs.extend(await UploadBatchService.claim_pending_ingestions(limit=limit))
            remaining = limit - len(jobs)
            if remaining:
                jobs.extend(await UploadBatchService.claim_processing(limit=remaining))
        return jobs

    @staticmethod
    async def _clear_claimed_parse_state(
        jobs: list[WorkerJob],
        *,
        project_write_locks: dict[UUID, asyncio.Lock] | None = None,
    ) -> dict[UUID, Exception]:
        """Replace old revisions before parser tasks enter the shared pool."""

        groups: dict[tuple[UUID, UUID], list[WorkerJob]] = {}
        for job in jobs:
            groups.setdefault((job.project_id, job.user_id), []).append(job)
        errors: dict[UUID, Exception] = {}
        for (project_id, user_id), group in groups.items():
            leases = {job.artifact_file_id: (job.lease_id, job.lease_expires_at) for job in group}
            try:

                async def clear_group(
                    group: list[WorkerJob] = group,
                    user_id: UUID = user_id,
                    leases: Mapping[UUID, tuple[UUID, datetime | None]] = leases,
                ) -> None:
                    await ArtifactUploadService.clear_previous_parse_results(
                        artifact_ids=[job.artifact_file_id for job in group],
                        user_id=user_id,
                        worker_lease_by_artifact_id=leases,
                        refresh_statistics=False,
                        defer_thermodynamic_refresh=True,
                    )

                if project_write_locks is None:
                    await clear_group()
                else:
                    async with project_write_locks.setdefault(project_id, asyncio.Lock()):
                        await clear_group()
            except Exception as error:
                for job in group:
                    errors[job.artifact_file_id] = error
                logger.exception(
                    "failed to clear old parse state project=%s files=%d",
                    project_id,
                    len(group),
                )
        return errors

    async def _renew_stream_leases(
        self,
        active_jobs: dict[UUID, WorkerJob],
        finished: asyncio.Event,
    ) -> None:
        """Renew all parser and persistence claims while the stream is active."""

        settings = get_settings()
        interval = max(5.0, min(60.0, settings.upload_worker_lease_seconds / 3))
        while not finished.is_set():
            try:
                await asyncio.wait_for(finished.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            jobs = tuple(active_jobs.values())
            processing_jobs = [job for job in jobs if isinstance(job, UploadProcessingJob)]
            pending_jobs = [job for job in jobs if isinstance(job, PendingIngestionJob)]
            try:
                if processing_jobs:
                    await UploadBatchService.renew_processing_leases(processing_jobs)
                if pending_jobs:
                    await UploadBatchService.renew_pending_ingestion_leases(pending_jobs)
            except Exception:
                logger.exception("failed to renew streaming upload leases jobs=%d", len(jobs))

    async def _run_streaming_cycle(
        self,
        stop_event: asyncio.Event | None = None,
    ) -> bool:
        """Continuously refill the shared parser pool and drain one DB consumer."""

        prefetch_limit = self._stream_prefetch_limit()
        parser_workers = molop_process_worker_count()
        result_queue: asyncio.Queue[QueuedParseResult | None] = asyncio.Queue(
            maxsize=prefetch_limit
        )
        active_jobs: dict[UUID, WorkerJob] = {}
        grouped_results: dict[tuple[UUID, UUID, bool], list[QueuedParseResult]] = {}
        grouped_frames: dict[tuple[UUID, UUID, bool], int] = {}
        project_write_locks: dict[UUID, asyncio.Lock] = {}
        settings = get_settings()
        persistence_file_limit = settings.upload_worker_persistence_batch_files
        persistence_frame_limit = settings.upload_worker_persistence_frame_limit
        persistence_buffer_limit = max(
            persistence_file_limit,
            min(prefetch_limit, parser_workers * 4),
        )
        buffered_result_count = 0
        had_work = False
        parse_file_count = 0
        parse_file_sum_ms = 0.0
        parse_phase_started_at: float | None = None
        parse_phase_finished_at: float | None = None

        def frame_count(task: ParsedArtifactTask) -> int:
            parsed = task.parsed
            records = getattr(parsed, "frame_records", ())
            source_count = getattr(parsed, "source_frame_count", 0)
            return max(1, len(records) or int(source_count or 0))

        async def flush_group(key: tuple[UUID, UUID, bool]) -> None:
            nonlocal buffered_result_count
            entries = grouped_results.pop(key, [])
            grouped_frames.pop(key, None)
            if not entries:
                return
            buffered_result_count = max(0, buffered_result_count - len(entries))
            jobs = [job for job, _task in entries]
            tasks = [task for _job, task in entries]
            project_id, user_id, source_atom_order_authoritative = key
            leases = {job.artifact_file_id: job.lease_id for job in jobs}
            async with project_write_locks.setdefault(project_id, asyncio.Lock()):
                try:
                    results = await ArtifactUploadService.persist_parsed_microbatch(
                        tasks,
                        project_id=project_id,
                        user_id=user_id,
                        worker_lease_by_artifact_id=leases,
                        defer_thermodynamic_refresh=True,
                        source_atom_order_authoritative=source_atom_order_authoritative,
                    )
                except Exception as error:
                    logger.exception(
                        "failed to persist parser microbatch project=%s files=%d",
                        project_id,
                        len(jobs),
                    )
                    results = {job.artifact_file_id: error for job in jobs}

                upload_jobs = [job for job in jobs if isinstance(job, UploadProcessingJob)]
                if upload_jobs:
                    try:
                        finalized = await UploadBatchService.finish_processing_batch(
                            upload_jobs,
                            results,
                        )
                        if finalized != len(upload_jobs):
                            logger.warning(
                                "streaming upload finalization was incomplete project=%s "
                                "expected=%d finalized=%d",
                                project_id,
                                len(upload_jobs),
                                finalized,
                            )
                    except Exception:
                        logger.exception(
                            "failed to finalize streaming upload microbatch project=%s files=%d",
                            project_id,
                            len(upload_jobs),
                        )
                    for upload_job in upload_jobs:
                        active_jobs.pop(upload_job.artifact_file_id, None)
                for pending_job in jobs:
                    if not isinstance(pending_job, PendingIngestionJob):
                        active_jobs.pop(pending_job.artifact_file_id, None)
                        continue
                    result = results.get(pending_job.artifact_file_id)
                    if isinstance(result, ArtifactUploadResult):
                        active_jobs.pop(pending_job.artifact_file_id, None)
                        continue
                    try:
                        await ArtifactUploadService.fail_pending_ingestion(
                            ingestion_id=pending_job.ingestion_id,
                            lease_id=pending_job.lease_id,
                            error=(
                                result
                                if isinstance(result, Exception)
                                else RuntimeError("parser microbatch returned no result")
                            ),
                        )
                    except Exception:
                        logger.exception(
                            "failed to finalize compatibility ingestion=%s artifact=%s",
                            pending_job.ingestion_id,
                            pending_job.artifact_file_id,
                        )
                    active_jobs.pop(pending_job.artifact_file_id, None)

        async def consume_results() -> None:
            nonlocal buffered_result_count
            while True:
                entry = await result_queue.get()
                try:
                    if entry is None:
                        break
                    job, task = entry
                    key = (
                        job.project_id,
                        job.user_id,
                        bool(getattr(job, "source_atom_order_authoritative", False)),
                    )
                    grouped_results.setdefault(key, []).append(entry)
                    grouped_frames[key] = grouped_frames.get(key, 0) + frame_count(task)
                    buffered_result_count += 1
                    if (
                        len(grouped_results[key]) >= persistence_file_limit
                        or grouped_frames[key] >= persistence_frame_limit
                    ):
                        await flush_group(key)
                    else:
                        if buffered_result_count >= persistence_buffer_limit:
                            # A low-volume project/user group may never reach
                            # the normal microbatch threshold while the global
                            # queue remains busy. Flush the largest available
                            # group to keep result memory bounded; authorization
                            # isolation still prevents cross-user persistence.
                            oldest_key = max(
                                grouped_results,
                                key=lambda candidate: len(grouped_results[candidate]),
                            )
                            await flush_group(oldest_key)
                finally:
                    result_queue.task_done()
            for key in tuple(grouped_results):
                await flush_group(key)

        async def parse_and_enqueue(job: WorkerJob, clear_error: Exception | None) -> None:
            nonlocal parse_file_count, parse_file_sum_ms
            nonlocal parse_phase_started_at, parse_phase_finished_at
            started_at = perf_counter()
            parse_phase_started_at = min(parse_phase_started_at or started_at, started_at)
            if clear_error is not None:
                task = ParsedArtifactTask(
                    artifact_id=job.artifact_file_id,
                    started_at=datetime.now(UTC),
                    parsed=clear_error,
                )
            else:
                task = await ArtifactUploadService.parse_staged_artifact(job.artifact_file_id)
            finished_at = perf_counter()
            parse_file_count += 1
            parse_file_sum_ms += (finished_at - started_at) * 1000
            parse_phase_finished_at = max(parse_phase_finished_at or finished_at, finished_at)
            await result_queue.put((job, task))

        finished = asyncio.Event()
        heartbeat = asyncio.create_task(self._renew_stream_leases(active_jobs, finished))
        consumer = asyncio.create_task(consume_results())
        parser_tasks: set[asyncio.Task[None]] = set()
        prefer_processing = True

        async def wait_for_parser_task() -> None:
            if not parser_tasks:
                return
            done, pending = await asyncio.wait(
                parser_tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            parser_tasks.clear()
            parser_tasks.update(pending)
            for task in done:
                task.result()

        try:
            self._stream_active = True
            while stop_event is None or not stop_event.is_set():
                room = prefetch_limit - len(parser_tasks)
                if room > 0:
                    claim_limit = min(room, max(1, parser_workers))
                    jobs = await self._claim_stream_jobs(
                        limit=claim_limit,
                        prefer_processing=prefer_processing,
                    )
                    prefer_processing = not prefer_processing
                    if jobs:
                        had_work = True
                        for job in jobs:
                            active_jobs[job.artifact_file_id] = job
                        self._mark_statistics_dirty(job.project_id for job in jobs)
                        clear_errors = await self._clear_claimed_parse_state(
                            jobs,
                            project_write_locks=project_write_locks,
                        )
                        for job in jobs:
                            parser_tasks.add(
                                asyncio.create_task(
                                    parse_and_enqueue(
                                        job,
                                        clear_errors.get(job.artifact_file_id),
                                    )
                                )
                            )
                        continue
                if parser_tasks:
                    await wait_for_parser_task()
                    continue
                break
            while parser_tasks:
                await wait_for_parser_task()
            await result_queue.put(None)
            await consumer
            logger.info(
                "upload worker cycle parsed files=%d parse_wall_ms=%.1f parse_sum_ms=%.1f "
                "parse_overlap=%.2f buffered_results=%d",
                parse_file_count,
                (
                    (parse_phase_finished_at - parse_phase_started_at) * 1000
                    if parse_phase_started_at is not None and parse_phase_finished_at is not None
                    else 0.0
                ),
                parse_file_sum_ms,
                parse_file_sum_ms
                / max(
                    (parse_phase_finished_at - parse_phase_started_at) * 1000
                    if parse_phase_started_at is not None and parse_phase_finished_at is not None
                    else 1.0,
                    1.0,
                ),
                buffered_result_count,
            )
        except BaseException:
            for task in parser_tasks:
                task.cancel()
            if parser_tasks:
                await asyncio.gather(*parser_tasks, return_exceptions=True)
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            raise
        finally:
            self._stream_active = False
            finished.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        return had_work

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        settings = get_settings()
        recovery_limit = max(1, min(512, self._stream_prefetch_limit()))
        startup_recovery_complete = False
        try:
            while stop_event is None or not stop_event.is_set():
                try:
                    if not startup_recovery_complete:
                        recovered_total = 0
                        while True:
                            recovered = await UploadBatchService.recover_stale(
                                limit=recovery_limit,
                                recover_unexpired_processing=True,
                            )
                            recovered_total += recovered
                            if recovered < recovery_limit:
                                break
                        startup_recovery_complete = True
                        if recovered_total:
                            logger.warning(
                                "recovered processing leases left by the previous upload worker "
                                "files=%d",
                                recovered_total,
                            )
                    await UploadBatchService.recover_stale(limit=recovery_limit)
                    had_work = await self._run_streaming_cycle(stop_event)
                    if had_work:
                        continue
                    await self._flush_statistics()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A database/object-store outage must not terminate the
                    # worker; leases make the next pass safe after the outage
                    # clears.
                    logger.exception("upload worker poll failed")

                if stop_event is None:
                    await asyncio.sleep(settings.upload_worker_poll_interval_seconds)
                else:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(
                            stop_event.wait(),
                            timeout=settings.upload_worker_poll_interval_seconds,
                        )
        finally:
            await self._flush_statistics()


async def _run_worker() -> None:
    worker = UploadBatchWorker()
    try:
        warmed_workers = await warm_molop_process_pool()
        logger.info(
            "warmed shared MolOP process pool workers=%d configured=%d",
            warmed_workers,
            molop_process_worker_count(),
        )
        warmed_storage_workers = await warm_storage_process_pool()
        logger.info(
            "warmed shared RustFS storage process pool workers=%d configured=%d",
            warmed_storage_workers,
            get_settings().upload_max_concurrency,
        )
        await worker.run()
    finally:
        # Cancellation can interrupt a parse group before ``run`` reaches its
        # normal idle boundary. Give completed database work one final
        # post-commit statistics refresh before shutting down the shared pool.
        await worker._flush_statistics()
        await close_molop_process_pool()
        await dispose_engine()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        # The API image may install a handler before the console entry point
        # runs.  Without ``force`` basicConfig leaves the inherited WARNING
        # level in place and hides the worker's phase telemetry.
        force=True,
    )
    asyncio.run(_run_worker())


__all__ = ["UploadBatchWorker", "main"]
