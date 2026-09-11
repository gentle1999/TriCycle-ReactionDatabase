"""Run the durable server-owned artifact upload queue."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from tricycle_reaction_db.application.services.artifact_uploads import (
    ArtifactUploadService,
    close_molop_process_pool,
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

    async def _renew_until_done(
        self,
        job: UploadProcessingJob,
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
                if not await UploadBatchService.renew_processing_lease(job):
                    logger.warning(
                        "processing lease was lost batch=%s item=%s",
                        job.batch_id,
                        job.item_id,
                    )
                    return
            except Exception:
                # The parser remains authoritative for this lease. A transient
                # heartbeat failure is retried on the next interval; if the
                # lease really expires, finalization is protected by lease_id.
                logger.exception(
                    "failed to renew processing lease batch=%s item=%s",
                    job.batch_id,
                    job.item_id,
                )

    async def _process(self, job: UploadProcessingJob) -> None:
        finished = asyncio.Event()
        heartbeat = asyncio.create_task(self._renew_until_done(job, finished))
        try:
            result = await ArtifactUploadService.reparse(
                artifact_id=job.artifact_file_id,
                user_id=job.user_id,
            )
        except asyncio.CancelledError:
            # Leave the lease for recovery.  A cancellation can happen while
            # MolOP is being shut down and must not manufacture a parse failure.
            raise
        except Exception as error:
            try:
                await UploadBatchService.finish_processing(job, error=error)
            except Exception:
                logger.exception(
                    "failed to record upload worker error batch=%s item=%s",
                    job.batch_id,
                    job.item_id,
                )
        else:
            try:
                await UploadBatchService.finish_processing(job, result=result)
            except Exception:
                logger.exception(
                    "failed to record upload worker result batch=%s item=%s",
                    job.batch_id,
                    job.item_id,
                )
        finally:
            finished.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _renew_pending_until_done(
        self,
        job: PendingIngestionJob,
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
                if not await UploadBatchService.renew_pending_ingestion_lease(job):
                    logger.warning(
                        "pending ingestion lease was lost ingestion=%s artifact=%s",
                        job.ingestion_id,
                        job.artifact_file_id,
                    )
                    return
            except Exception:
                logger.exception(
                    "failed to renew pending ingestion lease ingestion=%s artifact=%s",
                    job.ingestion_id,
                    job.artifact_file_id,
                )

    async def _process_pending(self, job: PendingIngestionJob) -> None:
        """Resume an orphaned reservation and let the service finalize it."""

        finished = asyncio.Event()
        heartbeat = asyncio.create_task(self._renew_pending_until_done(job, finished))
        try:
            await ArtifactUploadService.reparse(
                artifact_id=job.artifact_file_id,
                user_id=job.user_id,
                ingestion_id=job.ingestion_id,
                ingestion_lease_id=job.lease_id,
            )
        except asyncio.CancelledError:
            # The lease deliberately remains claimable after expiry.
            raise
        except Exception as error:
            # ``reparse`` records parse/storage failures itself. This fallback
            # also covers authorization or precondition errors raised before
            # its normal failure boundary.
            try:
                await ArtifactUploadService.fail_pending_ingestion(
                    ingestion_id=job.ingestion_id,
                    lease_id=job.lease_id,
                    error=error,
                )
            except Exception:
                logger.exception(
                    "failed to record orphaned ingestion error ingestion=%s artifact=%s",
                    job.ingestion_id,
                    job.artifact_file_id,
                )
        finally:
            finished.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        settings = get_settings()
        while stop_event is None or not stop_event.is_set():
            try:
                await UploadBatchService.recover_stale()
                jobs = await UploadBatchService.claim_processing(
                    limit=settings.upload_worker_concurrency,
                )
                if jobs:
                    await asyncio.gather(*(self._process(job) for job in jobs))
                    continue
                pending_jobs = await UploadBatchService.claim_pending_ingestions(
                    limit=settings.upload_worker_concurrency,
                )
                if pending_jobs:
                    await asyncio.gather(*(self._process_pending(job) for job in pending_jobs))
                    continue
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


async def _run_worker() -> None:
    try:
        await UploadBatchWorker().run()
    finally:
        await close_molop_process_pool()
        await dispose_engine()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_run_worker())


__all__ = ["UploadBatchWorker", "main"]
