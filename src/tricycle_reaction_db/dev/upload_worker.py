"""Run the durable server-owned artifact upload queue."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from contextlib import suppress
from time import monotonic
from uuid import UUID

from tricycle_reaction_db.application.dtos import ArtifactUploadResult
from tricycle_reaction_db.application.services.artifact_uploads import (
    ArtifactUploadService,
    close_molop_process_pool,
)
from tricycle_reaction_db.application.services.database_statistics import (
    refresh_project_statistics,
)
from tricycle_reaction_db.application.services.thermodynamic_profile_refresh import (
    refresh_dirty_mapped_reaction_profiles,
)
from tricycle_reaction_db.application.services.upload_batches import (
    PendingIngestionJob,
    UploadBatchService,
    UploadProcessingJob,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.session import dispose_engine

logger = logging.getLogger(__name__)


class UploadBatchWorker:
    """Lease staged objects, parse them, and publish terminal queue states."""

    def __init__(self) -> None:
        # A worker poll can contain several project/user groups and several
        # client-side one-file batches.  Coalesce their affected projects and
        # analyze once when both queues become empty, instead of analyzing
        # once per file or per persistence microbatch.
        self._statistics_dirty_project_ids: set[UUID] = set()
        self._statistics_refresh_failed = False
        self._profile_dirty_project_ids: set[UUID] = set()
        self._profile_dirty_since: dict[UUID, float] = {}
        self._profile_refresh_retry_at: float | None = None
        self._profile_recovery_checked = False

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

    def _mark_profiles_dirty(self, project_ids: Iterable[UUID]) -> None:
        now = monotonic()
        for project_id in project_ids:
            if not isinstance(project_id, UUID):
                continue
            self._profile_dirty_project_ids.add(project_id)
            self._profile_dirty_since.setdefault(project_id, now)
        self._profile_refresh_retry_at = None
        self._profile_recovery_checked = False

    async def _flush_profiles(self, *, force: bool = False) -> None:
        """Refresh deferred profiles at queue drain or after the max delay."""

        if (
            self._profile_refresh_retry_at is not None
            and monotonic() < self._profile_refresh_retry_at
        ):
            return
        if force:
            if not self._profile_dirty_project_ids and self._profile_recovery_checked:
                return
            # A forced drain also recovers dirty reactions left by a previous
            # worker process, so do not narrow the query to this worker's
            # in-memory project set.
            project_ids: tuple[UUID, ...] | None = None
        else:
            if not self._profile_dirty_project_ids:
                return
            max_delay = get_settings().upload_worker_profile_refresh_max_delay_seconds
            now = monotonic()
            project_ids = tuple(
                project_id
                for project_id in self._profile_dirty_project_ids
                if now - self._profile_dirty_since.get(project_id, now) >= max_delay
            )
            if not project_ids:
                return

        refreshed = await refresh_dirty_mapped_reaction_profiles(
            project_ids,
            reason=(
                "upload-worker-queue-drained"
                if force
                else "upload-worker-profile-refresh-max-delay"
            ),
        )
        if refreshed:
            if project_ids is None:
                self._profile_dirty_project_ids.clear()
                self._profile_dirty_since.clear()
                self._profile_recovery_checked = True
            else:
                for project_id in project_ids:
                    self._profile_dirty_project_ids.discard(project_id)
                    self._profile_dirty_since.pop(project_id, None)
        else:
            # Avoid turning a database maintenance outage into a tight retry
            # loop, while still retrying durable dirty rows when the outage
            # clears without requiring a new upload.
            retry_delay = max(
                5.0,
                min(
                    60.0,
                    get_settings().upload_worker_profile_refresh_max_delay_seconds,
                ),
            )
            self._profile_refresh_retry_at = monotonic() + retry_delay

    async def _renew_until_done(
        self,
        jobs: list[UploadProcessingJob],
        finished: asyncio.Event,
    ) -> None:
        settings = get_settings()
        interval = max(5.0, min(60.0, settings.upload_worker_lease_seconds / 3))
        while not finished.is_set():
            try:
                await asyncio.wait_for(finished.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                renewed = await UploadBatchService.renew_processing_leases(jobs)
                if renewed == 0 and not finished.is_set():
                    logger.warning(
                        "processing lease group was lost project=%s jobs=%d",
                        jobs[0].project_id,
                        len(jobs),
                    )
                    return
            except Exception:
                # The parser remains authoritative for this lease. A transient
                # heartbeat failure is retried on the next interval; if the
                # lease really expires, finalization is protected by lease_id.
                logger.exception(
                    "failed to renew processing lease group project=%s jobs=%d",
                    jobs[0].project_id,
                    len(jobs),
                )

    async def _process_jobs(self, jobs: list[UploadProcessingJob]) -> None:
        """Reparse one claim window through sequential persistence microbatches.

        Queue batches describe the client-facing upload session, not the
        persistence unit.  A claim window can therefore contain many
        one-file batches.  Combine those files by project and author before
        calling ``reparse_batch`` so the parser queue and the single database
        persistence consumer see a real microbatch.  Different projects (or
        users, whose authorization context must remain isolated) are processed
        one after another because each call owns a project-scoped transaction.
        """

        if not jobs:
            return
        # UploadBatch is a client-facing queue boundary, not a persistence
        # boundary. Grouping by project and user lets independent one-file
        # queues enter the same parser/persistence microbatch while keeping
        # the authorization and project-scoped reconciliation context safe.
        groups: dict[tuple[UUID, UUID], list[UploadProcessingJob]] = {}
        for job in jobs:
            groups.setdefault((job.project_id, job.user_id), []).append(job)

        async def process_microbatch(group: list[UploadProcessingJob]) -> None:
            group_results: dict[UUID, ArtifactUploadResult | Exception]
            finished = asyncio.Event()
            heartbeat = asyncio.create_task(self._renew_until_done(group, finished))
            try:
                try:
                    group_results = await ArtifactUploadService.reparse_batch(
                        artifact_ids=[job.artifact_file_id for job in group],
                        user_id=group[0].user_id,
                        force_reparse=True,
                        refresh_statistics=False,
                        defer_thermodynamic_refresh=True,
                    )
                except Exception as error:
                    group_results = {job.artifact_file_id: error for job in group}

                try:
                    finalized = await UploadBatchService.finish_processing_batch(
                        group,
                        group_results,
                    )
                    if finalized != len(group):
                        logger.warning(
                            "upload worker finalized only part of a result group "
                            "expected=%d finalized=%d",
                            len(group),
                            finalized,
                        )
                except Exception:
                    logger.exception(
                        "failed to record upload worker result group project=%s files=%d",
                        group[0].project_id,
                        len(group),
                    )
            finally:
                finished.set()
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

        # Do not submit persistence groups concurrently.  Each
        # ``reparse_batch`` owns a project-scoped transaction and identity
        # locks; concurrent groups only turn independent one-file queue
        # items into a database lock convoy.  Parsing inside each call is
        # still concurrent through the shared MolOP process pool.
        for group in groups.values():
            await process_microbatch(group)

    async def _renew_pending_until_done(
        self,
        jobs: list[PendingIngestionJob],
        finished: asyncio.Event,
    ) -> None:
        settings = get_settings()
        interval = max(5.0, min(60.0, settings.upload_worker_lease_seconds / 3))
        while not finished.is_set():
            try:
                await asyncio.wait_for(finished.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                renewed = await UploadBatchService.renew_pending_ingestion_leases(jobs)
                if renewed == 0 and not finished.is_set():
                    logger.warning(
                        "pending ingestion lease group was lost project=%s jobs=%d",
                        jobs[0].project_id,
                        len(jobs),
                    )
                    return
            except Exception:
                logger.exception(
                    "failed to renew pending ingestion lease group project=%s jobs=%d",
                    jobs[0].project_id,
                    len(jobs),
                )

    async def _process_pending_jobs(self, jobs: list[PendingIngestionJob]) -> None:
        """Feed compatibility reservations into the shared batch pipeline.

        These rows predate ``UploadBatch`` but must have the same execution
        semantics as durable web uploads.  In particular, do not reparse each
        reservation in its own coroutine: that creates a completion barrier
        around the claim window and makes every one-file legacy upload own a
        separate persistence transaction.  Grouping by project and user lets
        ``reparse_batch`` keep one parser/persistence pipeline and one
        project-scoped transaction for the whole group.
        """

        if not jobs:
            return

        groups: dict[tuple[UUID, UUID], list[PendingIngestionJob]] = {}
        for job in jobs:
            groups.setdefault((job.project_id, job.user_id), []).append(job)

        async def process_microbatch(group: list[PendingIngestionJob]) -> None:
            group_results: dict[UUID, ArtifactUploadResult | Exception]
            finished = asyncio.Event()
            heartbeat = asyncio.create_task(self._renew_pending_until_done(group, finished))
            try:
                try:
                    group_results = await ArtifactUploadService.reparse_batch(
                        artifact_ids=[job.artifact_file_id for job in group],
                        user_id=group[0].user_id,
                        force_reparse=True,
                        refresh_statistics=False,
                        defer_thermodynamic_refresh=True,
                    )
                except asyncio.CancelledError:
                    # Leave the reservations leased so normal stale-lease
                    # recovery can return them to pending after shutdown.
                    raise
                except Exception as error:
                    group_results = {job.artifact_file_id: error for job in group}

                for job in group:
                    result = group_results.get(job.artifact_file_id)
                    if isinstance(result, Exception):
                        try:
                            await ArtifactUploadService.fail_pending_ingestion(
                                ingestion_id=job.ingestion_id,
                                lease_id=job.lease_id,
                                error=result,
                            )
                        except Exception:
                            logger.exception(
                                "failed to record orphaned ingestion error "
                                "ingestion=%s artifact=%s",
                                job.ingestion_id,
                                job.artifact_file_id,
                            )
                    elif result is None:
                        missing_result_error = RuntimeError("artifact reparse returned no result")
                        try:
                            await ArtifactUploadService.fail_pending_ingestion(
                                ingestion_id=job.ingestion_id,
                                lease_id=job.lease_id,
                                error=missing_result_error,
                            )
                        except Exception:
                            logger.exception(
                                "failed to record missing orphaned ingestion result ingestion=%s "
                                "artifact=%s",
                                job.ingestion_id,
                                job.artifact_file_id,
                            )
                    # A successful or partial ArtifactUploadResult already owns
                    # final ingestion state through upload_batch.  Do not write
                    # a second terminal state after that transaction.
            finally:
                finished.set()
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

        # Keep project/user groups serial.  Parsing within each call is still
        # concurrent through the single shared MolOP process pool, while
        # concurrent project transactions would create DB lock convoys and
        # defeat the shared persistence consumer.
        for group in groups.values():
            await process_microbatch(group)

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        settings = get_settings()
        startup_recovery_complete = False
        while stop_event is None or not stop_event.is_set():
            try:
                if not startup_recovery_complete:
                    recovered_total = 0
                    while True:
                        recovered = await UploadBatchService.recover_stale(
                            limit=settings.max_batch_files,
                            recover_unexpired_processing=True,
                        )
                        recovered_total += recovered
                        if recovered < settings.max_batch_files:
                            break
                    startup_recovery_complete = True
                    if recovered_total:
                        logger.warning(
                            "recovered processing leases left by the previous upload worker "
                            "files=%d",
                            recovered_total,
                        )
                await UploadBatchService.recover_stale()
                jobs = await UploadBatchService.claim_processing(
                    # Keep the parser pool fed with a bounded file queue. The
                    # pool itself remains limited by ``molop_batch_n_jobs``;
                    # claiming only that many files would make a slow file
                    # pause replenishment and leave workers idle.
                    limit=settings.max_batch_files,
                )
                if jobs:
                    self._mark_statistics_dirty(job.project_id for job in jobs)
                    self._mark_profiles_dirty(job.project_id for job in jobs)
                    await self._process_jobs(jobs)
                    await self._flush_profiles()
                    continue
                pending_jobs = await UploadBatchService.claim_pending_ingestions(
                    # Compatibility reservations use the same bounded claim
                    # window as durable upload batches.  The old value here
                    # (the worker concurrency, normally 16) caused a
                    # sixteen-file completion barrier and starved the shared
                    # parser pool between claims.
                    limit=settings.max_batch_files,
                )
                if pending_jobs:
                    self._mark_statistics_dirty(job.project_id for job in pending_jobs)
                    self._mark_profiles_dirty(job.project_id for job in pending_jobs)
                    await self._process_pending_jobs(pending_jobs)
                    await self._flush_profiles()
                    continue
                await self._flush_profiles(force=True)
                await self._flush_statistics()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A database/object-store outage must not terminate the worker;
                # leases make the next pass safe after the outage clears.
                logger.exception("upload worker poll failed")

            if stop_event is None:
                await asyncio.sleep(settings.upload_worker_poll_interval_seconds)
            else:
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=settings.upload_worker_poll_interval_seconds,
                    )
        # A graceful stop can arrive immediately after the last result was
        # finalized.  Flush the coalesced project set before returning so the
        # final import/reparse boundary gets the same statistics refresh as an
        # idle poll.
        await self._flush_profiles(force=True)
        await self._flush_statistics()


async def _run_worker() -> None:
    worker = UploadBatchWorker()
    try:
        await worker.run()
    finally:
        # Cancellation can interrupt a parse group before ``run`` reaches its
        # normal idle boundary. Give the completed database work one final
        # post-commit statistics refresh before shutting down the shared pool.
        await worker._flush_profiles(force=True)
        await worker._flush_statistics()
        await close_molop_process_pool()
        await dispose_engine()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_run_worker())


__all__ = ["UploadBatchWorker", "main"]
