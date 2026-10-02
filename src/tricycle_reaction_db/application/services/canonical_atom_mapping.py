"""Canonical-SMILES atom-order transforms for reaction mapping.

This module is the shared mapping primitive for topology reuse and TS
Geometry binding. It deliberately does not search molecular graph matches:
canonical isomeric-SMILES traversal proposes atom correspondence, and unique
temporary atom maps validate the resulting bijection.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from rdkit import Chem

from tricycle_reaction_db.domain.enums import LogicalReactionParticipantSide

MappedComponent = tuple[Chem.Mol, Sequence[int]]
ReactionComponents = Mapping[LogicalReactionParticipantSide, Sequence[MappedComponent]]


@dataclass(frozen=True, slots=True)
class ReactionAtomMapTranslation:
    """One verified source-reaction-map to target-reaction-map bijection."""

    source_to_target: dict[int, int]
    method: str


def _map_free(molecule: Chem.Mol) -> Chem.Mol:
    result = Chem.Mol(molecule)
    result.RemoveAllConformers()
    for atom in result.GetAtoms():  # type: ignore[no-untyped-call]
        atom.SetAtomMapNum(0)
    for property_name in ("_smilesAtomOutputOrder", "_smilesBondOutputOrder"):
        if result.HasProp(property_name):
            result.ClearProp(property_name)
    return result


def _canonical_smiles(
    molecule: Chem.Mol,
    *,
    include_stereochemistry: bool,
    preserve_assigned_stereo: bool = False,
) -> tuple[str, tuple[int, ...]] | None:
    from tricycle_reaction_db.ingestion.normalization import (
        project_serializable_double_bond_stereochemistry,
    )

    candidate = _map_free(molecule)
    try:
        if preserve_assigned_stereo:
            # Match the topology identity writer: RenumberAtoms leaves index-based
            # ring caches stale, and BondDir can disagree with trusted BondStereo.
            for atom in candidate.GetAtoms():  # type: ignore[no-untyped-call]
                for name in ("_ringStereoAtoms", "_ringStereochemCand"):
                    if atom.HasProp(name):
                        atom.ClearProp(name)
            if include_stereochemistry:
                candidate = project_serializable_double_bond_stereochemistry(candidate)
            else:
                Chem.RemoveStereochemistry(candidate)
            params: Any = Chem.SmilesWriteParams()
            params.canonical = True
            params.doIsomericSmiles = include_stereochemistry
            params.allHsExplicit = True
            params.cleanStereo = False
            smiles = Chem.MolToSmiles(candidate, params)
        else:
            # Keep the established reaction-index traversal policy unchanged.
            smiles = Chem.MolToSmiles(
                candidate,
                canonical=True,
                isomericSmiles=include_stereochemistry,
                allHsExplicit=True,
            )
    except (RuntimeError, ValueError):
        return None
    raw_order = candidate.GetPropsAsDict(includePrivate=True, includeComputed=True).get(
        "_smilesAtomOutputOrder"
    )
    if raw_order is None:
        return None
    order = tuple(int(index) for index in raw_order)
    if sorted(order) != list(range(molecule.GetNumAtoms())):
        return None
    return smiles, order


def _uniquely_labelled_smiles(
    molecule: Chem.Mol,
    atom_map_numbers: Sequence[int],
    *,
    include_stereochemistry: bool,
) -> str | None:
    if len(atom_map_numbers) != molecule.GetNumAtoms():
        return None
    labelled = _map_free(molecule)
    for atom, map_number in zip(
        labelled.GetAtoms(),  # type: ignore[no-untyped-call]
        atom_map_numbers,
        strict=True,
    ):
        atom.SetAtomMapNum(int(map_number))
    try:
        from tricycle_reaction_db.ingestion.normalization import serialize_molecule_smiles

        return serialize_molecule_smiles(
            labelled,
            preserve_atom_maps=True,
            isomeric_smiles=include_stereochemistry,
            all_hs_explicit=True,
        )
    except (RuntimeError, ValueError):
        return None


def canonical_atom_index_mapping(
    source: Chem.Mol,
    target: Chem.Mol,
    *,
    include_stereochemistry: bool = True,
) -> tuple[int, ...] | None:
    """Map source atom indices to target indices through canonical traversal.

    For symmetric molecules, this selects one deterministic representative;
    it does not claim to recover historical labels for indistinguishable atoms.
    A uniquely labelled isomeric-SMILES round trip verifies the proposed
    permutation, including bond connectivity and the requested stereo state.
    """

    if source.GetNumAtoms() != target.GetNumAtoms() or source.GetNumBonds() != target.GetNumBonds():
        return None
    source_projection = _canonical_smiles(
        source,
        include_stereochemistry=include_stereochemistry,
        preserve_assigned_stereo=True,
    )
    target_projection = _canonical_smiles(
        target,
        include_stereochemistry=include_stereochemistry,
        preserve_assigned_stereo=True,
    )
    if (
        source_projection is None
        or target_projection is None
        or source_projection[0] != target_projection[0]
    ):
        return None

    source_order = source_projection[1]
    target_order = target_projection[1]
    source_to_target = [-1] * source.GetNumAtoms()
    for source_index, target_index in zip(source_order, target_order, strict=True):
        source_to_target[source_index] = target_index
    if sorted(source_to_target) != list(range(target.GetNumAtoms())):
        return None

    source_labels = list(range(1, source.GetNumAtoms() + 1))
    target_labels = [0] * target.GetNumAtoms()
    for source_index, target_index in enumerate(source_to_target):
        target_labels[target_index] = source_labels[source_index]
    source_labelled = _uniquely_labelled_smiles(
        source,
        source_labels,
        include_stereochemistry=include_stereochemistry,
    )
    target_labelled = _uniquely_labelled_smiles(
        target,
        target_labels,
        include_stereochemistry=include_stereochemistry,
    )
    if source_labelled is None or source_labelled != target_labelled:
        return None
    return tuple(source_to_target)


def _combine_components(
    components: Sequence[MappedComponent],
) -> tuple[Chem.Mol, list[int]] | None:
    combined: Chem.Mol | None = None
    map_numbers: list[int] = []
    for molecule, raw_maps in components:
        maps = [int(number) for number in raw_maps]
        if (
            not maps
            or len(maps) != molecule.GetNumAtoms()
            or any(number <= 0 for number in maps)
            or len(set(maps)) != len(maps)
        ):
            return None
        component = _map_free(molecule)
        combined = component if combined is None else Chem.CombineMols(combined, component)
        map_numbers.extend(maps)
    if combined is None or not map_numbers or len(set(map_numbers)) != len(map_numbers):
        return None
    return combined, map_numbers


def canonical_reaction_atom_map_translation(
    source: ReactionComponents,
    target: ReactionComponents,
    *,
    include_stereochemistry: bool = True,
) -> ReactionAtomMapTranslation | None:
    """Compare complete labelled reaction graphs and compose their bijections."""
    from .canonical_reaction_identity import canonical_reaction_identity

    def project(components: ReactionComponents) -> ReactionComponents:
        if include_stereochemistry:
            return components
        result: dict[LogicalReactionParticipantSide, list[MappedComponent]] = {}
        for side, entries in components.items():
            result[side] = []
            for molecule, maps in entries:
                molecule = Chem.Mol(molecule)
                Chem.RemoveStereochemistry(molecule)
                result[side].append((molecule, maps))
        return result

    try:
        source_identity = canonical_reaction_identity(project(source))
        target_identity = canonical_reaction_identity(project(target))
    except (ValueError, RuntimeError):
        return None
    if source_identity.smiles != target_identity.smiles:
        return None
    inverse = {value: key for key, value in target_identity.source_map_to_canonical.items()}
    return ReactionAtomMapTranslation(
        source_to_target={
            key: inverse[value] for key, value in source_identity.source_map_to_canonical.items()
        },
        method="canonical-reactant-joint-correspondence",
    )


def reaction_atom_maps_in_geometry_order(
    source: ReactionComponents,
    target: ReactionComponents,
    source_to_geometry_atom_indices: Sequence[int],
) -> list[int] | None:
    """Compose a source-frame map transform with its frame-to-Geometry order."""

    atom_count = len(source_to_geometry_atom_indices)
    if sorted(int(index) for index in source_to_geometry_atom_indices) != list(range(atom_count)):
        return None
    source_side_maps: list[set[int]] = []
    for side in (LogicalReactionParticipantSide.REACTANT, LogicalReactionParticipantSide.PRODUCT):
        combined = _combine_components(source.get(side, ()))
        if combined is None:
            return None
        molecule, maps = combined
        if molecule.GetNumAtoms() != atom_count:
            return None
        source_side_maps.append(set(maps))
    expected_source_maps = set(range(1, atom_count + 1))
    if source_side_maps != [expected_source_maps, expected_source_maps]:
        return None

    translation = canonical_reaction_atom_map_translation(source, target)
    if translation is None:
        return None
    geometry_maps = [0] * atom_count
    for source_index, geometry_index in enumerate(source_to_geometry_atom_indices):
        target_map = translation.source_to_target.get(source_index + 1)
        if target_map is None:
            return None
        geometry_maps[int(geometry_index)] = target_map
    if any(number <= 0 for number in geometry_maps) or len(set(geometry_maps)) != atom_count:
        return None
    return geometry_maps


__all__ = [
    "MappedComponent",
    "ReactionAtomMapTranslation",
    "ReactionComponents",
    "canonical_atom_index_mapping",
    "canonical_reaction_atom_map_translation",
    "reaction_atom_maps_in_geometry_order",
]
