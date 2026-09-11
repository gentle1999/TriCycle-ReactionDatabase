"""Resolve concrete logical members into strict mapped-reaction instances."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy.orm.attributes import set_committed_value
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


def _require_project_owner(entity: Any, *, label: str) -> UUID:
    project_id = getattr(entity, "project_id", None)
    if not isinstance(project_id, UUID):
        raise ValueError(f"{label} must have a project_id before reaction expansion")
    return project_id


def _source_participants(
    session: Session,
    mapped_reaction: MappedReaction,
    *,
    topology_context: Any | None = None,
) -> tuple[MappedReactionParticipant, ...]:
    """Load participants explicitly when a fast-path reaction is detached."""

    mapped_reaction_id = mapped_reaction.id
    project_id = _require_project_owner(mapped_reaction, label="MappedReaction")
    if isinstance(mapped_reaction_id, UUID) and topology_context is not None:
        cached = topology_context.mapped_reaction_participants_by_reaction.get(mapped_reaction_id)
        if cached is not None:
            return cast(tuple[MappedReactionParticipant, ...], cached)
    participants = tuple(mapped_reaction.participants)
    if participants:
        if isinstance(mapped_reaction_id, UUID) and topology_context is not None:
            topology_context.mapped_reaction_participants_by_reaction[mapped_reaction_id] = (
                participants
            )
        return participants
    persisted = tuple(
        session.exec(
            select(MappedReactionParticipant)
            .join(
                MappedReaction,
                col(MappedReactionParticipant.mapped_reaction_id) == col(MappedReaction.id),
            )
            .where(
                col(MappedReactionParticipant.mapped_reaction_id) == mapped_reaction.id,
                col(MappedReaction.project_id) == project_id,
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
    result = tuple(by_id.values())
    if isinstance(mapped_reaction_id, UUID) and topology_context is not None:
        topology_context.mapped_reaction_participants_by_reaction[mapped_reaction_id] = result
    return result


def _mapped_reactions_for_logical_reaction(
    session: Session,
    logical_reaction_id: UUID,
    *,
    project_id: UUID,
    topology_context: Any | None = None,
) -> tuple[MappedReaction, ...]:
    """Load mapped reactions, including rows deferred by fast insertion."""

    if topology_context is not None:
        cached = topology_context.mapped_reactions_by_logical_reaction.get(logical_reaction_id)
        if cached is not None:
            return tuple(
                mapped_reaction
                for mapped_reaction in cast(tuple[MappedReaction, ...], cached)
                if mapped_reaction.project_id == project_id
            )
    persisted = tuple(
        session.exec(
            select(MappedReaction)
            .join(
                LogicalReaction,
                col(MappedReaction.logical_reaction_id) == col(LogicalReaction.id),
            )
            .where(
                col(MappedReaction.logical_reaction_id) == logical_reaction_id,
                col(MappedReaction.project_id) == project_id,
                col(LogicalReaction.project_id) == project_id,
            )
        ).all()
    )
    pending = tuple(
        entity
        for entity in (
            *tuple(session.new),
            *tuple(session.info.get("_fast_pending_entities", ())),
        )
        if (
            isinstance(entity, MappedReaction)
            and entity.logical_reaction_id == logical_reaction_id
            and entity.project_id == project_id
        )
    )
    by_id = {
        _require_id(mapped_reaction, label="MappedReaction"): mapped_reaction
        for mapped_reaction in (*persisted, *pending)
        if isinstance(mapped_reaction.id, UUID)
    }
    result = tuple(
        sorted(
            by_id.values(),
            key=lambda mapped_reaction: (
                mapped_reaction.mapping_hash,
                str(_require_id(mapped_reaction, label="MappedReaction")),
            ),
        )
    )
    if topology_context is not None:
        topology_context.mapped_reactions_by_logical_reaction[logical_reaction_id] = result
        mapped_reaction_ids = {
            mapped_reaction_id
            for mapped_reaction in result
            if isinstance(mapped_reaction_id := mapped_reaction.id, UUID)
        }
        if mapped_reaction_ids:
            participant_rows = session.exec(
                select(MappedReactionParticipant)
                .join(
                    MappedReaction,
                    col(MappedReactionParticipant.mapped_reaction_id) == col(MappedReaction.id),
                )
                .where(
                    col(MappedReactionParticipant.mapped_reaction_id).in_(mapped_reaction_ids),
                    col(MappedReaction.project_id) == project_id,
                )
            ).all()
            participants_by_reaction: dict[UUID, list[MappedReactionParticipant]] = {
                mapped_reaction_id: [] for mapped_reaction_id in mapped_reaction_ids
            }
            for participant in participant_rows:
                if isinstance(
                    mapped_reaction_id := participant.mapped_reaction_id,
                    UUID,
                ):
                    participants_by_reaction[mapped_reaction_id].append(participant)
            pending_participants = tuple(
                entity
                for entity in (
                    *tuple(session.new),
                    *tuple(session.info.get("_fast_pending_entities", ())),
                )
                if isinstance(entity, MappedReactionParticipant)
                and entity.mapped_reaction_id in mapped_reaction_ids
            )
            for mapped_reaction_id in mapped_reaction_ids:
                if (
                    mapped_reaction_id
                    not in topology_context.mapped_reaction_participants_by_reaction
                ):
                    topology_context.mapped_reaction_participants_by_reaction[
                        mapped_reaction_id
                    ] = tuple(
                        (*participants_by_reaction[mapped_reaction_id],)
                        + tuple(
                            participant
                            for participant in pending_participants
                            if participant.mapped_reaction_id == mapped_reaction_id
                        )
                    )
    return result


def _memberships_for_concrete_topology(
    session: Session,
    concrete_topology_id: UUID,
    *,
    project_id: UUID,
    topology_context: Any | None = None,
) -> tuple[LogicalParticipantConcreteTopology, ...]:
    """Load concrete memberships, including fast-path rows not flushed yet."""

    if topology_context is not None:
        cached = topology_context.memberships_by_concrete_topology.get(concrete_topology_id)
        if cached is not None:
            return cast(tuple[LogicalParticipantConcreteTopology, ...], cached)
    persisted_rows = session.exec(
        select(
            LogicalParticipantConcreteTopology,
            LogicalReactionParticipant,
            LogicalReaction,
        )
        .join(
            LogicalReactionParticipant,
            col(LogicalReactionParticipant.id)
            == col(LogicalParticipantConcreteTopology.logical_reaction_participant_id),
        )
        .join(
            LogicalReaction,
            col(LogicalReaction.id) == col(LogicalReactionParticipant.logical_reaction_id),
        )
        .join(
            MolecularTopology,
            col(MolecularTopology.id)
            == col(LogicalParticipantConcreteTopology.concrete_topology_id),
        )
        .where(
            LogicalParticipantConcreteTopology.concrete_topology_id == concrete_topology_id,
            col(MolecularTopology.project_id) == project_id,
            col(MolecularTopology.project_id) == col(LogicalReaction.project_id),
            col(LogicalReaction.project_id) == project_id,
            col(MolecularTopology.project_id).is_not(None),
        )
    ).all()
    persisted_entities: list[LogicalParticipantConcreteTopology] = []
    for membership, logical_participant, logical_reaction in persisted_rows:
        # A concrete topology can be shared by many logical reactions within
        # one project. Load
        # the complete ownership chain in one query so expansion does not
        # trigger one participant SELECT and one reaction SELECT per member.
        set_committed_value(
            membership,
            "logical_reaction_participant",
            logical_participant,
        )
        set_committed_value(logical_participant, "logical_reaction", logical_reaction)
        persisted_entities.append(membership)
        if topology_context is not None:
            logical_participant_id = logical_participant.id
            if isinstance(logical_participant_id, UUID):
                topology_context.logical_participants_by_id[logical_participant_id] = (
                    logical_participant
                )
    persisted = tuple(persisted_entities)
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
    result = tuple(by_id.values())
    if topology_context is not None:
        topology_context.memberships_by_concrete_topology[concrete_topology_id] = result
    return result


def _logical_participants_for_reaction(
    session: Session,
    logical_reaction_id: UUID,
    *,
    project_id: UUID,
    topology_context: Any | None = None,
) -> tuple[LogicalReactionParticipant, ...]:
    """Load one logical reaction's participants once per persistence batch."""

    if topology_context is not None:
        cached = topology_context.logical_participants_by_logical_reaction.get(logical_reaction_id)
        if cached is not None:
            return cast(tuple[LogicalReactionParticipant, ...], cached)
    participants = tuple(
        session.exec(
            select(LogicalReactionParticipant)
            .join(
                LogicalReaction,
                col(LogicalReactionParticipant.logical_reaction_id) == col(LogicalReaction.id),
            )
            .where(
                col(LogicalReactionParticipant.logical_reaction_id) == logical_reaction_id,
                col(LogicalReaction.project_id) == project_id,
            )
        ).all()
    )
    if topology_context is not None:
        topology_context.logical_participants_by_logical_reaction[logical_reaction_id] = (
            participants
        )
        topology_context.logical_participants_by_id.update(
            {
                participant_id: participant
                for participant in participants
                if isinstance(
                    participant_id := participant.id,
                    UUID,
                )
            }
        )
    return participants


