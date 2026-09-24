"""Stream mapped-reaction transition-state geometries as JSON Lines."""

from __future__ import annotations

import json
import logging
import math
from collections.abc import AsyncIterator
from uuid import UUID

from rdkit import Chem
from sqlmodel import col, select

from tricycle_reaction_db.application.services.mapped_geometry_atom_order import (
    molecule_in_atom_map_order,
)
from tricycle_reaction_db.db.models import (
    Geometry,
    MappedReaction,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionNodeGeometryMapping,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import MappedReactionNodeRole

logger = logging.getLogger(__name__)
PAGE_SIZE = 64


def _jsonl_record(
    *,
    binding: MappedReactionNodeGeometry,
    mapped_reaction: MappedReaction,
    geometry: Geometry,
    mapping: MappedReactionNodeGeometryMapping,
) -> bytes | None:
    """Serialize one verified binding with coordinates and an RDKit-readable Mol block."""

    if binding.id is None or geometry.id is None:
        return None
    atom_maps = list(mapping.geometry_atom_map_numbers)
    try:
        molecule, atom_maps = molecule_in_atom_map_order(geometry.mol, atom_maps)
    except ValueError as error:
        logger.warning(
            "Skipping TS geometry %s with invalid atom-map binding: %s",
            geometry.id,
            error,
        )
        return None
    if molecule.GetNumConformers() != 1 or not molecule.GetConformer().Is3D():
        logger.warning("Skipping TS geometry %s without exactly one 3D conformer", geometry.id)
        return None

    conformer = molecule.GetConformer()
    atoms: list[dict[str, object]] = []
    for index, atom in enumerate(molecule.GetAtoms()):  # type: ignore[no-untyped-call]
        position = conformer.GetAtomPosition(index)
        coordinates = [float(position.x), float(position.y), float(position.z)]
        if not all(math.isfinite(value) for value in coordinates):
            logger.warning("Skipping TS geometry %s with non-finite coordinates", geometry.id)
            return None
        atom_map = atom_maps[index]
        atom.SetAtomMapNum(atom_map)
        atoms.append(
            {
                "element": atom.GetSymbol(),
                "atomic_number": int(atom.GetAtomicNum()),
                "atom_map_number": atom_map,
                "coordinates_angstrom": coordinates,
            }
        )

    mol_block = Chem.MolToMolBlock(molecule)
    record = {
        "schema": "mapped-reaction-ts-geometry-v2",
        "key": mapped_reaction.mapped_reaction_smiles,
        "value": {
            "geometry": {
                "geometry_id": str(geometry.id),
                "geometry_binding_id": str(binding.id),
                "geometry_hash": geometry.geometry_hash,
                "charge": int(geometry.charge),
                "multiplicity": int(geometry.multiplicity),
                "coordinate_units": "angstrom",
                "atoms": atoms,
            },
            "rdkit_mol": {
                "format": "molblock",
                "value": mol_block,
            },
        },
    }
    return (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


async def iter_mapped_reaction_geometry_export(
    project_id: UUID,
    *,
    after_binding_id: UUID | None = None,
    max_records: int | None = None,
) -> AsyncIterator[bytes]:
    """Yield verified project TS mappings, optionally from a cursor with a record limit."""

    last_binding_id = after_binding_id
    emitted_records = 0
    while True:
        statement = (
            select(
                MappedReactionNodeGeometry,
                MappedReaction,
                Geometry,
                MappedReactionNodeGeometryMapping,
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
                col(MappedReaction.project_id) == project_id,
                col(MappedReactionNode.role) == MappedReactionNodeRole.TRANSITION_STATE,
                col(Geometry.project_id) == project_id,
                col(MappedReactionNodeGeometryMapping.verified).is_(True),
            )
            .order_by(col(MappedReactionNodeGeometry.id))
            .limit(PAGE_SIZE)
        )
        if last_binding_id is not None:
            statement = statement.where(col(MappedReactionNodeGeometry.id) > last_binding_id)

        async with session_factory() as session:
            rows = (await session.exec(statement)).all()
        if not rows:
            break

        for binding, mapped_reaction, geometry, mapping in rows:
            if binding.id is None:
                continue
            last_binding_id = binding.id
            record = _jsonl_record(
                binding=binding,
                mapped_reaction=mapped_reaction,
                geometry=geometry,
                mapping=mapping,
            )
            if record is not None:
                yield record
                emitted_records += 1
                if max_records is not None and emitted_records >= max_records:
                    return


__all__ = ["iter_mapped_reaction_geometry_export"]
