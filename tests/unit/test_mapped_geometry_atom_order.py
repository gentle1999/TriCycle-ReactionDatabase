from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID

import numpy as np
import pytest
from rdkit import Chem
from rdkit.Geometry import Point3D

from tricycle_reaction_db.application.services.mapped_geometry_atom_order import (
    molecule_in_atom_map_order,
)
from tricycle_reaction_db.application.services.mapped_reaction_geometry_export import (
    _jsonl_record,
)
from tricycle_reaction_db.application.services.units_ts_dataset_export import (
    _make_multidatasetv2_raw_record,
    make_units_ts_sample,
    reaction_center_atom_maps,
)

GEOMETRY_ID = UUID("00000000-0000-7000-8000-000000000101")
BINDING_ID = UUID("00000000-0000-7000-8000-000000000102")
REACTION_ID = UUID("00000000-0000-7000-8000-000000000103")


def _geometry() -> SimpleNamespace:
    molecule = Chem.MolFromSmiles("CO")
    assert molecule is not None
    conformer = Chem.Conformer(molecule.GetNumAtoms())
    conformer.Set3D(True)
    conformer.SetAtomPosition(0, Point3D(10.0, 1.0, 2.0))
    conformer.SetAtomPosition(1, Point3D(20.0, 3.0, 4.0))
    molecule.AddConformer(conformer, assignId=True)
    return SimpleNamespace(
        id=GEOMETRY_ID,
        geometry_hash="a" * 64,
        mol=molecule,
        charge=0,
        multiplicity=1,
    )


def _mapped_reaction() -> SimpleNamespace:
    return SimpleNamespace(
        id=REACTION_ID,
        mapped_reaction_smiles="[CH3:2][OH:1]>>[CH2:2]=[O:1]",
    )


def test_units_ts_sample_and_raw_record_use_atom_map_order() -> None:
    geometry = _geometry()
    reaction = _mapped_reaction()
    geometry_maps = [2, 1]  # Geometry order is C(map 2), O(map 1).

    sample = make_units_ts_sample(
        geometry=geometry,  # type: ignore[arg-type]
        mapped_reaction=reaction,  # type: ignore[arg-type]
        geometry_atom_map_numbers=geometry_maps,
        binding_id=BINDING_ID,
    )

    assert sample["geometry_atom_map_numbers"].tolist() == [1, 2]
    assert sample["atom_symbols"] == ["O", "C"]
    assert sample["mol_atoms"].tolist() == [8, 6]
    np.testing.assert_array_equal(
        sample["mol_coords"],
        np.asarray([[20.0, 3.0, 4.0], [10.0, 1.0, 2.0]], dtype=np.float32),
    )
    assert sample["reactive_atoms"].tolist() == [0, 1]
    assert set(zip(*sample["edge_index"].tolist(), strict=True)) == {
        (0, 1),
        (1, 0),
    }

    raw_record = _make_multidatasetv2_raw_record(
        sample,
        geometry=geometry,  # type: ignore[arg-type]
        mapped_reaction=reaction,  # type: ignore[arg-type]
        geometry_atom_map_numbers=geometry_maps,
        binding_id=BINDING_ID,
    )
    assert len(raw_record) == 6
    raw_molecule = raw_record[3]
    assert [atom.GetAtomMapNum() for atom in raw_molecule.GetAtoms()] == [1, 2]
    assert [atom.GetSymbol() for atom in raw_molecule.GetAtoms()] == ["O", "C"]
    np.testing.assert_array_equal(raw_record[1], sample["mol_coords"])
    assert raw_record[4] == ((0, 1),)

    # Export reordering never mutates the canonical database Geometry.
    assert [atom.GetSymbol() for atom in geometry.mol.GetAtoms()] == ["C", "O"]
    np.testing.assert_array_equal(
        geometry.mol.GetConformer().GetPositions(),
        np.asarray([[10.0, 1.0, 2.0], [20.0, 3.0, 4.0]]),
    )


def test_mapped_geometry_jsonl_reorders_atoms_and_molblock() -> None:
    geometry = _geometry()
    record_bytes = _jsonl_record(
        binding=SimpleNamespace(id=BINDING_ID),  # type: ignore[arg-type]
        mapped_reaction=_mapped_reaction(),  # type: ignore[arg-type]
        geometry=geometry,  # type: ignore[arg-type]
        mapping=SimpleNamespace(geometry_atom_map_numbers=[2, 1]),  # type: ignore[arg-type]
    )
    assert record_bytes is not None
    record = json.loads(record_bytes)

    assert record["schema"] == "mapped-reaction-ts-geometry-v2"
    atoms = record["value"]["geometry"]["atoms"]
    assert [atom["atom_map_number"] for atom in atoms] == [1, 2]
    assert [atom["element"] for atom in atoms] == ["O", "C"]
    assert [atom["coordinates_angstrom"] for atom in atoms] == [
        [20.0, 3.0, 4.0],
        [10.0, 1.0, 2.0],
    ]

    molblock = Chem.MolFromMolBlock(
        record["value"]["rdkit_mol"]["value"],
        removeHs=False,
        sanitize=False,
    )
    assert molblock is not None
    assert [atom.GetAtomMapNum() for atom in molblock.GetAtoms()] == [1, 2]
    np.testing.assert_allclose(
        molblock.GetConformer().GetPositions(),
        np.asarray([[20.0, 3.0, 4.0], [10.0, 1.0, 2.0]]),
        atol=1e-3,
    )


def test_atom_map_order_requires_a_complete_contiguous_map_domain() -> None:
    molecule = _geometry().mol

    with pytest.raises(ValueError, match="contiguous from 1"):
        molecule_in_atom_map_order(molecule, [1, 3])


def test_stereochemistry_only_reaction_change_marks_the_reactive_atoms() -> None:
    mapped_reaction = "[CH3:1]/[CH:2]=[CH:3]/[CH3:4]>>[CH3:1]/[CH:2]=[CH:3]\\[CH3:4]"

    assert reaction_center_atom_maps(mapped_reaction) == frozenset({2, 3, 4})