def _logical_participant_for_membership(
    session: Session,
    membership: LogicalParticipantConcreteTopology,
    *,
    topology_context: Any | None = None,
) -> LogicalReactionParticipant:
    """Resolve a membership's logical participant through the batch cache."""

    participant_id = membership.logical_reaction_participant_id
    logical_participant = None
    if topology_context is not None:
        logical_participant = topology_context.logical_participants_by_id.get(participant_id)
    if logical_participant is None:
        logical_participant = membership.logical_reaction_participant
    if logical_participant is None:
        logical_participant = session.get(LogicalReactionParticipant, participant_id)
    if logical_participant is None:  # pragma: no cover - protected by the FK
        raise RuntimeError("Concrete membership has no logical participant")
    if topology_context is not None:
        topology_context.logical_participants_by_id[participant_id] = logical_participant
    return logical_participant


def _molecular_topology_by_id(
    session: Session,
    topology_id: UUID,
    *,
    project_id: UUID,
    topology_context: Any | None = None,
) -> MolecularTopology | None:
    """Resolve a topology through the batch cache before consulting SQL."""

    if topology_context is not None:
        cached = topology_context.molecular_topologies_by_id.get(topology_id)
        if cached is not None:
            if cached.project_id != project_id:
                return None
            return cast(MolecularTopology, cached)
        for candidate in topology_context.topologies_by_identity.values():
            if candidate.id == topology_id:
                if candidate.project_id != project_id:
                    return None
                topology_context.molecular_topologies_by_id[topology_id] = candidate
                return cast(MolecularTopology, candidate)
    topology = cast(
        MolecularTopology | None,
        session.exec(
            select(MolecularTopology).where(
                col(MolecularTopology.id) == topology_id,
                col(MolecularTopology.project_id) == project_id,
            )
        ).first(),
    )
    if topology is None:
        topology = next(
            (
                entity
                for entity in (
                    *tuple(session.new),
                    *tuple(session.info.get("_fast_pending_entities", ())),
                )
                if (
                    isinstance(entity, MolecularTopology)
                    and entity.id == topology_id
                    and entity.project_id == project_id
                )
            ),
            None,
        )
    if topology is not None and topology_context is not None:
        topology_context.molecular_topologies_by_id[topology_id] = topology
    return topology


