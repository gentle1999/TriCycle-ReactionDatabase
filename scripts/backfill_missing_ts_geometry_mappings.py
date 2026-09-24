#!/usr/bin/env python3
"""Backfill missing TS geometry mappings from persisted frame permutations."""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from rdkit import Chem
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlalchemy.orm import selectinload
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.dtos import MappedReactionNodeGeometryMappingRecord
from tricycle_reaction_db.application.services.reactions import (
    atom_maps_from_source_order,
    mapped_smiles_for_geometry,
    persist_mapped_reaction_node_geometry_mapping,
)
from tricycle_reaction_db.core.chemistry_config import (
    REACTION_TS_GEOMETRY_LINK_METHOD,
    REACTION_TS_GEOMETRY_LINK_POLICY_VERSION,
)
from tricycle_reaction_db.db.models import (
    CalculationFrame,
    Geometry,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionNodeGeometryMapping,
    MappedReactionParticipant,
)
from tricycle_reaction_db.db.session import (
    dispose_engine,
    get_database_status,
    session_factory,
)
from tricycle_reaction_db.domain.enums import MappedReactionNodeRole
from tricycle_reaction_db.domain.reaction_frames import is_transition_state_frame_eligible

EXPECTED_DATABASE = "tricycle_cycloaddition_n_inversion_minimal_20260904"
EXPECTED_MISSING_MAPPING_COUNT = 62


@dataclass(frozen=True, slots=True)
class BackfillSummary:
    binding_count: int
    geometry_count: int
    reaction_count: int
    eligible_frame_min: int
    eligible_frame_max: int


def _participants_atom_signatures(
    session: Session,
    reaction_ids: set[UUID],
) -> dict[UUID, dict[int, tuple[int, int]]]:
    signatures_by_reaction: dict[UUID, dict[int, tuple[int, int]]] = defaultdict(dict)
    participants = session.exec(
        select(MappedReactionParticipant).where(
            col(MappedReactionParticipant.mapped_reaction_id).in_(reaction_ids)
        )
    ).all()
    for participant in participants:
        molecule = Chem.MolFromSmiles(participant.mapped_smiles, sanitize=False)
        if molecule is None:
            raise ValueError(
                f"cannot parse mapped participant for reaction {participant.mapped_reaction_id}"
            )
        reaction_signatures = signatures_by_reaction[participant.mapped_reaction_id]
        for atom in molecule.GetAtoms():  # type: ignore[no-untyped-call]
            atom_map = int(atom.GetAtomMapNum())
            if atom_map <= 0:
                continue
            signature = (int(atom.GetAtomicNum()), int(atom.GetIsotope()))
            existing = reaction_signatures.get(atom_map)
            if existing is not None and existing != signature:
                raise ValueError(
                    "mapped reaction assigns inconsistent element/isotope signatures "
                    f"to atom map {atom_map} in reaction {participant.mapped_reaction_id}"
                )
            reaction_signatures[atom_map] = signature
    return signatures_by_reaction


def _load_missing_bindings(session: Session) -> list[MappedReactionNodeGeometry]:
    statement = (
        select(MappedReactionNodeGeometry)
        .join(
            MappedReactionNode,
            col(MappedReactionNode.id) == col(MappedReactionNodeGeometry.mapped_reaction_node_id),
        )
        .outerjoin(
            MappedReactionNodeGeometryMapping,
            col(MappedReactionNodeGeometryMapping.mapped_reaction_node_geometry_id)
            == col(MappedReactionNodeGeometry.id),
        )
        .where(
            col(MappedReactionNode.role) == MappedReactionNodeRole.TRANSITION_STATE,
            col(MappedReactionNodeGeometryMapping.id).is_(None),
        )
        .options(
            selectinload(cast(Any, MappedReactionNodeGeometry.geometry)).selectinload(
                cast(Any, Geometry.topology)
            ),
            selectinload(cast(Any, MappedReactionNodeGeometry.mapped_reaction_node)).selectinload(
                cast(Any, MappedReactionNode.mapped_reaction)
            ),
        )
        .order_by(col(MappedReactionNodeGeometry.id))
        .with_for_update(of=MappedReactionNodeGeometry)
    )
    return list(session.exec(statement).all())


