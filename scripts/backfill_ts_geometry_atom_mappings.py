"""Repair or quarantine invalid verified TS geometry atom-map bindings."""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import selectinload
from sqlmodel import col, select

from tricycle_reaction_db.application.services.mapped_geometry_atom_order import (
    mapped_reaction_atom_elements,
    validate_geometry_atom_map_elements,
)
from tricycle_reaction_db.application.services.reaction_commands import (
    _atom_maps_in_persisted_topology_order,
)
from tricycle_reaction_db.application.services.reactions import (
    atom_maps_from_source_order,
    mapped_smiles_for_geometry,
)
from tricycle_reaction_db.core.chemistry_config import (
    REACTION_TS_GEOMETRY_LINK_METHOD,
    REACTION_TS_GEOMETRY_LINK_POLICY_VERSION,
)
from tricycle_reaction_db.db.models import (
    CalculationFrame,
    Geometry,
    LogicalReactionParticipant,
    MappedReaction,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionNodeGeometryMapping,
    MappedReactionParticipant,
    MolecularTopology,
    TransitionStateInference,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import (
    MappedReactionNodeRole,
    TransitionStateInferenceStatus,
)

PAGE_SIZE = 500
STATEMENT_TIMEOUT_MS = 300_000
UNRESOLVED_MAPPING_METHOD = "unresolved-map-transfer"
UNRESOLVED_MAPPING_VERSION = "reaction-ts-geometry-link-unverified-v1"


@dataclass(slots=True)
class GeometryMapRepair:
    mapping: MappedReactionNodeGeometryMapping
    geometry: Geometry
    reaction: MappedReaction
    candidate_atom_maps: list[int] | None
    source_count: int
    unresolved_reason: str | None = None


def _source_order_atom_maps(
    geometry: Geometry,
    observed_to_geometry_atom_indices: list[int],
) -> list[int]:
    atom_count = geometry.atom_count
    if len(observed_to_geometry_atom_indices) != atom_count or sorted(
        observed_to_geometry_atom_indices
    ) != list(range(atom_count)):
        raise ValueError("CalculationFrame source-to-Geometry indices are not a permutation")
    return atom_maps_from_source_order(
        geometry,
        list(range(1, atom_count + 1)),
        observed_to_geometry_atom_indices,
    )


def _reaction_map_translation(
    *,
    source_reaction_id: UUID,
    target_reaction_id: UUID,
    source_participants: dict[tuple[Any, int], tuple[UUID, UUID, list[int]]],
    target_participants: dict[tuple[Any, int], tuple[UUID, UUID, list[int]]],
    topologies: dict[UUID, MolecularTopology],
) -> dict[int, int] | None:
    """Translate source reaction maps to target maps through participant graphs."""

    if source_reaction_id == target_reaction_id:
        return {}
    if not source_participants or source_participants.keys() != target_participants.keys():
        return None

    source_to_target: dict[int, int] = {}
    target_to_source: dict[int, int] = {}
    for key, source in source_participants.items():
        target = target_participants[key]
        if source[0] != target[0]:
            return None
        source_topology = topologies.get(source[1])
        target_topology = topologies.get(target[1])
        if source_topology is None or target_topology is None:
            return None
        target_maps = target[2]
        if source[1] != target[1]:
            try:
                # Render the target map vector in source topology order so
                # corresponding atoms can be paired by their topology graph.
                target_maps = _atom_maps_in_persisted_topology_order(
                    cast(MolecularTopology, target_topology),
                    source_topology,
                    target_maps,
                )
            except ValueError:
                return None
        if len(source[2]) != len(target_maps):
            return None
        for source_map, target_map in zip(source[2], target_maps, strict=True):
            prior_target = source_to_target.setdefault(source_map, target_map)
            prior_source = target_to_source.setdefault(target_map, source_map)
            if prior_target != target_map or prior_source != source_map:
                # Different reaction mappings do not define a unique
                # geometry-map transfer, even if their logical graphs match.
                return None
    return source_to_target


async def _repair_candidates(
    session: Any,
) -> tuple[list[GeometryMapRepair], int]:
    """Find every invalid verified mapping and preflight a unique safe vector."""

    await session.exec(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
    invalid_rows: list[tuple[Any, ...]] = []
    scanned = 0
    reaction_elements_by_smiles: dict[str, dict[int, int]] = {}
    last_binding_id: UUID | None = None
    while True:
        statement = (
            select(
                MappedReactionNodeGeometry,
                MappedReactionNodeGeometryMapping,
                MappedReaction,
                Geometry,
            )
            .join(
                MappedReactionNode,
                col(MappedReactionNode.id)
                == col(MappedReactionNodeGeometry.mapped_reaction_node_id),
            )
            .join(
                MappedReaction,
                col(MappedReaction.id) == col(MappedReactionNode.mapped_reaction_id),
            )
            .join(Geometry, col(Geometry.id) == col(MappedReactionNodeGeometry.geometry_id))
            .join(
                MappedReactionNodeGeometryMapping,
                col(MappedReactionNodeGeometryMapping.mapped_reaction_node_geometry_id)
                == col(MappedReactionNodeGeometry.id),
            )
            .where(
                col(MappedReactionNode.role) == MappedReactionNodeRole.TRANSITION_STATE,
                col(MappedReactionNodeGeometryMapping.verified).is_(True),
            )
            .options(
                selectinload(
                    cast(Any, MappedReactionNodeGeometry.geometry)
                ).selectinload(cast(Any, Geometry.topology))
            )
            .order_by(col(MappedReactionNodeGeometry.id))
            .limit(PAGE_SIZE)
        )
        if last_binding_id is not None:
            statement = statement.where(
                col(MappedReactionNodeGeometry.id) > last_binding_id
            )
        rows = (await session.exec(statement)).all()
        if not rows:
            break
        for binding, mapping, reaction, geometry in rows:
            scanned += 1
            last_binding_id = binding.id
            try:
                reaction_elements = reaction_elements_by_smiles.get(
                    reaction.mapped_reaction_smiles
                )
                if reaction_elements is None:
                    reaction_elements = mapped_reaction_atom_elements(
                        reaction.mapped_reaction_smiles
                    )
                    reaction_elements_by_smiles[reaction.mapped_reaction_smiles] = (
                        reaction_elements
                    )
                validate_geometry_atom_map_elements(
                    geometry.mol,
                    mapping.geometry_atom_map_numbers,
                    reaction.mapped_reaction_smiles,
                    reaction_elements=reaction_elements,
                )
            except ValueError:
                invalid_rows.append((mapping, reaction, geometry))
        if scanned % 5_000 == 0:
            print(f"checked {scanned} verified TS geometry mappings", flush=True)

    if not invalid_rows:
        return [], scanned

    geometry_by_id = {geometry.id: geometry for _, _, geometry in invalid_rows}
    bad_geometry_ids = set(geometry_by_id)
    source_rows = (
        await session.exec(
            select(
                TransitionStateInference.mapped_reaction_id,
                CalculationFrame.geometry_id,
                CalculationFrame.observed_to_geometry_atom_indices,
            )
            .join(
                CalculationFrame,
                col(CalculationFrame.id)
                == col(TransitionStateInference.calculation_frame_id),
            )
            .where(
                TransitionStateInference.status == TransitionStateInferenceStatus.SUCCEEDED,
                col(TransitionStateInference.mapped_reaction_id).is_not(None),
                col(CalculationFrame.geometry_id).in_(bad_geometry_ids),
            )
        )
    ).all()
    sources_by_geometry: dict[UUID, list[tuple[UUID, list[int]]]] = defaultdict(list)
    for reaction_id, geometry_id, observed_to_geometry in source_rows:
        if reaction_id is None or geometry_id not in geometry_by_id:
            continue
        try:
            atom_maps = _source_order_atom_maps(
                geometry_by_id[geometry_id],
                list(observed_to_geometry),
            )
        except ValueError:
            continue
        sources_by_geometry[geometry_id].append((reaction_id, atom_maps))

    reaction_ids = {
        reaction.id for _, reaction, _ in invalid_rows if reaction.id is not None
    } | {reaction_id for rows in sources_by_geometry.values() for reaction_id, _ in rows}
    reactions = (
        await session.exec(
            select(MappedReaction).where(col(MappedReaction.id).in_(reaction_ids))
        )
    ).all()
    reactions_by_id = {reaction.id: reaction for reaction in reactions}
    participant_rows = (
        await session.exec(
            select(MappedReactionParticipant, LogicalReactionParticipant.topology_id)
            .join(
                LogicalReactionParticipant,
                col(LogicalReactionParticipant.id)
                == col(MappedReactionParticipant.logical_reaction_participant_id),
            )
            .where(col(MappedReactionParticipant.mapped_reaction_id).in_(reaction_ids))
        )
    ).all()
    participants_by_reaction: dict[
        UUID,
        dict[tuple[Any, int], tuple[UUID, UUID, list[int]]],
    ] = defaultdict(dict)
    topology_ids: set[UUID] = set()
    for participant, logical_topology_id in participant_rows:
        topology_id = participant.concrete_topology_id or logical_topology_id
        if topology_id is None:
            continue
        topology_ids.add(topology_id)
        participants_by_reaction[participant.mapped_reaction_id][
            (participant.side, participant.template_index)
        ] = (
            participant.logical_reaction_participant_id,
            topology_id,
            list(participant.atom_map_numbers),
        )
    topologies = (
        await session.exec(
            select(MolecularTopology).where(col(MolecularTopology.id).in_(topology_ids))
        )
    ).all()
    topologies_by_id = {topology.id: topology for topology in topologies}

    repairs: list[GeometryMapRepair] = []
    for mapping, target_reaction, geometry in invalid_rows:
        if target_reaction.id is None or geometry.id is None:
            raise RuntimeError("TS geometry mapping is missing a persisted identity")
        candidates: set[tuple[int, ...]] = set()
        geometry_sources = sources_by_geometry.get(geometry.id, ())
        for source_reaction_id, source_atom_maps in geometry_sources:
            source_reaction = reactions_by_id.get(source_reaction_id)
            if source_reaction is None:
                continue
            try:
                validate_geometry_atom_map_elements(
                    geometry.mol,
                    source_atom_maps,
                    source_reaction.mapped_reaction_smiles,
                )
            except ValueError:
                continue
            if source_reaction_id == target_reaction.id:
                candidate = source_atom_maps
            else:
                translation = _reaction_map_translation(
                    source_reaction_id=source_reaction_id,
                    target_reaction_id=target_reaction.id,
                    source_participants=participants_by_reaction.get(
                        source_reaction_id, {}
                    ),
                    target_participants=participants_by_reaction.get(
                        target_reaction.id, {}
                    ),
                    topologies=topologies_by_id,
                )
                if translation is None or not set(source_atom_maps).issubset(translation):
                    continue
                candidate = [translation[map_number] for map_number in source_atom_maps]
            try:
                validate_geometry_atom_map_elements(
                    geometry.mol,
                    candidate,
                    target_reaction.mapped_reaction_smiles,
                )
            except ValueError:
                continue
            candidates.add(tuple(candidate))

        if len(candidates) == 1:
            repairs.append(
                GeometryMapRepair(
                    mapping=mapping,
                    geometry=geometry,
                    reaction=target_reaction,
                    candidate_atom_maps=list(next(iter(candidates))),
                    source_count=len(geometry_sources),
                )
            )
        else:
            repairs.append(
                GeometryMapRepair(
                    mapping=mapping,
                    geometry=geometry,
                    reaction=target_reaction,
                    candidate_atom_maps=None,
                    source_count=len(geometry_sources),
                    unresolved_reason=(
                        "no unique source-to-reaction map transfer"
                        if candidates
                        else "no source-derived vector conserves the target reaction elements"
                    ),
                )
            )
    return repairs, scanned


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="persist the preflighted repairs in one transaction",
    )
    parser.add_argument(
        "--quarantine-unresolved",
        action="store_true",
        help="mark unresolved rows unverified so they cannot be exported as mappings",
    )
    args = parser.parse_args()
    if args.quarantine_unresolved and not args.apply:
        parser.error("--quarantine-unresolved requires --apply")

    async with session_factory() as session:
        repairs, scanned = await _repair_candidates(session)
        uniquely_repairable = [
            repair for repair in repairs if repair.candidate_atom_maps is not None
        ]
        unresolved = [repair for repair in repairs if repair.candidate_atom_maps is None]
        print(
            f"TS geometry mapping audit: scanned={scanned} invalid={len(repairs)} "
            f"repairable={len(uniquely_repairable)} unresolved={len(unresolved)}"
        )
        for repair in unresolved[:20]:
            print(
                f"unresolved binding={repair.mapping.mapped_reaction_node_geometry_id} "
                f"reaction={repair.reaction.id} geometry={repair.geometry.id} "
                f"sources={repair.source_count}: {repair.unresolved_reason}"
            )
        if not args.apply:
            await session.rollback()
            return
        if unresolved and not args.quarantine_unresolved:
            await session.rollback()
            raise SystemExit(
                "refusing partial write: pass --quarantine-unresolved to exclude ambiguous "
                "mappings from verified exports"
            )

        for repair in uniquely_repairable:
            candidate = cast(list[int], repair.candidate_atom_maps)
            repair.mapping.geometry_atom_map_numbers = candidate
            repair.mapping.mapped_smiles = mapped_smiles_for_geometry(
                repair.geometry,
                candidate,
                include_stereochemistry=False,
            )
            repair.mapping.mapping_method = REACTION_TS_GEOMETRY_LINK_METHOD
            repair.mapping.mapping_version = REACTION_TS_GEOMETRY_LINK_POLICY_VERSION
            repair.mapping.verified = True
            validate_geometry_atom_map_elements(
                repair.geometry.mol,
                candidate,
                repair.reaction.mapped_reaction_smiles,
            )
            session.add(repair.mapping)
        for repair in unresolved:
            repair.mapping.mapping_method = UNRESOLVED_MAPPING_METHOD
            repair.mapping.mapping_version = UNRESOLVED_MAPPING_VERSION
            repair.mapping.verified = False
            session.add(repair.mapping)
        await session.commit()
        print(
            f"TS geometry mapping repair committed: repaired={len(uniquely_repairable)} "
            f"quarantined={len(unresolved)}"
        )


if __name__ == "__main__":
    asyncio.run(main())