def _complete_mapped_reaction(
    session: Session,
    mapped_reaction: MappedReaction,
    source_participants: tuple[MappedReactionParticipant, ...],
    *,
    topology_context: Any | None = None,
) -> bool:
    """Return whether one mapped reaction is safe to use as a transfer seed."""

    project_id = _require_project_owner(mapped_reaction, label="MappedReaction")
    logical_participants = _logical_participants_for_reaction(
        session,
        mapped_reaction.logical_reaction_id,
        project_id=project_id,
        topology_context=topology_context,
    )
    if len(source_participants) != len(logical_participants):
        return False
    if not source_participants:
        return False
    logical_participants_by_id = (
        topology_context.logical_participants_by_id
        if topology_context is not None
        else {
            participant.id: participant
            for participant in logical_participants
            if isinstance(participant.id, UUID)
        }
    )
    side_maps: dict[LogicalReactionParticipantSide, set[int]] = {}
    for participant in source_participants:
        logical_participant = logical_participants_by_id.get(
            participant.logical_reaction_participant_id
        )
        if logical_participant is None:
            logical_participant = session.get(
                LogicalReactionParticipant,
                participant.logical_reaction_participant_id,
            )
        if logical_participant is None:
            return False
        concrete_topology = None
        topology_id = participant.concrete_topology_id or logical_participant.topology_id
        if topology_id is not None:
            concrete_topology = _molecular_topology_by_id(
                session,
                topology_id,
                project_id=project_id,
                topology_context=topology_context,
            )
        if concrete_topology is None:
            concrete_topology = logical_participant.topology
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
    *,
    topology_context: Any | None = None,
) -> LogicalReactionParticipant:
    logical_participant = None
    if topology_context is not None:
        logical_participant = topology_context.logical_participants_by_id.get(
            participant.logical_reaction_participant_id
        )
    if logical_participant is None:
        logical_participant = participant.logical_reaction_participant
    if logical_participant is None:  # pragma: no cover - protected by the FK
        raise RuntimeError("MappedReactionParticipant has no logical participant")
    if topology_context is not None and isinstance(logical_participant.id, UUID):
        topology_context.logical_participants_by_id[logical_participant.id] = logical_participant
    if logical_participant.topology is None:  # pragma: no cover - protected by the FK
        raise RuntimeError("LogicalReactionParticipant has no logical topology")
    return logical_participant


