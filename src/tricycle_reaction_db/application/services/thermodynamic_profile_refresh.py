"""Refresh mapped-reaction profiles after deferred upload persistence."""

from __future__ import annotations

import logging
from collections.abc import Collection
from time import perf_counter
from typing import cast
from uuid import UUID

from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session as SQLModelSession
from sqlmodel import col, select

from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence import (
    refresh_mapped_reactions_thermodynamics,
)
from tricycle_reaction_db.db.models import MappedReaction
from tricycle_reaction_db.db.session import session_factory

logger = logging.getLogger(__name__)

# Keep the maintenance transaction small enough that a profile refresh cannot
# recreate the long lock convoy that deferred upload persistence is designed
# to avoid.  The dirty flag is cleared in the same transaction as the profile
# replacement, so a failed chunk remains eligible for the next retry.
THERMODYNAMIC_PROFILE_REFRESH_CHUNK_SIZE = 256


async def _refresh_dirty_chunk(project_ids: tuple[UUID, ...] | None) -> int:
    predicates = [col(MappedReaction.thermodynamic_profile_dirty).is_(True)]
    if project_ids:
        predicates.append(col(MappedReaction.project_id).in_(project_ids))
    async with session_factory() as session:
        reactions = (
            await session.exec(
                select(MappedReaction)
                .where(*predicates)
                .order_by(col(MappedReaction.id))
                .limit(THERMODYNAMIC_PROFILE_REFRESH_CHUNK_SIZE)
            )
        ).all()
        if not reactions:
            return 0

        def _refresh_profiles(sync_session: SQLAlchemySession, /) -> None:
            refresh_mapped_reactions_thermodynamics(
                cast(SQLModelSession, sync_session),
                mapped_reactions=tuple(reactions),
            )

        await session.run_sync(_refresh_profiles)
        await session.commit()
        return len(reactions)


async def refresh_dirty_mapped_reaction_profiles(
    project_ids: Collection[UUID] | None = None,
    *,
    reason: str,
) -> bool:
    """Refresh all dirty profiles, in short independent transactions.

    ``None`` is intentional for worker recovery: a worker restart must still
    drain dirty reactions left by a previous process.  A project collection
    narrows normal queue-drain maintenance to the projects just processed.
    """

    normalized_project_ids = (
        tuple(sorted(set(project_ids), key=str)) if project_ids is not None else None
    )
    if normalized_project_ids == ():
        return True
    started_at = perf_counter()
    refreshed = 0
    chunks = 0
    try:
        while True:
            chunk_size = await _refresh_dirty_chunk(normalized_project_ids)
            if chunk_size == 0:
                break
            refreshed += chunk_size
            chunks += 1
    except Exception:
        logger.exception(
            "deferred thermodynamic profile refresh failed reason=%s projects=%s "
            "refreshed=%d chunks=%d",
            reason,
            ",".join(str(project_id) for project_id in normalized_project_ids or ()) or "all",
            refreshed,
            chunks,
        )
        return False

    logger.info(
        "deferred thermodynamic profiles refreshed reason=%s projects=%s reactions=%d "
        "chunks=%d elapsed_ms=%.1f",
        reason,
        ",".join(str(project_id) for project_id in normalized_project_ids or ()) or "all",
        refreshed,
        chunks,
        (perf_counter() - started_at) * 1000,
    )
    return True


__all__ = [
    "THERMODYNAMIC_PROFILE_REFRESH_CHUNK_SIZE",
    "refresh_dirty_mapped_reaction_profiles",
]
