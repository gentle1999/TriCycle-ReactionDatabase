"""Concrete-topology membership for abstract reaction participants.

This module owns the relation between a logical participant's query topology
and the strict topologies that may instantiate it.  It intentionally does not
create mapped reactions: a concrete topology may be useful evidence before a
complete atom mapping exists.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlmodel import Session, col, select

from tricycle_reaction_db.application.services._persistence import (
    _acquire_identity_locks,
    _flush_new_entity,
    _new_entity,
    _require_id,
)
from tricycle_reaction_db.application.services.topology_abstraction import (
    find_topology_matches,
    specialized_topologies,
)
from tricycle_reaction_db.core.chemistry_config import (
    LOGICAL_PARTICIPANT_CONCRETE_MATCH_POLICY_VERSION,
    LOGICAL_PARTICIPANT_CONCRETE_MATCH_SCHEMA_VERSION,
)
from tricycle_reaction_db.db.models import (
    LogicalParticipantConcreteTopology,
    LogicalReactionParticipant,
    MolecularTopology,
    MolecularTopologyAbstraction,
)


class ConcreteTopologyMembershipError(ValueError):
    """The proposed concrete topology cannot instantiate a logical participant."""


def _pending_entities(session: Session) -> tuple[object, ...]:
    return (
        *tuple(session.new),
        *tuple(session.info.get("_fast_pending_entities", ())),
    )


def _compatible_topology_candidate(
    candidate: MolecularTopology,
    concrete_topology: MolecularTopology,
) -> bool:
    return (
        candidate.formula_id == concrete_topology.formula_id
        and candidate.atom_count == concrete_topology.atom_count
        and candidate.formal_charge == concrete_topology.formal_charge
    )


def _match_metadata(
    logical_topology: MolecularTopology,
    concrete_topology: MolecularTopology,
    matches: tuple[tuple[int, ...], ...],
) -> dict[str, Any]:
    return {
        "match_schema_version": LOGICAL_PARTICIPANT_CONCRETE_MATCH_SCHEMA_VERSION,
        "logical_topology_id": str(_require_id(logical_topology, label="MolecularTopology")),
        "concrete_topology_id": str(_require_id(concrete_topology, label="MolecularTopology")),
        "candidate_match_count": len(matches),
        # A unique match is convenient for downstream mapping transfer.  For a
        # symmetric graph, retain every legal match and leave selection to the
        # reaction-level mapping constraints instead of taking an arbitrary one.
        "general_to_concrete_atom_indices": list(matches[0]) if len(matches) == 1 else None,
        "candidate_general_to_concrete_atom_indices": [list(match) for match in matches],
    }


def _find_pending_membership(
    session: Session,
    *,
    logical_participant_id: UUID,
    concrete_topology_id: UUID,
) -> LogicalParticipantConcreteTopology | None:
    for entity in _pending_entities(session):
        if not isinstance(entity, LogicalParticipantConcreteTopology):
            continue
        if (
            entity.logical_reaction_participant_id == logical_participant_id
            and entity.concrete_topology_id == concrete_topology_id
        ):
            return entity
    return None


def persist_logical_participant_concrete_topology(
    session: Session,
    logical_participant: LogicalReactionParticipant,
    concrete_topology: MolecularTopology,
    *,
    match_policy_version: str = LOGICAL_PARTICIPANT_CONCRETE_MATCH_POLICY_VERSION,
    match_metadata: dict[str, Any] | None = None,
) -> LogicalParticipantConcreteTopology:
    """Validate and idempotently persist one concrete member relation."""

    logical_participant_id = _require_id(
        logical_participant,
        label="LogicalReactionParticipant",
    )
    concrete_topology_id = _require_id(concrete_topology, label="MolecularTopology")
    logical_topology = logical_participant.topology
    if not _compatible_topology_candidate(logical_topology, concrete_topology):
        raise ConcreteTopologyMembershipError(
            "concrete topology differs in formula, atom count, or formal charge"
        )
    matches = find_topology_matches(concrete_topology.mol, logical_topology.mol)
    if not matches:
        raise ConcreteTopologyMembershipError(
            "concrete topology is not a stereo-aware graph match for the logical topology"
        )

    _acquire_identity_locks(
        session,
        (
            "logical_participant_concrete_topology",
            logical_participant_id,
            concrete_topology_id,
        ),
    )
    membership = session.exec(
        select(LogicalParticipantConcreteTopology).where(
            LogicalParticipantConcreteTopology.logical_reaction_participant_id
            == logical_participant_id,
            LogicalParticipantConcreteTopology.concrete_topology_id == concrete_topology_id,
        )
    ).first()
    if membership is None:
        membership = _find_pending_membership(
            session,
            logical_participant_id=logical_participant_id,
            concrete_topology_id=concrete_topology_id,
        )
    metadata = _match_metadata(logical_topology, concrete_topology, matches)
    if match_metadata:
        metadata["caller_metadata"] = dict(match_metadata)
    if membership is not None:
        # Legacy backfill rows carry identity-only evidence.  Refresh that
        # evidence whenever the relation is touched, while retaining the one
        # membership identity and never creating a second policy row.
        membership.match_policy_version = match_policy_version
        membership.match_status = "matched"
        membership.match_metadata = metadata
        if membership not in session.new and membership not in session.info.get(
            "_fast_pending_entities", ()
        ):
            session.add(membership)
            session.flush()
        return membership

    membership = _new_entity(
        session,
        LogicalParticipantConcreteTopology,
        logical_reaction_participant=logical_participant,
        logical_reaction_participant_id=logical_participant_id,
        concrete_topology=concrete_topology,
        concrete_topology_id=concrete_topology_id,
        match_policy_version=match_policy_version,
        match_status="matched",
        match_metadata=metadata,
    )
    _flush_new_entity(session, membership, label="LogicalParticipantConcreteTopology")
    return membership


def _pending_specializations(
    session: Session,
    root_id: UUID,
    candidates: Iterable[MolecularTopology],
) -> tuple[MolecularTopology, ...]:
    """Collect same-transaction DAG descendants not visible to SQL yet."""

    topology_by_id = {
        _require_id(topology, label="MolecularTopology"): topology for topology in candidates
    }
    edges = tuple(
        edge
        for entity in _pending_entities(session)
        if isinstance(entity, MolecularTopologyAbstraction)
        for edge in (entity,)
    )
    reached = {root_id}
    changed = True
    while changed:
        changed = False
        for edge in edges:
            if edge.general_topology_id in reached and edge.specific_topology_id not in reached:
                reached.add(edge.specific_topology_id)
                changed = True
    return tuple(
        topology for topology_id, topology in topology_by_id.items() if topology_id in reached
    )


def concrete_topology_candidates_for_logical_participant(
    session: Session,
    logical_participant: LogicalReactionParticipant,
    *,
    candidate_topologies: Iterable[MolecularTopology] = (),
) -> tuple[MolecularTopology, ...]:
    """Return already materialized DAG descendants of one logical topology."""

    logical_topology = logical_participant.topology
    root_id = _require_id(logical_topology, label="MolecularTopology")
    candidates_by_id: dict[UUID, MolecularTopology] = {
        root_id: logical_topology,
    }
    for topology in specialized_topologies(session, logical_topology, include_general=True):
        topology_id = _require_id(topology, label="MolecularTopology")
        if _compatible_topology_candidate(topology, logical_topology):
            candidates_by_id[topology_id] = topology
    pending = _pending_specializations(session, root_id, candidate_topologies)
    for topology in pending:
        if _compatible_topology_candidate(topology, logical_topology):
            candidates_by_id[_require_id(topology, label="MolecularTopology")] = topology
    return tuple(
        sorted(
            candidates_by_id.values(),
            key=lambda topology: (
                topology.graph_hash,
                str(_require_id(topology, label="MolecularTopology")),
            ),
        )
    )


def ensure_logical_participant_concrete_memberships(
    session: Session,
    logical_participant: LogicalReactionParticipant,
    *,
    candidate_topologies: Iterable[MolecularTopology] = (),
) -> tuple[LogicalParticipantConcreteTopology, ...]:
    """Register every matching materialized descendant currently in the DAG."""

    memberships: list[LogicalParticipantConcreteTopology] = []
    for topology in concrete_topology_candidates_for_logical_participant(
        session,
        logical_participant,
        candidate_topologies=candidate_topologies,
    ):
        memberships.append(
            persist_logical_participant_concrete_topology(
                session,
                logical_participant,
                topology,
            )
        )
    return tuple(memberships)


def logical_participant_matches_for_concrete_topology(
    session: Session,
    concrete_topology: MolecularTopology,
    *,
    candidate_participants: Iterable[LogicalReactionParticipant] = (),
) -> tuple[tuple[LogicalReactionParticipant, tuple[tuple[int, ...], ...]], ...]:
    """Find existing logical participants that a concrete topology instantiates."""

    _require_id(concrete_topology, label="MolecularTopology")
    participants_by_id: dict[UUID, LogicalReactionParticipant] = {}
    for participant in candidate_participants:
        participant_id = _require_id(participant, label="LogicalReactionParticipant")
        if _compatible_topology_candidate(participant.topology, concrete_topology):
            participants_by_id[participant_id] = participant
    rows = session.exec(
        select(LogicalReactionParticipant)
        .join(MolecularTopology)
        .where(
            col(MolecularTopology.formula_id) == concrete_topology.formula_id,
            col(MolecularTopology.atom_count) == concrete_topology.atom_count,
            col(MolecularTopology.formal_charge) == concrete_topology.formal_charge,
        )
    ).all()
    for participant in rows:
        participant_id = _require_id(participant, label="LogicalReactionParticipant")
        participants_by_id[participant_id] = participant

    matches: list[tuple[LogicalReactionParticipant, tuple[tuple[int, ...], ...]]] = []
    for participant in sorted(
        participants_by_id.values(),
        key=lambda item: (
            str(item.logical_reaction_id),
            item.side.value,
            item.participant_index,
            str(_require_id(item, label="LogicalReactionParticipant")),
        ),
    ):
        topology_matches = find_topology_matches(
            concrete_topology.mol,
            participant.topology.mol,
        )
        if topology_matches:
            matches.append((participant, topology_matches))
    return tuple(matches)


def ensure_concrete_topology_memberships(
    session: Session,
    concrete_topology: MolecularTopology,
    *,
    candidate_participants: Iterable[LogicalReactionParticipant] = (),
) -> tuple[LogicalParticipantConcreteTopology, ...]:
    """Register all logical participants instantiated by one concrete topology."""

    memberships: list[LogicalParticipantConcreteTopology] = []
    for participant, topology_matches in logical_participant_matches_for_concrete_topology(
        session,
        concrete_topology,
        candidate_participants=candidate_participants,
    ):
        memberships.append(
            persist_logical_participant_concrete_topology(
                session,
                participant,
                concrete_topology,
                match_metadata={
                    "candidate_match_count": len(topology_matches),
                },
            )
        )
    return tuple(memberships)


__all__ = [
    "LOGICAL_PARTICIPANT_CONCRETE_MATCH_POLICY_VERSION",
    "ConcreteTopologyMembershipError",
    "concrete_topology_candidates_for_logical_participant",
    "ensure_concrete_topology_memberships",
    "ensure_logical_participant_concrete_memberships",
    "logical_participant_matches_for_concrete_topology",
    "persist_logical_participant_concrete_topology",
]