def _target_topologies_for_source(
    session: Session,
    source_participants: tuple[MappedReactionParticipant, ...],
    *,
    selected_logical_participant_id: UUID,
    selected_concrete_topology: MolecularTopology,
    project_id: UUID,
    topology_context: Any | None = None,
) -> dict[tuple[LogicalReactionParticipantSide, int], MolecularTopology]:
    target: dict[tuple[LogicalReactionParticipantSide, int], MolecularTopology] = {}
    if selected_concrete_topology.project_id != project_id:
        raise ValueError("selected concrete topology is outside the reaction project")
    for source_participant in source_participants:
        logical_participant = _logical_participant(
            session,
            source_participant,
            topology_context=topology_context,
        )
        logical_participant_id = _require_id(
            logical_participant,
            label="LogicalReactionParticipant",
        )
        if logical_participant_id == selected_logical_participant_id:
            concrete_topology = selected_concrete_topology
        elif source_participant.concrete_topology_id is not None:
            resolved_topology = _molecular_topology_by_id(
                session,
                source_participant.concrete_topology_id,
                project_id=project_id,
                topology_context=topology_context,
            )
            concrete_topology = (
                resolved_topology
                if resolved_topology is not None
                else _resolve_topology_value(
                    session,
                    source_participant.concrete_topology_id,
                )
            )
        else:
            # Compatibility for rows created before concrete_topology_id was
            # added.  The migration backfills normal rows, but this fallback
            # keeps manually repaired legacy rows readable.
            concrete_topology = logical_participant.topology
        if concrete_topology.project_id != project_id:
            raise ValueError("reaction mapping source topology crosses project boundary")
        target[(source_participant.side, source_participant.template_index)] = concrete_topology
    return target


