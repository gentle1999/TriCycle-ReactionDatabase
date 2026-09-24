"""Atom-order transformations for atom-mapped geometry exports."""

from __future__ import annotations

from rdkit import Chem


def molecule_in_atom_map_order(
    molecule: Chem.Mol,
    geometry_atom_map_numbers: list[int],
) -> tuple[Chem.Mol, list[int]]:
    """Return a molecule whose atom/coordinate index is ``atom_map_number - 1``.

    Database geometries retain their canonical Geometry order. Exported mapped
    geometries instead use the reaction atom-map order, so consumers can apply
    map number ``n`` directly at array index ``n - 1``.
    """

    atom_count = molecule.GetNumAtoms()
    if atom_count != len(geometry_atom_map_numbers):
        raise ValueError("geometry atom mapping length does not match its topology")
    if any(map_number <= 0 for map_number in geometry_atom_map_numbers):
        raise ValueError("geometry atom maps must be positive")
    if len(set(geometry_atom_map_numbers)) != atom_count:
        raise ValueError("geometry atom maps must be unique")

    expected_map_numbers = list(range(1, atom_count + 1))
    if sorted(geometry_atom_map_numbers) != expected_map_numbers:
        raise ValueError("geometry atom maps must be contiguous from 1 through atom count")

    geometry_indices_by_map = {
        map_number: geometry_index
        for geometry_index, map_number in enumerate(geometry_atom_map_numbers)
    }
    atom_order = [geometry_indices_by_map[map_number] for map_number in expected_map_numbers]
    ordered_molecule = Chem.RenumberAtoms(Chem.Mol(molecule), atom_order)
    for atom_index, map_number in enumerate(expected_map_numbers):
        ordered_molecule.GetAtomWithIdx(atom_index).SetAtomMapNum(map_number)
    return ordered_molecule, expected_map_numbers


__all__ = ["molecule_in_atom_map_order"]
