"""Consume durable UniTS TS dataset export jobs outside the API process."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from tricycle_reaction_db.application.services.units_ts_dataset_export import (
    claim_units_ts_dataset_export_job,
    expire_units_ts_dataset_exports,
    process_units_ts_dataset_export_job,
    purge_legacy_units_ts_dataset_exports,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.session import dispose_engine

logger = logging.getLogger(__name__)
LEGACY_EXPORT_PURGE_INTERVAL_SECONDS = 300.0


class UnitsTsDatasetWorker:
    """Build queued datasets in a dedicated maintenance process."""

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        settings = get_settings()
        loop = asyncio.get_running_loop()
        next_legacy_export_purge = 0.0
        try:
            while stop_event is None or not stop_event.is_set():
                try:
                    if loop.time() >= next_legacy_export_purge:
                        next_legacy_export_purge = (
                            loop.time() + LEGACY_EXPORT_PURGE_INTERVAL_SECONDS
                        )
                        await purge_legacy_units_ts_dataset_exports()
                    await expire_units_ts_dataset_exports()
                    job = await claim_units_ts_dataset_export_job()
                    if job is not None:
                        await process_units_ts_dataset_export_job(job)
                        continue
                except Exception:
                    logger.exception("UniTS dataset worker pass failed")
                if stop_event is None:
                    await asyncio.sleep(settings.units_dataset_worker_poll_interval_seconds)
                else:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(
                            stop_event.wait(),
                            timeout=settings.units_dataset_worker_poll_interval_seconds,
                        )
        finally:
            await dispose_engine()


async def _run_worker() -> None:
    await UnitsTsDatasetWorker().run()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    try:
        asyncio.run(_run_worker())
    except KeyboardInterrupt:
        logger.info("UniTS dataset worker stopped")


__all__ = ["UnitsTsDatasetWorker", "main"]