def _mapped_reaction_has_selected_topology(
    topology_context: Any | None,
    source_participants: tuple[MappedReactionParticipant, ...],
    *,
    selected_logical_participant_id: UUID,
    selected_concrete_topology_id: UUID,
) -> bool:
    for participant in source_participants:
        if participant.logical_reaction_participant_id != selected_logical_participant_id:
            continue
        current_topology_id = participant.concrete_topology_id
        if current_topology_id is None:
            logical_participant = None
            if topology_context is not None:
                logical_participant = topology_context.logical_participants_by_id.get(
                    participant.logical_reaction_participant_id
                )
            if logical_participant is None:
                logical_participant = participant.logical_reaction_participant
            if logical_participant is None:
                continue
            current_topology_id = logical_participant.topology_id
        return current_topology_id == selected_concrete_topology_id
    return False


def ensure_mapped_reactions_for_concrete_topology(
    session: Session,
    concrete_topology: MolecularTopology,
    *,
    topology_context: Any | None = None,
    reconciliation_cache: ReconciliationBatchCache | None = None,
    refresh_thermodynamics: bool = True,
    skip_topology_ids: set[UUID] | None = None,
) -> tuple[MappedReaction, ...]:
    """Create strict mapped reactions for a newly discovered concrete member.

    Membership is always recorded first.  A mapped reaction is created only
    when an existing mapped reaction under the same logical reaction supplies
    a complete mapping template; a topology with no such template remains a
    concrete member only.
    """

    concrete_topology_id = _require_id(concrete_topology, label="MolecularTopology")
    project_id = _require_project_owner(concrete_topology, label="MolecularTopology")
    if skip_topology_ids is not None and concrete_topology_id in skip_topology_ids:
        return ()
    ensured_memberships = ensure_concrete_topology_memberships(session, concrete_topology)
    if topology_context is not None:
        # ``ensure_concrete_topology_memberships`` has just enumerated the
        # complete membership set for this topology. Reuse those ORM objects
        # instead of querying the same set again before expansion.
        topology_context.memberships_by_concrete_topology[concrete_topology_id] = (
            ensured_memberships
        )
    memberships = _memberships_for_concrete_topology(
        session,
        concrete_topology_id,
        project_id=project_id,
        topology_context=topology_context,
    )
    created_or_reused: dict[UUID, MappedReaction] = {}
    for membership in memberships:
        logical_participant = _logical_participant_for_membership(
            session,
            membership,
            topology_context=topology_context,
        )
        logical_participant_id = _require_id(
            logical_participant,
            label="LogicalReactionParticipant",
        )
        logical_reaction = logical_participant.logical_reaction
        logical_reaction_id = _require_id(logical_reaction, label="LogicalReaction")
        mapped_reactions = _mapped_reactions_for_logical_reaction(
            session,
            logical_reaction_id,
            project_id=project_id,
            topology_context=topology_context,
        )
        for source_mapped_reaction in mapped_reactions:
            source_participants = _source_participants(
                session,
                source_mapped_reaction,
                topology_context=topology_context,
            )
            if not _complete_mapped_reaction(
                session,
                source_mapped_reaction,
                source_participants,
                topology_context=topology_context,
            ):
                continue
            if _mapped_reaction_has_selected_topology(
                topology_context,
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
                project_id=project_id,
                topology_context=topology_context,
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
                        _logical_participant(
                            session,
                            participant,
                            topology_context=topology_context,
                        ).topology,
                        label="MolecularTopology",
                    )
                    for participant in source_participants
                },
                concrete_topology_ids_by_template=transferred.concrete_topologies_by_template,
                precomputed_mapped_smiles_by_template=transferred.mapped_smiles_by_template,
                topology_context=topology_context,
            )
            mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
            created_or_reused[mapped_reaction_id] = mapped_reaction
            if topology_context is not None:
                current_reactions = topology_context.mapped_reactions_by_logical_reaction.get(
                    logical_reaction_id,
                    (),
                )
                if all(existing.id != mapped_reaction_id for existing in current_reactions):
                    topology_context.mapped_reactions_by_logical_reaction[logical_reaction_id] = (
                        *current_reactions,
                        mapped_reaction,
                    )
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
    if skip_topology_ids is not None:
        skip_topology_ids.add(concrete_topology_id)
    return tuple(created_or_reused.values())


