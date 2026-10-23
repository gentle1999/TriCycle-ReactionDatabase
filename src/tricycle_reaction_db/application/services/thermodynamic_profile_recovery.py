"""Bounded dependency audits recover refresh work even when no queue row exists."""

from dataclasses import dataclass
from uuid import UUID

from sqlmodel import Session, col, select

from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics import (
    MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION,
)
from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence import (
    enqueue_mapped_reaction_profile_refresh,
)
from tricycle_reaction_db.application.services.reaction_topology_representatives import (
    observed_participant_topology_replacements,
)
from tricycle_reaction_db.db.models import (
    MappedReaction,
    MappedReactionThermodynamicProfileRefreshJob,
)


@dataclass(frozen=True)
class ProfileDependencyAudit:
    scanned: int
    topology_replacements: int
    enqueued: int
    next_cursor: UUID | None


def recover_profile_refresh_dependencies(
    session: Session, *, after_id: UUID | None = None, batch_size: int = 256
) -> ProfileDependencyAudit:
    """Audit one ID-ordered page and enqueue repairs without mutating source facts.

    A current generation alone cannot prove topology references are current.
    Existing jobs own their retries; healthy profiles, including legitimate
    incomplete profiles, must not be requeued merely because energy is null.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    statement = select(MappedReaction).order_by(col(MappedReaction.id)).limit(batch_size)
    if after_id is not None:
        statement = statement.where(col(MappedReaction.id) > after_id)
    page = session.exec(statement).all()
    ids = [r.id for r in page if isinstance(r.id, UUID)]
    if not ids:
        return ProfileDependencyAudit(0, 0, 0, None)
    queued = set(
        session.exec(
            select(MappedReactionThermodynamicProfileRefreshJob.mapped_reaction_id).where(
                col(MappedReactionThermodynamicProfileRefreshJob.mapped_reaction_id).in_(ids)
            )
        ).all()
    )
    unqueued = [r for r in page if r.id not in queued]
    replacements = observed_participant_topology_replacements(
        session, [r.id for r in unqueued if isinstance(r.id, UUID)]
    )
    changed_reactions = {r.participant.mapped_reaction_id for r in replacements}
    affected = [
        reaction
        for reaction in unqueued
        if reaction.id in changed_reactions
        or reaction.thermodynamic_profile_materialized_generation
        < reaction.thermodynamic_profile_generation
        or reaction.thermodynamic_profile_policy_version
        != MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION
    ]
    if affected:
        enqueue_mapped_reaction_profile_refresh(session, affected, immediate=True)
    return ProfileDependencyAudit(
        scanned=len(page),
        topology_replacements=len(replacements),
        enqueued=len(affected),
        next_cursor=ids[-1] if len(page) == batch_size else None,
    )