def _backfill_in_transaction(sync_session: SQLAlchemySession) -> BackfillSummary:
    session = cast(Session, sync_session)
    bindings = _load_missing_bindings(session)
    if len(bindings) != EXPECTED_MISSING_MAPPING_COUNT:
        raise ValueError(
            f"expected {EXPECTED_MISSING_MAPPING_COUNT} missing TS mappings, "
            f"found {len(bindings)}; "
            "refusing to modify an unexpected target set"
        )

    geometries_by_id: dict[UUID, Geometry] = {}
    reactions_by_id: dict[UUID, Any] = {}
    for binding in bindings:
        if binding.id is None or binding.geometry.id is None:
            raise ValueError("missing binding or Geometry primary key")
        node = binding.mapped_reaction_node
        reaction = node.mapped_reaction
        if node.role is not MappedReactionNodeRole.TRANSITION_STATE:
            raise ValueError(f"binding {binding.id} is no longer attached to a TS node")
        if reaction.id is None:
            raise ValueError(f"binding {binding.id} has no mapped-reaction identity")
        if binding.geometry.project_id != reaction.project_id:
            raise ValueError(f"binding {binding.id} crosses project ownership")
        geometries_by_id[binding.geometry.id] = binding.geometry
        reactions_by_id[reaction.id] = reaction

    frames_by_geometry: dict[UUID, list[CalculationFrame]] = defaultdict(list)
    frame_rows = session.exec(
        select(CalculationFrame).where(col(CalculationFrame.geometry_id).in_(set(geometries_by_id)))
    ).all()
    for frame in frame_rows:
        if is_transition_state_frame_eligible(frame.frame_role):
            frames_by_geometry[frame.geometry_id].append(frame)

    signatures_by_reaction = _participants_atom_signatures(
        session,
        set(reactions_by_id),
    )
    vectors_by_geometry: dict[UUID, set[tuple[int, ...]]] = defaultdict(set)
    for geometry_id, geometry in geometries_by_id.items():
        frames = frames_by_geometry.get(geometry_id, [])
        if not frames:
            raise ValueError(f"Geometry {geometry_id} has no eligible source frame")
        for frame in frames:
            vector = atom_maps_from_source_order(
                geometry,
                range(1, geometry.atom_count + 1),
                frame.observed_to_geometry_atom_indices,
            )
            vectors_by_geometry[geometry_id].add(tuple(vector))
        if len(vectors_by_geometry[geometry_id]) != 1:
            raise ValueError(
                f"Geometry {geometry_id} has conflicting frame-derived atom-map vectors"
            )

    eligible_frame_counts = [
        len(frames_by_geometry[geometry_id]) for geometry_id in geometries_by_id
    ]
    for binding in bindings:
        geometry = binding.geometry
        reaction = binding.mapped_reaction_node.mapped_reaction
        assert geometry.id is not None and reaction.id is not None
        vector = list(next(iter(vectors_by_geometry[geometry.id])))
        participant_signatures = signatures_by_reaction.get(reaction.id, {})
        for geometry_index, atom_map in enumerate(vector):
            atom = geometry.mol.GetAtomWithIdx(geometry_index)
            observed_signature = (int(atom.GetAtomicNum()), int(atom.GetIsotope()))
            expected_signature = participant_signatures.get(atom_map)
            if expected_signature is None or expected_signature != observed_signature:
                raise ValueError(
                    f"binding {binding.id} map {atom_map} has no matching element/isotope "
                    "in its mapped-reaction participants"
                )

        record = MappedReactionNodeGeometryMappingRecord(
            geometry_atom_map_numbers=vector,
            mapped_smiles=mapped_smiles_for_geometry(
                geometry,
                vector,
                include_stereochemistry=False,
            ),
            mapping_method=REACTION_TS_GEOMETRY_LINK_METHOD,
            mapping_version=REACTION_TS_GEOMETRY_LINK_POLICY_VERSION,
            verified=True,
        )
        persisted = persist_mapped_reaction_node_geometry_mapping(
            session,
            binding,
            record,
        )
        if not persisted.verified or persisted.geometry_atom_map_numbers != vector:
            raise ValueError(f"persistence returned an unexpected mapping for binding {binding.id}")

    session.flush()
    return BackfillSummary(
        binding_count=len(bindings),
        geometry_count=len(geometries_by_id),
        reaction_count=len(reactions_by_id),
        eligible_frame_min=min(eligible_frame_counts),
        eligible_frame_max=max(eligible_frame_counts),
    )


async def _count_missing_bindings() -> int:
    statement = (
        select(MappedReactionNodeGeometry.id)
        .join(
            MappedReactionNode,
            col(MappedReactionNode.id) == col(MappedReactionNodeGeometry.mapped_reaction_node_id),
        )
        .outerjoin(
            MappedReactionNodeGeometryMapping,
            col(MappedReactionNodeGeometryMapping.mapped_reaction_node_geometry_id)
            == col(MappedReactionNodeGeometry.id),
        )
        .where(
            col(MappedReactionNode.role) == MappedReactionNodeRole.TRANSITION_STATE,
            col(MappedReactionNodeGeometryMapping.id).is_(None),
        )
    )
    async with session_factory() as session:
        return len((await session.exec(statement)).all())


async def main(*, apply: bool) -> None:
    try:
        database_status = await get_database_status()
        if database_status["database"] != EXPECTED_DATABASE:
            raise RuntimeError(
                f"connected database {database_status['database']!r} does not match "
                f"the approved target {EXPECTED_DATABASE!r}"
            )

        async with session_factory() as session:
            await session.begin()
            try:
                summary = await session.run_sync(_backfill_in_transaction)
                if apply:
                    await session.commit()
                else:
                    await session.rollback()
            except BaseException:
                await session.rollback()
                raise

        remaining = await _count_missing_bindings() if apply else EXPECTED_MISSING_MAPPING_COUNT
        print(f"database={database_status['database']}")
        print(f"mode={'apply' if apply else 'dry-run'}")
        print(f"backfilled_or_validated={summary.binding_count}")
        print(f"distinct_geometries={summary.geometry_count}")
        print(f"distinct_mapped_reactions={summary.reaction_count}")
        print(
            "eligible_source_frames_per_geometry="
            f"{summary.eligible_frame_min}..{summary.eligible_frame_max}"
        )
        print(f"mapping_version={REACTION_TS_GEOMETRY_LINK_POLICY_VERSION}")
        print(f"remaining_missing_ts_mappings={remaining}")
        if apply and remaining != 0:
            raise RuntimeError("post-commit verification found missing TS mappings")
    finally:
        await dispose_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="commit the all-or-nothing backfill (default validates and rolls back)",
    )
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