def ensure_mapped_reactions_for_logical_reaction(
    session: Session,
    logical_reaction: LogicalReaction,
    *,
    topology_context: Any | None = None,
    reconciliation_cache: ReconciliationBatchCache | None = None,
    refresh_thermodynamics: bool = True,
    processed_topology_ids: set[UUID] | None = None,
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
                processed_topology_ids=processed_topology_ids,
            )
        finally:
            session.info["tricycle_fast_insert"] = previous_fast_insert

    logical_reaction_id = _require_id(logical_reaction, label="LogicalReaction")
    project_id = _require_project_owner(logical_reaction, label="LogicalReaction")
    participants = tuple(
        session.exec(
            select(LogicalReactionParticipant)
            .join(
                LogicalReaction,
                col(LogicalReactionParticipant.logical_reaction_id) == col(LogicalReaction.id),
            )
            .where(
                col(LogicalReactionParticipant.logical_reaction_id) == logical_reaction_id,
                col(LogicalReaction.project_id) == project_id,
            )
            .order_by(
                col(LogicalReactionParticipant.side),
                col(LogicalReactionParticipant.participant_index),
            )
        ).all()
    )
    for participant in participants:
        # The fixed-point expansion repeatedly reads this relationship for
        # every concrete membership.  The owning reaction is already the
        # function argument, so mark it as loaded without dirtying the ORM
        # object or issuing one lazy SELECT per participant.
        set_committed_value(participant, "logical_reaction", logical_reaction)
    if topology_context is not None:
        topology_context.logical_participants_by_logical_reaction[logical_reaction_id] = (
            participants
        )
        topology_context.logical_participants_by_id.update(
            {
                participant_id: participant
                for participant in participants
                if isinstance(
                    participant_id := participant.id,
                    UUID,
                )
            }
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
            .join(
                LogicalReaction,
                col(LogicalReactionParticipant.logical_reaction_id) == col(LogicalReaction.id),
            )
            .where(
                col(LogicalReactionParticipant.logical_reaction_id) == logical_reaction_id,
                col(LogicalReaction.project_id) == project_id,
            )
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
            project_id=project_id,
            topology_context=topology_context,
        )
    }
    created: dict[UUID, MappedReaction] = {}

    # Keep revisiting the materialized candidates until every newly-created
    # source mapping has been used.  This covers concrete combinations across
    # multiple reaction participants without enumerating theoretical variants.
    while True:
        previous_count = len(known_ids)
        for concrete_topology_id in sorted(concrete_topology_ids, key=str):
            concrete_topology = _molecular_topology_by_id(
                session,
                concrete_topology_id,
                project_id=project_id,
                topology_context=topology_context,
            )
            if concrete_topology is None:
                raise ValueError("reaction expansion topology crosses project boundary")
            materialized_reactions = ensure_mapped_reactions_for_concrete_topology(
                session,
                concrete_topology,
                topology_context=topology_context,
                reconciliation_cache=reconciliation_cache,
                refresh_thermodynamics=refresh_thermodynamics,
            )
            if processed_topology_ids is not None:
                processed_topology_ids.add(concrete_topology_id)
            for mapped_reaction in materialized_reactions:
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
