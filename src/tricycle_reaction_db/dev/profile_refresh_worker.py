"""Run durable thermodynamic-profile refreshes outside the upload worker."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from tricycle_reaction_db.application.services.thermodynamic_profile_refresh import (
    refresh_pending_mapped_reaction_profiles,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.session import dispose_engine

logger = logging.getLogger(__name__)


class ThermodynamicProfileRefreshWorker:
    """Consume the coalesced profile queue in a maintenance process.

    This process intentionally owns no MolOP pool and no upload leases.  The
    profile calculation uses synchronous ORM work internally, so keeping it in
    this separate service prevents a large refresh from blocking parsing,
    persistence, or upload-queue heartbeats.
    """

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        settings = get_settings()
        try:
            while stop_event is None or not stop_event.is_set():
                refreshed = await refresh_pending_mapped_reaction_profiles(
                    allow_while_busy=False,
                    reason="profile-refresh-worker",
                )
                if not refreshed:
                    logger.warning("profile refresh pass failed; retrying after poll interval")
                if stop_event is None:
                    await asyncio.sleep(settings.profile_refresh_worker_poll_interval_seconds)
                else:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(
                            stop_event.wait(),
                            timeout=settings.profile_refresh_worker_poll_interval_seconds,
                        )
        finally:
            await dispose_engine()


async def _run_worker() -> None:
    await ThermodynamicProfileRefreshWorker().run()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    try:
        asyncio.run(_run_worker())
    except KeyboardInterrupt:
        logger.info("profile refresh worker stopped")


__all__ = ["ThermodynamicProfileRefreshWorker", "main"]
