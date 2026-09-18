"""Durable, coalesced refreshes for mapped-reaction thermodynamic profiles."""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session as SQLModelSession
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence import (
    refresh_mapped_reactions_thermodynamics,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    MappedReaction,
    MappedReactionThermodynamicProfile,
    MappedReactionThermodynamicProfileRefreshJob,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import ThermodynamicProfileRefreshJobStatus

logger = logging.getLogger(__name__)

# Compatibility constant for callers that used the old refresh service. The
# worker's actual batch size is now deployment-configurable.
THERMODYNAMIC_PROFILE_REFRESH_CHUNK_SIZE = 256
_ERROR_MAX_LENGTH = 4_000
_RETRY_MAX_SECONDS = 300


@dataclass(frozen=True, slots=True)
class ClaimedProfileRefreshJob:
    """One leased queue item handed to the profile refresh consumer."""

    mapped_reaction_id: UUID
    requested_generation: int
    lease_id: UUID
    lease_expires_at: datetime


def _normalized_project_ids(
    project_ids: Collection[UUID] | None,
) -> tuple[UUID, ...] | None:
    if project_ids is None:
        return None
    return tuple(sorted(set(project_ids), key=str))


async def _claim_profile_refresh_jobs(
    *,
    project_ids: tuple[UUID, ...] | None,
    limit: int,
    allow_while_busy: bool,
    ignore_schedule: bool,
) -> tuple[ClaimedProfileRefreshJob, ...]:
    if limit < 1 or project_ids == ():
        return ()

    now = datetime.now(UTC)
    settings = get_settings()
    lease_expires_at = now + timedelta(seconds=settings.upload_worker_profile_refresh_lease_seconds)
    lease_id = uuid4()
    async with session_factory() as session:
        # A worker crash must not strand a job in ``processing``. Recovery is
        # deliberately part of claiming so another worker can make progress
        # without a separate profile-recovery daemon.
        await session.exec(
            update(MappedReactionThermodynamicProfileRefreshJob)
            .where(
                col(MappedReactionThermodynamicProfileRefreshJob.status)
                == ThermodynamicProfileRefreshJobStatus.PROCESSING,
                (
                    col(MappedReactionThermodynamicProfileRefreshJob.lease_expires_at).is_(None)
                    | (col(MappedReactionThermodynamicProfileRefreshJob.lease_expires_at) <= now)
                ),
            )
            .values(
                status=ThermodynamicProfileRefreshJobStatus.PENDING,
                lease_id=None,
                lease_expires_at=None,
                available_at=now,
                updated_at=now,
            )
        )

        predicates = [
            col(MappedReactionThermodynamicProfileRefreshJob.status)
            == ThermodynamicProfileRefreshJobStatus.PENDING,
            col(MappedReactionThermodynamicProfileRefreshJob.available_at) <= now,
        ]
        if project_ids is not None:
            predicates.append(col(MappedReaction.project_id).in_(project_ids))
        if not ignore_schedule and allow_while_busy:
            max_delay = timedelta(seconds=settings.upload_worker_profile_refresh_max_delay_seconds)
            predicates.append(
                (col(MappedReactionThermodynamicProfileRefreshJob.priority) > 0)
                | (
                    col(MappedReactionThermodynamicProfileRefreshJob.requested_at)
                    <= now - max_delay
                )
            )

        rows = (
            await session.exec(
                select(MappedReactionThermodynamicProfileRefreshJob, MappedReaction)
                .join(
                    MappedReaction,
                    col(MappedReaction.id)
                    == col(MappedReactionThermodynamicProfileRefreshJob.mapped_reaction_id),
                )
                .where(*predicates)
                .order_by(
                    col(MappedReactionThermodynamicProfileRefreshJob.priority).desc(),
                    col(MappedReactionThermodynamicProfileRefreshJob.requested_at),
                    col(MappedReactionThermodynamicProfileRefreshJob.mapped_reaction_id),
                )
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        ).all()
        if not rows:
            await session.commit()
            return ()

        jobs: list[ClaimedProfileRefreshJob] = []
        for job, _reaction in rows:
            job.status = ThermodynamicProfileRefreshJobStatus.PROCESSING
            job.lease_id = lease_id
            job.lease_expires_at = lease_expires_at
            job.attempt_count += 1
            job.updated_at = now
            session.add(job)
            jobs.append(
                ClaimedProfileRefreshJob(
                    mapped_reaction_id=job.mapped_reaction_id,
                    requested_generation=job.requested_generation,
                    lease_id=lease_id,
                    lease_expires_at=lease_expires_at,
                )
            )
        await session.commit()
        return tuple(jobs)


async def _refresh_source_visibility(
    session: AsyncSession,
    mapped_reaction_ids: Collection[UUID],
) -> None:
    profile_ids = (
        await session.exec(
            select(MappedReactionThermodynamicProfile.id).where(
                col(MappedReactionThermodynamicProfile.mapped_reaction_id).in_(
                    tuple(mapped_reaction_ids)
                )
            )
        )
    ).all()
    normalized_profile_ids = tuple(
        profile_id for profile_id in profile_ids if profile_id is not None
    )
    if not normalized_profile_ids:
        return

    statement = text(
        "SELECT refresh_thermodynamic_profile_source_visibility(:profile_ids)"
    ).bindparams(
        bindparam(
            "profile_ids",
            type_=ARRAY(PostgreSQLUUID(as_uuid=True)),
        )
    )
    await session.execute(
        statement,
        {"profile_ids": list(normalized_profile_ids)},
    )


async def _reschedule_profile_refresh_jobs(
    claimed_jobs: Collection[ClaimedProfileRefreshJob],
    error: Exception,
) -> None:
    if not claimed_jobs:
        return
    now = datetime.now(UTC)
    lease_ids = tuple({job.lease_id for job in claimed_jobs})
    job_ids = tuple(job.mapped_reaction_id for job in claimed_jobs)
    error_text = str(error).strip() or error.__class__.__name__
    error_text = error_text[:_ERROR_MAX_LENGTH]
    async with session_factory() as session:
        jobs = (
            await session.exec(
                select(MappedReactionThermodynamicProfileRefreshJob)
                .where(
                    col(MappedReactionThermodynamicProfileRefreshJob.mapped_reaction_id).in_(
                        job_ids
                    ),
                    col(MappedReactionThermodynamicProfileRefreshJob.lease_id).in_(lease_ids),
                    col(MappedReactionThermodynamicProfileRefreshJob.status)
                    == ThermodynamicProfileRefreshJobStatus.PROCESSING,
                )
                .with_for_update()
            )
        ).all()
        for job in jobs:
            retry_seconds = min(
                _RETRY_MAX_SECONDS,
                2 ** min(max(job.attempt_count - 1, 0), 8),
            )
            job.status = ThermodynamicProfileRefreshJobStatus.PENDING
            job.available_at = now + timedelta(seconds=retry_seconds)
            job.lease_id = None
            job.lease_expires_at = None
            job.last_error = error_text
            job.updated_at = now
            session.add(job)
        await session.commit()


async def _process_profile_refresh_jobs(
    claimed_jobs: tuple[ClaimedProfileRefreshJob, ...],
) -> int:
    if not claimed_jobs:
        return 0
    claimed_by_id = {job.mapped_reaction_id: job for job in claimed_jobs}
    mapped_reaction_ids = tuple(claimed_by_id)
    try:
        # Do not lock MappedReaction while the synchronous profile algorithm
        # reads source rows and performs RDKit/policy work.  A source write can
        # advance the generation concurrently; the short finalization
        # transaction below will detect that and keep the job pending.
        async with session_factory() as session:
            reactions = (
                await session.exec(
                    select(MappedReaction).where(col(MappedReaction.id).in_(mapped_reaction_ids))
                )
            ).all()
            reactions_by_id = {
                reaction.id: reaction for reaction in reactions if isinstance(reaction.id, UUID)
            }
            refreshable = tuple(
                reaction
                for reaction in reactions
                if isinstance(reaction.id, UUID)
                and reaction.thermodynamic_profile_materialized_generation
                < max(
                    claimed_by_id[reaction.id].requested_generation,
                    reaction.thermodynamic_profile_generation,
                )
            )
            refreshed_ids = tuple(
                reaction.id for reaction in refreshable if isinstance(reaction.id, UUID)
            )
            if refreshable:
                await session.execute(
                    text("SET LOCAL tricycle.defer_profile_source_visibility = 'on'")
                )

                def _refresh_profiles(sync_session: SQLAlchemySession, /) -> None:
                    refresh_mapped_reactions_thermodynamics(
                        cast(SQLModelSession, sync_session),
                        mapped_reactions=refreshable,
                        clear_refresh_jobs=False,
                    )

                await session.run_sync(_refresh_profiles)
            # Commit profile replacement before running source visibility SQL.
            # This releases profile/reaction write locks while the visibility
            # dependency graph is traversed and lets upload/delete transactions
            # proceed independently.
            await session.commit()

        # Visibility is deliberately a separate short transaction. If it fails,
        # the still-processing lease is rescheduled and the profile is retried
        # rather than leaving a permanently stale visible/hidden state.
        async with session_factory() as session:
            await _refresh_source_visibility(session, refreshed_ids)
            current_reactions = (
                await session.exec(
                    select(MappedReaction)
                    .where(col(MappedReaction.id).in_(mapped_reaction_ids))
                    .with_for_update()
                )
            ).all()
            jobs = (
                await session.exec(
                    select(MappedReactionThermodynamicProfileRefreshJob)
                    .where(
                        col(MappedReactionThermodynamicProfileRefreshJob.mapped_reaction_id).in_(
                            mapped_reaction_ids
                        ),
                        col(MappedReactionThermodynamicProfileRefreshJob.status)
                        == ThermodynamicProfileRefreshJobStatus.PROCESSING,
                    )
                    .with_for_update()
                )
            ).all()
            reactions_by_id = {
                reaction.id: reaction
                for reaction in current_reactions
                if isinstance(reaction.id, UUID)
            }
            jobs_by_id = {job.mapped_reaction_id: job for job in jobs}
            now = datetime.now(UTC)
            completed = 0
            for mapped_reaction_id, claimed in claimed_by_id.items():
                job = jobs_by_id.get(mapped_reaction_id)
                if job is None or job.lease_id != claimed.lease_id:
                    continue
                reaction = reactions_by_id.get(mapped_reaction_id)
                if reaction is None:
                    await session.delete(job)
                    completed += 1
                    continue
                current_generation = reaction.thermodynamic_profile_generation
                requested_generation = max(job.requested_generation, current_generation)
                if reaction.thermodynamic_profile_materialized_generation >= requested_generation:
                    await session.delete(job)
                    completed += 1
                    continue
                # New source evidence arrived while this job was running. Keep
                # the coalesced row and immediately schedule one more pass.
                job.status = ThermodynamicProfileRefreshJobStatus.PENDING
                job.requested_generation = requested_generation
                job.available_at = now
                job.lease_id = None
                job.lease_expires_at = None
                job.last_error = None
                job.updated_at = now
                session.add(job)
            await session.commit()
            return completed
    except Exception as error:
        await _reschedule_profile_refresh_jobs(claimed_jobs, error)
        raise


async def refresh_pending_mapped_reaction_profiles(
    project_ids: Collection[UUID] | None = None,
    *,
    limit: int | None = None,
    allow_while_busy: bool = False,
    ignore_schedule: bool = False,
    reason: str,
) -> bool:
    """Drain durable profile jobs in short leased transactions.

    ``allow_while_busy`` admits only manual priority jobs and jobs older than
    the configured maximum delay. An idle worker drains all ready jobs,
    including normal debounced import work. Generations make retries and
    concurrent source updates idempotent.
    """

    normalized_project_ids = _normalized_project_ids(project_ids)
    if normalized_project_ids == ():
        return True
    started_at = perf_counter()
    processed = 0
    batches = 0
    try:
        while limit is None or processed < limit:
            batch_limit = get_settings().upload_worker_profile_refresh_batch_size
            if limit is not None:
                batch_limit = min(batch_limit, limit - processed)
            claimed = await _claim_profile_refresh_jobs(
                project_ids=normalized_project_ids,
                limit=batch_limit,
                allow_while_busy=allow_while_busy,
                ignore_schedule=ignore_schedule,
            )
            if not claimed:
                break
            await _process_profile_refresh_jobs(claimed)
            processed += len(claimed)
            batches += 1
    except Exception:
        logger.exception(
            "thermodynamic profile refresh failed reason=%s projects=%s processed=%d batches=%d",
            reason,
            ",".join(str(project_id) for project_id in normalized_project_ids or ()) or "all",
            processed,
            batches,
        )
        return False

    logger.info(
        "thermodynamic profiles refreshed reason=%s projects=%s jobs=%d batches=%d elapsed_ms=%.1f",
        reason,
        ",".join(str(project_id) for project_id in normalized_project_ids or ()) or "all",
        processed,
        batches,
        (perf_counter() - started_at) * 1000,
    )
    return True


async def refresh_dirty_mapped_reaction_profiles(
    project_ids: Collection[UUID] | None = None,
    *,
    reason: str,
) -> bool:
    """Compatibility wrapper for callers using the former dirty-flag API."""

    return await refresh_pending_mapped_reaction_profiles(
        project_ids,
        allow_while_busy=False,
        ignore_schedule=True,
        reason=reason,
    )


__all__ = [
    "ClaimedProfileRefreshJob",
    "THERMODYNAMIC_PROFILE_REFRESH_CHUNK_SIZE",
    "refresh_dirty_mapped_reaction_profiles",
    "refresh_pending_mapped_reaction_profiles",
]
