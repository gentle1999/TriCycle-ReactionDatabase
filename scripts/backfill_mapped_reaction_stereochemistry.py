"""Repair persisted mapped-reaction SMILES after the lossless stereo fix.

The old serializer could persist an assigned E/Z value without the slash
directions needed to recover it from mapped SMILES.  This maintenance command
re-renders every persisted participant from its trusted topology and then
rebuilds the reaction-level projection from those participants.

The default mode is a read-only preflight. Use ``--apply`` only after the
reported collision checks are clean.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, cast

from sqlalchemy.orm import selectinload
from sqlmodel import select

from tricycle_reaction_db.application.services.reactions import mapped_smiles_for_topology
from tricycle_reaction_db.db.models import (
    Geometry,
    LogicalReactionParticipant,
    MappedReaction,
    MappedReactionNodeGeometry,
    MappedReactionNodeGeometryMapping,
    MappedReactionParticipant,
)
from tricycle_reaction_db.db.session import session_factory


@dataclass(slots=True)
class RepairPlan:
    participant_updates: list[tuple[MappedReactionParticipant, str]]
    node_mapping_updates: list[tuple[MappedReactionNodeGeometryMapping, str]]
    reaction_updates: list[tuple[MappedReaction, str, str, str | None]]
    errors: list[str]
    collisions: list[str]


def _expected_reaction_smiles(
    mapped_reaction: MappedReaction,
    expected_participants: dict[object, str],
) -> str:
    sides: dict[str, list[tuple[int, str]]] = {"reactant": [], "product": []}
    for participant in mapped_reaction.participants:
        participant_id = participant.id
        if participant_id is None or participant_id not in expected_participants:
            raise ValueError(f"missing expected participant projection for {participant.id}")
        side = participant.side.value
        if side not in sides:
            raise ValueError(f"unsupported mapped participant side: {side}")
        sides[side].append((participant.template_index, expected_participants[participant_id]))

    if not sides["reactant"] or not sides["product"]:
        raise ValueError("mapped reaction must have at least one reactant and product")
    reactants = ".".join(smiles for _, smiles in sorted(sides["reactant"]))
    products = ".".join(smiles for _, smiles in sorted(sides["product"]))
    old_parts = mapped_reaction.mapped_reaction_smiles.split(">")
    agents = old_parts[1] if len(old_parts) == 3 else ""
    return f"{reactants}>{agents}>{products}" if agents else f"{reactants}>>{products}"


async def _build_plan() -> RepairPlan:
    participant_updates: list[tuple[MappedReactionParticipant, str]] = []
    node_mapping_updates: list[tuple[MappedReactionNodeGeometryMapping, str]] = []
    reaction_updates: list[tuple[MappedReaction, str, str, str | None]] = []
    errors: list[str] = []

    async with session_factory() as session:
        participant_rows = (await session.exec(
            select(MappedReactionParticipant).options(
                selectinload(cast(Any, MappedReactionParticipant.logical_reaction_participant))
                .selectinload(cast(Any, LogicalReactionParticipant.topology))
            )
        )).all()
        expected_participants: dict[object, str] = {}
        for participant in participant_rows:
            try:
                if participant.id is None:
                    raise ValueError("persisted participant has no id")
                expected = mapped_smiles_for_topology(
                    participant.logical_reaction_participant.topology,
                    participant.atom_map_numbers,
                )
                expected_participants[participant.id] = expected
                if expected != participant.mapped_smiles:
                    participant_updates.append((participant, expected))
            except Exception as exc:
                errors.append(
                    f"participant {participant.id}: {type(exc).__name__}: {exc}"
                )

        node_rows = (await session.exec(
            select(MappedReactionNodeGeometryMapping).options(
                selectinload(
                    cast(Any, MappedReactionNodeGeometryMapping.mapped_reaction_node_geometry)
                )
                .selectinload(cast(Any, MappedReactionNodeGeometry.geometry))
                .selectinload(cast(Any, Geometry.topology))
            )
        )).all()
        for mapping in node_rows:
            try:
                expected = mapped_smiles_for_topology(
                    mapping.mapped_reaction_node_geometry.geometry.topology,
                    mapping.geometry_atom_map_numbers,
                )
                if expected != mapping.mapped_smiles:
                    node_mapping_updates.append((mapping, expected))
            except Exception as exc:
                errors.append(f"node mapping {mapping.id}: {type(exc).__name__}: {exc}")

        reaction_rows = (await session.exec(
            select(MappedReaction).options(selectinload(cast(Any, MappedReaction.participants)))
        )).all()
        for mapped_reaction in reaction_rows:
            try:
                expected_smiles = _expected_reaction_smiles(
                    mapped_reaction,
                    expected_participants,
                )
                expected_hash = sha256(expected_smiles.encode("utf-8")).hexdigest()
                expected_key = (
                    f"mapping:{expected_hash}"
                    if mapped_reaction.mapped_reaction_key
                    == f"mapping:{mapped_reaction.mapping_hash}"
                    else None
                )
                if (
                    expected_smiles != mapped_reaction.mapped_reaction_smiles
                    or expected_hash != mapped_reaction.mapping_hash
                    or expected_key is not None
                    and expected_key != mapped_reaction.mapped_reaction_key
                ):
                    reaction_updates.append(
                        (mapped_reaction, expected_smiles, expected_hash, expected_key)
                    )
            except Exception as exc:
                errors.append(f"mapped reaction {mapped_reaction.id}: {type(exc).__name__}: {exc}")

        collisions: list[str] = []
        hash_targets: dict[tuple[object, str], object] = {}
        key_targets: dict[tuple[object, str], object] = {}
        for mapped_reaction, _, expected_hash, expected_key in reaction_updates:
            reaction_id = mapped_reaction.logical_reaction_id
            hash_key = (reaction_id, expected_hash)
            previous_hash_target = hash_targets.get(hash_key)
            if previous_hash_target is not None and previous_hash_target != mapped_reaction.id:
                collisions.append(
                    f"mapping hash {expected_hash} has multiple rows under logical reaction "
                    f"{reaction_id}: {previous_hash_target} and {mapped_reaction.id}"
                )
            hash_targets[hash_key] = mapped_reaction.id
            if expected_key is not None:
                key_key = (reaction_id, expected_key)
                previous_key_target = key_targets.get(key_key)
                if previous_key_target is not None and previous_key_target != mapped_reaction.id:
                    collisions.append(
                        f"mapping key {expected_key} has multiple rows under logical reaction "
                        f"{reaction_id}: {previous_key_target} and {mapped_reaction.id}"
                    )
                key_targets[key_key] = mapped_reaction.id

        for mapped_reaction in reaction_rows:
            if mapped_reaction.id is None:
                continue
            current_hash_key = (mapped_reaction.logical_reaction_id, mapped_reaction.mapping_hash)
            target = hash_targets.get(current_hash_key)
            if target is not None and target != mapped_reaction.id:
                collisions.append(
                    f"mapping hash {mapped_reaction.mapping_hash} would collide under logical "
                    f"reaction {mapped_reaction.logical_reaction_id}: {target} and "
                    f"{mapped_reaction.id}"
                )
            current_key_key = (
                mapped_reaction.logical_reaction_id,
                mapped_reaction.mapped_reaction_key,
            )
            target = key_targets.get(current_key_key)
            if target is not None and target != mapped_reaction.id:
                collisions.append(
                    f"mapping key {mapped_reaction.mapped_reaction_key} would collide under "
                    f"logical reaction {mapped_reaction.logical_reaction_id}: {target} and "
                    f"{mapped_reaction.id}"
                )

    return RepairPlan(
        participant_updates=participant_updates,
        node_mapping_updates=node_mapping_updates,
        reaction_updates=reaction_updates,
        errors=errors,
        collisions=collisions,
    )


async def _apply(plan: RepairPlan) -> None:
    async with session_factory() as session:
        for participant, expected in plan.participant_updates:
            session.add(participant)
            participant.mapped_smiles = expected
        for mapping, expected in plan.node_mapping_updates:
            session.add(mapping)
            mapping.mapped_smiles = expected
        for mapped_reaction, expected_smiles, expected_hash, expected_key in plan.reaction_updates:
            session.add(mapped_reaction)
            mapped_reaction.mapped_reaction_smiles = expected_smiles
            mapped_reaction.mapping_hash = expected_hash
            if expected_key is not None:
                mapped_reaction.mapped_reaction_key = expected_key
        await session.flush()
        await session.commit()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="commit the preflighted stereo projections and reaction identities",
    )
    return parser.parse_args()


async def main() -> int:
    args = _parse_args()
    plan = await _build_plan()
    print(f"participant updates: {len(plan.participant_updates)}")
    print(f"node geometry mapping updates: {len(plan.node_mapping_updates)}")
    print(f"mapped reaction updates: {len(plan.reaction_updates)}")
    print(f"errors: {len(plan.errors)}")
    print(f"collisions: {len(plan.collisions)}")
    for message in [*plan.errors, *plan.collisions]:
        print(message)
    if plan.errors or plan.collisions:
        return 1
    if not args.apply:
        print("dry run only; use --apply to commit")
        return 0
    await _apply(plan)
    print("applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
