"""Move stale participant references to calculation-backed canonical equivalents."""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.services._persistence import _require_id
from tricycle_reaction_db.application.services.canonical_atom_mapping import (
    canonical_atom_index_mapping,
)
from tricycle_reaction_db.application.services.reactions import mapped_smiles_for_topology
from tricycle_reaction_db.db.models import (
    CalculationFrame,
    Geometry,
    LogicalParticipantConcreteTopology,
    MappedReaction,
    MappedReactionParticipant,
    MolecularTopology,
)


def _maps_on_equivalent_topology(
    participant: MappedReactionParticipant,
    source: MolecularTopology,
    target: MolecularTopology,
) -> list[int] | None:
    if source.project_id != target.project_id:
        return None
    witness = canonical_atom_index_mapping(source.mol, target.mol)
    if witness is None or len(participant.atom_map_numbers) != len(witness):
        return None
    maps = [0] * len(witness)
    for source_index, target_index in enumerate(witness):
        maps[target_index] = participant.atom_map_numbers[source_index]
    # Preserve this reaction's labelled stereo graph, not just the map-free
    # topology. Canonical traversal is the only correspondence mechanism.
    if mapped_smiles_for_topology(target, maps) != participant.mapped_smiles:
        return None
    return maps


@dataclass(frozen=True)
class ParticipantTopologyReplacement:
    participant: MappedReactionParticipant
    topology: MolecularTopology
    atom_maps: list[int]


def observed_participant_topology_replacements(
    session: Session, mapped_reaction_ids: Sequence[UUID]
) -> tuple[ParticipantTopologyReplacement, ...]:
    """Find no-frame references with a verified calculation-backed replacement.

    A legacy topology and its canonical replacement may both be concrete members
    of one logical slot. Reusing a mapped reaction must not strand its energies
    on the no-frame row. Existing calculation-backed participants are retained.
    """
    if not mapped_reaction_ids:
        return ()
    source = aliased(MolecularTopology)
    target = aliased(MolecularTopology)

    def has_frames(topology):
        return (
            select(Geometry.id)
            .join(CalculationFrame, col(CalculationFrame.geometry_id) == col(Geometry.id))
            .where(col(Geometry.topology_id) == topology.id)
            .exists()
        )

    rows = session.exec(
        select(MappedReactionParticipant, source, target)
        .join(
            MappedReaction,
            col(MappedReactionParticipant.mapped_reaction_id) == col(MappedReaction.id),
        )
        .join(source, col(MappedReactionParticipant.concrete_topology_id) == source.id)
        .join(
            LogicalParticipantConcreteTopology,
            col(LogicalParticipantConcreteTopology.logical_reaction_participant_id)
            == col(MappedReactionParticipant.logical_reaction_participant_id),
        )
        .join(target, col(LogicalParticipantConcreteTopology.concrete_topology_id) == target.id)
        .where(
            col(MappedReaction.id).in_(mapped_reaction_ids),
            source.id != target.id,
            source.project_id == col(MappedReaction.project_id),
            target.project_id == col(MappedReaction.project_id),
            ~has_frames(source),
            has_frames(target),
        )
        .order_by(col(MappedReactionParticipant.id), col(target.id))
    ).all()
    replacements: dict[UUID, ParticipantTopologyReplacement] = {}
    for participant, old_topology, new_topology in rows:
        participant_id = _require_id(participant, label="MappedReactionParticipant")
        if participant_id in replacements:
            continue
        maps = _maps_on_equivalent_topology(participant, old_topology, new_topology)
        if maps is not None:
            replacements[participant_id] = ParticipantTopologyReplacement(
                participant, new_topology, maps
            )
    return tuple(replacements.values())


def prefer_observed_participant_topologies(
    session: Session, mapped_reaction_ids: Sequence[UUID]
) -> tuple[UUID, ...]:
    """Apply verified replacements atomically with the ensuing profile refresh."""
    changed: list[UUID] = []
    for replacement in observed_participant_topology_replacements(session, mapped_reaction_ids):
        participant = replacement.participant
        participant.concrete_topology = replacement.topology
        participant.concrete_topology_id = replacement.topology.id
        participant.atom_map_numbers = replacement.atom_maps
        session.add(participant)
        changed.append(_require_id(participant, label="MappedReactionParticipant"))
    if changed:
        session.flush()
    return tuple(sorted(changed, key=str))
