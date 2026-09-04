"""Resolve concrete logical members into strict mapped-reaction instances."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlmodel import Session, col, select

from tricycle_reaction_db.application.dtos.reactions import MappedReactionRecord
from tricycle_reaction_db.application.services._persistence import (
    _attach_pending_entities,
    _require_id,
)
from tricycle_reaction_db.application.services.reaction_geometry_reconciliation import (
    ReconciliationBatchCache,
    reconcile_mapped_reaction_with_geometries,
    resolve_endpoint_node,
    share_mapped_reaction_evidence,
)
from tricycle_reaction_db.application.services.reaction_topology_membership import (
    ensure_concrete_topology_memberships,
    ensure_logical_participant_concrete_memberships,
)
from tricycle_reaction_db.application.services.reactions import (
    _resolve_topology_value,
    persist_mapped_reaction,
    transfer_mapped_reaction_to_concrete_topologies,
)
from tricycle_reaction_db.db.models import (
    LogicalParticipantConcreteTopology,
    LogicalReaction,
    LogicalReactionParticipant,
    MappedReaction,
    MappedReactionParticipant,
    MolecularTopology,
)
from tricycle_reaction_db.domain.enums import LogicalReactionParticipantSide


def _source_participants(
    session: Session,
    mapped_reaction: MappedReaction,
) -> tuple[MappedReactionParticipant, ...]:
    """Load participants explicitly when a fast-path reaction is detached."""

    participants = tuple(mapped_reaction.participants)
    if participants:
        return participants
    persisted = tuple(
        session.exec(
            select(MappedReactionParticipant).where(
                MappedReactionParticipant.mapped_reaction_id == mapped_reaction.id
            )
        ).all()
    )
    pending = tuple(
        entity
        for entity in (
            *tuple(session.new),
            *tuple(session.info.get("_fast_pending_entities", ())),
        )
        if isinstance(entity, MappedReactionParticipant)
        and entity.mapped_reaction_id == mapped_reaction.id
    )
    by_id = {
        _require_id(participant, label="MappedReactionParticipant"): participant
        for participant in (*persisted, *pending)
        if isinstance(participant.id, UUID)
    }
    return tuple(by_id.values())


def _mapped_reactions_for_logical_reaction(
    session: Session,
    logical_reaction_id: UUID,
) -> tuple[MappedReaction, ...]:
    """Load mapped reactions, including rows deferred by fast insertion."""

    persisted = tuple(
        session.exec(
            select(MappedReaction).where(MappedReaction.logical_reaction_id == logical_reaction_id)
        ).all()
    )
    pending = tuple(
        entity
        for entity in (
            *tuple(session.new),
            *tuple(session.info.get("_fast_pending_entities", ())),
        )
        if isinstance(entity, MappedReaction) and entity.logical_reaction_id == logical_reaction_id
    )
    by_id = {
        _require_id(mapped_reaction, label="MappedReaction"): mapped_reaction
        for mapped_reaction in (*persisted, *pending)
        if isinstance(mapped_reaction.id, UUID)
    }
    return tuple(
        sorted(
            by_id.values(),
            key=lambda mapped_reaction: (
                mapped_reaction.mapping_hash,
                str(_require_id(mapped_reaction, label="MappedReaction")),
            ),
        )
    )


def _memberships_for_concrete_topology(
    session: Session,
    concrete_topology_id: UUID,
) -> tuple[LogicalParticipantConcreteTopology, ...]:
    """Load concrete memberships, including fast-path rows not flushed yet."""

    persisted = tuple(
        session.exec(
            select(LogicalParticipantConcreteTopology).where(
                LogicalParticipantConcreteTopology.concrete_topology_id == concrete_topology_id
            )
        ).all()
    )
    pending = tuple(
        entity
        for entity in (
            *tuple(session.new),
            *tuple(session.info.get("_fast_pending_entities", ())),
        )
        if isinstance(entity, LogicalParticipantConcreteTopology)
        and entity.concrete_topology_id == concrete_topology_id
    )
    by_id = {
        _require_id(membership, label="LogicalParticipantConcreteTopology"): membership
        for membership in (*persisted, *pending)
        if isinstance(membership.id, UUID)
    }
    return tuple(by_id.values())


def _complete_mapped_reaction(
    session: Session,
    mapped_reaction: MappedReaction,
    source_participants: tuple[MappedReactionParticipant, ...],
) -> bool:
    """Return whether one mapped reaction is safe to use as a transfer seed."""

    logical_participants = tuple(
        session.exec(
            select(LogicalReactionParticipant).where(
                LogicalReactionParticipant.logical_reaction_id
                == mapped_reaction.logical_reaction_id
            )
        ).all()
    )
    if len(source_participants) != len(logical_participants):
        return False
    if not source_participants:
        return False
    side_maps: dict[LogicalReactionParticipantSide, set[int]] = {}
    for participant in source_participants:
        logical_participant = session.get(
            LogicalReactionParticipant,
            participant.logical_reaction_participant_id,
        )
        if logical_participant is None:
            return False
        concrete_topology = (
            session.get(MolecularTopology, participant.concrete_topology_id)
            if participant.concrete_topology_id is not None
            else logical_participant.topology
        )
        if concrete_topology is None:
            return False
        atom_maps = tuple(int(number) for number in participant.atom_map_numbers)
        if (
            len(atom_maps) != concrete_topology.atom_count
            or any(number <= 0 for number in atom_maps)
            or len(set(atom_maps)) != len(atom_maps)
        ):
            return False
        side_maps.setdefault(participant.side, set()).update(atom_maps)
    return side_maps.get(LogicalReactionParticipantSide.REACTANT, set()) == side_maps.get(
        LogicalReactionParticipantSide.PRODUCT, set()
    ) and bool(side_maps.get(LogicalReactionParticipantSide.REACTANT))


def _logical_participant(
    session: Session,
    participant: MappedReactionParticipant,
) -> LogicalReactionParticipant:
    logical_participant = participant.logical_reaction_participant
    if logical_participant is None:  # pragma: no cover - protected by the FK
        raise RuntimeError("MappedReactionParticipant has no logical participant")
    if logical_participant.topology is None:  # pragma: no cover - protected by the FK
        raise RuntimeError("LogicalReactionParticipant has no logical topology")
    return logical_participant


def _target_topologies_for_source(
    session: Session,
    source_participants: tuple[MappedReactionParticipant, ...],
    *,
    selected_logical_participant_id: UUID,
    selected_concrete_topology: MolecularTopology,
) -> dict[tuple[LogicalReactionParticipantSide, int], MolecularTopology]:
    target: dict[tuple[LogicalReactionParticipantSide, int], MolecularTopology] = {}
    for source_participant in source_participants:
        logical_participant = _logical_participant(session, source_participant)
        logical_participant_id = _require_id(
            logical_participant,
            label="LogicalReactionParticipant",
        )
        if logical_participant_id == selected_logical_participant_id:
            concrete_topology = selected_concrete_topology
        elif source_participant.concrete_topology_id is not None:
            concrete_topology = _resolve_topology_value(
                session,
                source_participant.concrete_topology_id,
            )
        else:
            # Compatibility for rows created before concrete_topology_id was
            # added.  The migration backfills normal rows, but this fallback
            # keeps manually repaired legacy rows readable.
            concrete_topology = logical_participant.topology
        target[(source_participant.side, source_participant.template_index)] = concrete_topology
    return target


def _mapped_reaction_has_selected_topology(
    source_participants: tuple[MappedReactionParticipant, ...],
    *,
    selected_logical_participant_id: UUID,
    selected_concrete_topology_id: UUID,
) -> bool:
    for participant in source_participants:
        logical_participant = participant.logical_reaction_participant
        if logical_participant is None:
            continue
        if _require_id(logical_participant, label="LogicalReactionParticipant") != (
            selected_logical_participant_id
        ):
            continue
        current_topology_id = participant.concrete_topology_id or logical_participant.topology_id
        return current_topology_id == selected_concrete_topology_id
    return False


def ensure_mapped_reactions_for_concrete_topology(
    session: Session,
    concrete_topology: MolecularTopology,
    *,
    topology_context: Any | None = None,
    reconciliation_cache: ReconciliationBatchCache | None = None,
    refresh_thermodynamics: bool = True,
) -> tuple[MappedReaction, ...]:
    """Create strict mapped reactions for a newly discovered concrete member.

    Membership is always recorded first.  A mapped reaction is created only
    when an existing mapped reaction under the same logical reaction supplies
    a complete mapping template; a topology with no such template remains a
    concrete member only.
    """

    concrete_topology_id = _require_id(concrete_topology, label="MolecularTopology")
    ensure_concrete_topology_memberships(session, concrete_topology)
    memberships = _memberships_for_concrete_topology(session, concrete_topology_id)
    created_or_reused: dict[UUID, MappedReaction] = {}
    for membership in memberships:
        logical_participant = membership.logical_reaction_participant
        logical_participant_id = _require_id(
            logical_participant,
            label="LogicalReactionParticipant",
        )
        logical_reaction = logical_participant.logical_reaction
        logical_reaction_id = _require_id(logical_reaction, label="LogicalReaction")
        mapped_reactions = _mapped_reactions_for_logical_reaction(session, logical_reaction_id)
        for source_mapped_reaction in mapped_reactions:
            source_participants = _source_participants(session, source_mapped_reaction)
            if not _complete_mapped_reaction(
                session,
                source_mapped_reaction,
                source_participants,
            ):
                continue
            if _mapped_reaction_has_selected_topology(
                source_participants,
                selected_logical_participant_id=logical_participant_id,
                selected_concrete_topology_id=concrete_topology_id,
            ):
                continue
            target_topologies = _target_topologies_for_source(
                session,
                source_participants,
                selected_logical_participant_id=logical_participant_id,
                selected_concrete_topology=concrete_topology,
            )
            transferred = transfer_mapped_reaction_to_concrete_topologies(
                session,
                source_mapped_reaction,
                target_topologies,
            )
            mapped_reaction = persist_mapped_reaction(
                session,
                logical_reaction,
                MappedReactionRecord(
                    mapped_reaction_key=f"mapping:{transferred.mapping_hash}",
                    label=source_mapped_reaction.label,
                    mapped_reaction_kind=source_mapped_reaction.mapped_reaction_kind,
                    mapped_reaction_smiles=transferred.mapped_reaction_smiles,
                    mapping_hash=transferred.mapping_hash,
                ),
                source_atom_maps_by_template=transferred.atom_maps_by_template,
                topology_ids_by_template={
                    (
                        participant.side,
                        participant.template_index,
                    ): _require_id(
                        _logical_participant(session, participant).topology,
                        label="MolecularTopology",
                    )
                    for participant in source_participants
                },
                concrete_topology_ids_by_template=transferred.concrete_topologies_by_template,
                precomputed_mapped_smiles_by_template=transferred.mapped_smiles_by_template,
            )
            mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
            created_or_reused[mapped_reaction_id] = mapped_reaction
            share_mapped_reaction_evidence(
                session,
                source_mapped_reaction=source_mapped_reaction,
                target_mapped_reaction=mapped_reaction,
                cache=reconciliation_cache,
            )
            resolve_endpoint_node(
                session,
                mapped_reaction,
                LogicalReactionParticipantSide.REACTANT,
                cache=reconciliation_cache,
            )
            resolve_endpoint_node(
                session,
                mapped_reaction,
                LogicalReactionParticipantSide.PRODUCT,
                cache=reconciliation_cache,
            )
            if topology_context is not None:
                topology_context.mapped_reactions_by_id[mapped_reaction_id] = mapped_reaction
                topology_context.mapped_reactions_to_reconcile[mapped_reaction_id] = mapped_reaction
            else:
                reconcile_mapped_reaction_with_geometries(
                    session,
                    mapped_reaction,
                    refresh_thermodynamics=refresh_thermodynamics,
                    cache=reconciliation_cache,
                )
    return tuple(created_or_reused.values())


def ensure_mapped_reactions_for_logical_reaction(
    session: Session,
    logical_reaction: LogicalReaction,
    *,
    topology_context: Any | None = None,
    reconciliation_cache: ReconciliationBatchCache | None = None,
    refresh_thermodynamics: bool = True,
) -> tuple[MappedReaction, ...]:
    """Materialize mappings for every already-known concrete reaction member.

    This is the reaction-level counterpart to
    :func:`ensure_mapped_reactions_for_concrete_topology`.  It is deliberately
    a fixed-point pass: when more than one participant has concrete variants,
    a mapping produced for one variant becomes a source for the next variant.
    No unobserved topology is generated.
    """

    # A direct caller can request expansion while the artifact fast path still
    # has deferred rows.  Flush that queue once and use the regular path for
    # the small reaction graph; otherwise the fixed-point loop could keep
    # creating rows that its SQL queries cannot yet see.
    if session.info.get("tricycle_fast_insert", False):
        _attach_pending_entities(session)
        session.flush()
        previous_fast_insert = session.info["tricycle_fast_insert"]
        session.info["tricycle_fast_insert"] = False
        try:
            return ensure_mapped_reactions_for_logical_reaction(
                session,
                logical_reaction,
                topology_context=topology_context,
                reconciliation_cache=reconciliation_cache,
                refresh_thermodynamics=refresh_thermodynamics,
            )
        finally:
            session.info["tricycle_fast_insert"] = previous_fast_insert

    logical_reaction_id = _require_id(logical_reaction, label="LogicalReaction")
    participants = tuple(
        session.exec(
            select(LogicalReactionParticipant)
            .where(LogicalReactionParticipant.logical_reaction_id == logical_reaction_id)
            .order_by(
                col(LogicalReactionParticipant.side),
                col(LogicalReactionParticipant.participant_index),
            )
        ).all()
    )
    for participant in participants:
        ensure_logical_participant_concrete_memberships(session, participant)

    participant_by_id = {
        _require_id(participant, label="LogicalReactionParticipant"): participant
        for participant in participants
    }
    persisted_memberships = tuple(
        session.exec(
            select(LogicalParticipantConcreteTopology)
            .join(
                LogicalReactionParticipant,
                col(LogicalReactionParticipant.id)
                == col(LogicalParticipantConcreteTopology.logical_reaction_participant_id),
            )
            .where(LogicalReactionParticipant.logical_reaction_id == logical_reaction_id)
        ).all()
    )
    pending_memberships = tuple(
        entity
        for entity in (
            *tuple(session.new),
            *tuple(session.info.get("_fast_pending_entities", ())),
        )
        if isinstance(entity, LogicalParticipantConcreteTopology)
        and entity.logical_reaction_participant_id in participant_by_id
    )
    memberships_by_id = {
        _require_id(membership, label="LogicalParticipantConcreteTopology"): membership
        for membership in (*persisted_memberships, *pending_memberships)
        if isinstance(membership.id, UUID)
    }
    memberships = tuple(memberships_by_id.values())
    concrete_topology_ids: set[UUID] = set()
    for membership in memberships:
        logical_participant = participant_by_id.get(membership.logical_reaction_participant_id)
        if logical_participant is None:
            continue
        # An abstract participant topology is the query root, not a strict
        # reaction instance.  Its actual downstream rows are the candidates.
        if (
            logical_participant.topology.is_stereo_abstraction_upstream
            and membership.concrete_topology_id == logical_participant.topology_id
        ):
            continue
        concrete_topology_ids.add(membership.concrete_topology_id)
    if not concrete_topology_ids:
        return ()

    known_ids = {
        _require_id(mapped_reaction, label="MappedReaction")
        for mapped_reaction in _mapped_reactions_for_logical_reaction(
            session,
            logical_reaction_id,
        )
    }
    created: dict[UUID, MappedReaction] = {}

    # Keep revisiting the materialized candidates until every newly-created
    # source mapping has been used.  This covers concrete combinations across
    # multiple reaction participants without enumerating theoretical variants.
    while True:
        previous_count = len(known_ids)
        for concrete_topology_id in sorted(concrete_topology_ids, key=str):
            concrete_topology = _resolve_topology_value(session, concrete_topology_id)
            for mapped_reaction in ensure_mapped_reactions_for_concrete_topology(
                session,
                concrete_topology,
                topology_context=topology_context,
                reconciliation_cache=reconciliation_cache,
                refresh_thermodynamics=refresh_thermodynamics,
            ):
                if mapped_reaction.logical_reaction_id != logical_reaction_id:
                    continue
                mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
                if mapped_reaction_id not in known_ids:
                    known_ids.add(mapped_reaction_id)
                    created[mapped_reaction_id] = mapped_reaction
        if len(known_ids) == previous_count:
            break
    return tuple(created.values())


__all__ = [
    "ensure_mapped_reactions_for_concrete_topology",
    "ensure_mapped_reactions_for_logical_reaction",
]
