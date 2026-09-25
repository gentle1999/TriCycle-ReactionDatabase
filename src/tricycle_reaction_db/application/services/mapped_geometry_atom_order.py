"""Atom-order transformations for atom-mapped geometry exports."""

from __future__ import annotations

from collections.abc import Mapping

from rdkit import Chem
from rdkit.Chem import rdChemReactions


def parse_mapped_reaction_smiles(
    mapped_reaction_smiles: str,
) -> rdChemReactions.ChemicalReaction:
    """Parse mapped reaction SMILES with RDKit's reaction parser."""

    try:
        reaction = rdChemReactions.ReactionFromSmarts(mapped_reaction_smiles, useSmiles=True)
    except (RuntimeError, ValueError) as exc:
        raise ValueError("mapped reaction SMILES could not be parsed by RDKit") from exc
    if (
        reaction is None
        or reaction.GetNumReactantTemplates() == 0
        or reaction.GetNumProductTemplates() == 0
    ):
        raise ValueError("mapped reaction SMILES must contain reactant and product templates")
    return reaction


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


def mapped_reaction_atom_signatures(
    mapped_reaction_smiles: str,
) -> dict[int, tuple[int, int]]:
    """Return the conserved atom-map to element/isotope projection of an RXN SMILES."""

    reaction = parse_mapped_reaction_smiles(mapped_reaction_smiles)
    side_signatures: list[dict[int, tuple[int, int]]] = []
    for templates in (reaction.GetReactants(), reaction.GetProducts()):
        signatures: dict[int, tuple[int, int]] = {}
        for molecule in templates:
            for atom in molecule.GetAtoms():  # type: ignore[no-untyped-call]
                map_number = atom.GetAtomMapNum()
                if map_number <= 0:
                    raise ValueError("mapped reaction atoms must have positive atom-map numbers")
                if map_number in signatures:
                    raise ValueError("mapped reaction side contains duplicate atom-map numbers")
                signatures[map_number] = (int(atom.GetAtomicNum()), int(atom.GetIsotope()))
        side_signatures.append(signatures)

    reactant_signatures, product_signatures = side_signatures
    if reactant_signatures.keys() != product_signatures.keys():
        raise ValueError("mapped reaction atom maps must be conserved across both sides")
    reactant_elements = {
        map_number: signature[0] for map_number, signature in reactant_signatures.items()
    }
    product_elements = {
        map_number: signature[0] for map_number, signature in product_signatures.items()
    }
    if reactant_elements != product_elements:
        raise ValueError("mapped reaction atom maps change elements across both sides")
    if reactant_signatures != product_signatures:
        raise ValueError("mapped reaction atom maps change isotopes across both sides")
    return reactant_signatures


def mapped_reaction_atom_elements(mapped_reaction_smiles: str) -> dict[int, int]:
    """Return the conserved atom-map to atomic-number projection of an RXN SMILES."""

    return {
        map_number: atomic_number
        for map_number, (atomic_number, _isotope) in mapped_reaction_atom_signatures(
            mapped_reaction_smiles
        ).items()
    }


def validate_geometry_atom_map_elements(
    molecule: Chem.Mol,
    geometry_atom_map_numbers: list[int],
    mapped_reaction_smiles: str,
    *,
    reaction_elements: Mapping[int, int] | None = None,
) -> None:
    """Raise when a geometry map does not identify the same element in RXN SMILES."""

    if reaction_elements is None:
        reaction_elements = mapped_reaction_atom_elements(mapped_reaction_smiles)
    if len(geometry_atom_map_numbers) != molecule.GetNumAtoms():
        raise ValueError("geometry atom mapping length does not match its topology")
    if len(set(geometry_atom_map_numbers)) != len(geometry_atom_map_numbers):
        raise ValueError("geometry atom maps must be unique")
    if set(geometry_atom_map_numbers) != reaction_elements.keys():
        raise ValueError("geometry atom maps must cover the mapped reaction atoms exactly")

    for atom, map_number in zip(
        molecule.GetAtoms(),
        geometry_atom_map_numbers,
        strict=True,  # type: ignore[no-untyped-call]
    ):
        expected_atomic_number = reaction_elements[map_number]
        if atom.GetAtomicNum() != expected_atomic_number:
            raise ValueError(
                f"geometry map {map_number} has atomic number {atom.GetAtomicNum()}, "
                f"but mapped reaction atom has atomic number {expected_atomic_number}"
            )


__all__ = [
    "mapped_reaction_atom_elements",
    "mapped_reaction_atom_signatures",
    "molecule_in_atom_map_order",
    "parse_mapped_reaction_smiles",
    "validate_geometry_atom_map_elements",
]
