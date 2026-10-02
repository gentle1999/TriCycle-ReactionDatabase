"""Stream mapped-reaction transition-state geometries as JSON Lines."""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import UUID

from rdkit import Chem
from rdkit.Chem.rdchem import KekulizeException
from sqlalchemy.orm import undefer
from sqlmodel import col, select

from tricycle_reaction_db.application.services.mapped_calculation_order import (
    MappedCalculationOrder,
)
from tricycle_reaction_db.application.services.mapped_geometry_atom_order import (
    molecule_in_atom_map_order,
    validate_geometry_atom_map_elements,
)
from tricycle_reaction_db.db.models import (
    CalculationFrame,
    Geometry,
    MappedReaction,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionNodeGeometryMapping,
    NMRResult,
    NMRShieldingTensor,
    ScientificArray,
    ScientificArrayAssignment,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import MappedReactionNodeRole

logger = logging.getLogger(__name__)
PAGE_SIZE = 64


def _calculation_record(
    frame: CalculationFrame,
    geometry_maps: list[int],
    arrays: list[tuple[ScientificArray, NMRResult | None, NMRShieldingTensor | None]],
) -> dict[str, Any]:
    projection = MappedCalculationOrder.from_geometry(
        list(frame.observed_to_geometry_atom_indices), geometry_maps
    )
    values = []
    for array, nmr, shielding in arrays:
        data, axes = projection.scientific_array(
            array.kind,
            array.data,
            coupling_atom_indices=list(nmr.coupling_atom_indices) if nmr is not None else None,
        )
        metadata = dict(array.array_metadata or {})
        # Replace source-axis declarations as a unit; retaining old axis_order
        # beside reordered values would give consumers contradictory metadata.
        metadata.pop("axis_order", None)
        metadata.update(axes)
        metadata["atom_order"] = "mapped_reaction"
        metadata["coordinate_reference"] = "calculation.observed_coordinates_angstrom"
        if shielding is not None:
            metadata["atom_index"] = projection.atom_indices([shielding.atom_index])[0]
            metadata["isotropic_ppm"] = shielding.isotropic_ppm
            metadata["anisotropy_ppm"] = shielding.anisotropy_ppm
            metadata["orientation"] = shielding.orientation
        values.append(
            {
                "id": str(array.id),
                "kind": array.kind.value,
                "ordinal": array.ordinal,
                "unit": array.unit,
                "shape": list(data.shape),
                "data": data.tolist(),
                "metadata": metadata,
            }
        )
    return {
        "frame_id": str(frame.id),
        "atom_order": "mapped_reaction",
        "coordinate_frame": "source_cartesian",
        "source_to_mapped_atom_indices": list(projection.source_to_mapped),
        "observed_coordinates_angstrom": projection.array(
            frame.observed_coordinates, axes=(0,)
        ).tolist(),
        "scientific_arrays": values,
    }


def _jsonl_record(
    *,
    binding: MappedReactionNodeGeometry,
    mapped_reaction: MappedReaction,
    geometry: Geometry,
    mapping: MappedReactionNodeGeometryMapping,
    calculations: list[dict[str, Any]] | None = None,
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
    try:
        validate_geometry_atom_map_elements(
            molecule,
            atom_maps,
            mapped_reaction.mapped_reaction_smiles,
        )
    except ValueError as error:
        logger.error(
            "Skipping TS geometry %s with atom-map elements inconsistent with mapped reaction: %s",
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

    try:
        mol_block = Chem.MolToMolBlock(molecule)
    except KekulizeException:
        # Some valid stored metal/aromatic geometries cannot be kekulized by
        # RDKit's V2000 writer. Keep aromatic bonds intact and avoid aborting
        # the complete JSONL stream for one such geometry.
        logger.warning(
            "Could not kekulize TS geometry %s for Mol block export; preserving aromatic bonds",
            geometry.id,
        )
        mol_block = Chem.MolToMolBlock(molecule, kekulize=False)
    record = {
        "schema": "mapped-reaction-ts-geometry-v3",
        "key": mapped_reaction.mapped_reaction_smiles,
        "value": {
            "calculations": calculations or [],
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

        frames_by_geometry: dict[UUID, list[CalculationFrame]] = defaultdict(list)
        arrays_by_frame: dict[UUID, list[Any]] = defaultdict(list)
        async with session_factory() as session:
            rows = (await session.exec(statement)).all()
            geometry_ids = [geometry.id for _, _, geometry, _ in rows]
            if geometry_ids:
                frames = (
                    await session.exec(
                        select(CalculationFrame)
                        .options(undefer(cast(Any, CalculationFrame.observed_coordinates)))
                        .where(col(CalculationFrame.geometry_id).in_(geometry_ids))
                    )
                ).all()
                for frame in frames:
                    frames_by_geometry[frame.geometry_id].append(frame)
                frame_ids = [frame.id for frame in frames]
                if frame_ids:
                    arrays = (
                        await session.exec(
                            select(ScientificArray, NMRResult, NMRShieldingTensor)
                            .options(undefer(cast(Any, ScientificArray.data)))
                            .outerjoin(
                                ScientificArrayAssignment,
                                col(ScientificArrayAssignment.scientific_array_id)
                                == col(ScientificArray.id),
                            )
                            .outerjoin(
                                NMRResult,
                                col(NMRResult.id) == col(ScientificArrayAssignment.nmr_result_id),
                            )
                            .outerjoin(
                                NMRShieldingTensor,
                                col(NMRShieldingTensor.id)
                                == col(ScientificArrayAssignment.nmr_shielding_tensor_id),
                            )
                            .where(col(ScientificArray.frame_id).in_(frame_ids))
                            .order_by(col(ScientificArray.kind), col(ScientificArray.ordinal))
                        )
                    ).all()
                    for array, nmr, shielding in arrays:
                        arrays_by_frame[array.frame_id].append((array, nmr, shielding))
        if not rows:
            break

        for binding, mapped_reaction, geometry, mapping in rows:
            if binding.id is None:
                continue
            last_binding_id = binding.id
            try:
                calculations = [
                    _calculation_record(
                        frame,
                        list(mapping.geometry_atom_map_numbers),
                        arrays_by_frame.get(cast(UUID, frame.id), []),
                    )
                    for frame in frames_by_geometry.get(cast(UUID, geometry.id), [])
                ]
            except ValueError as error:
                raise ValueError(
                    f"cannot export mapped calculation for Geometry {geometry.id}"
                ) from error
            record = _jsonl_record(
                binding=binding,
                mapped_reaction=mapped_reaction,
                geometry=geometry,
                mapping=mapping,
                calculations=calculations,
            )
            if record is not None:
                yield record
                emitted_records += 1
                if max_records is not None and emitted_records >= max_records:
                    return


__all__ = ["iter_mapped_reaction_geometry_export"]
