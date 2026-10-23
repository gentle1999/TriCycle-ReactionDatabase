"""Run durable thermodynamic-profile refreshes outside the upload worker."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import cast
from uuid import UUID

from sqlmodel import Session

from tricycle_reaction_db.application.services.thermodynamic_profile_recovery import (
    recover_profile_refresh_dependencies,
)
from tricycle_reaction_db.application.services.thermodynamic_profile_refresh import (
    refresh_pending_mapped_reaction_profiles,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.session import dispose_engine, session_factory

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
        cursor: UUID | None = None
        scanned_in_cycle = 0
        try:
            while stop_event is None or not stop_event.is_set():
                try:
                    async with session_factory() as session:
                        audit = await session.run_sync(
                            lambda sync_session, audit_cursor=cursor: (
                                recover_profile_refresh_dependencies(
                                    cast(Session, sync_session),
                                    after_id=audit_cursor,
                                    batch_size=settings.profile_refresh_worker_audit_batch_size,
                                )
                            )
                        )
                        await session.commit()
                    cursor = audit.next_cursor
                    scanned_in_cycle += audit.scanned
                    if audit.enqueued:
                        logger.info(
                            "profile dependency audit recovered jobs=%d topology_references=%d",
                            audit.enqueued,
                            audit.topology_replacements,
                        )
                    if cursor is None:
                        logger.info("profile dependency audit cycle scanned=%d", scanned_in_cycle)
                        scanned_in_cycle = 0
                except Exception:
                    # Keep the cursor unchanged and retry this page. A transient
                    # audit failure must not prevent already queued work running.
                    logger.exception("profile dependency audit failed; retaining cursor for retry")
                refreshed = await refresh_pending_mapped_reaction_profiles(
                    # Bound queue draining so continuous imports cannot starve
                    # the next dependency-audit page or worker shutdown.
                    limit=settings.upload_worker_profile_refresh_batch_size * 4,
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
