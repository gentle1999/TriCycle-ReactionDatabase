"""Canonical-SMILES traversal mapping and complete-reaction map-link tests."""

from __future__ import annotations

from rdkit import Chem

from tricycle_reaction_db.application.services.canonical_atom_mapping import (
    canonical_atom_index_mapping,
    canonical_reaction_atom_map_translation,
    reaction_atom_maps_in_geometry_order,
)
from tricycle_reaction_db.domain.enums import LogicalReactionParticipantSide as Side


def _molecule(smiles: str) -> Chem.Mol:
    molecule = Chem.MolFromSmiles(smiles)
    assert molecule is not None
    return molecule


def _reaction(
    reactants: list[tuple[Chem.Mol, list[int]]],
    products: list[tuple[Chem.Mol, list[int]]],
) -> dict[Side, list[tuple[Chem.Mol, list[int]]]]:
    return {Side.REACTANT: reactants, Side.PRODUCT: products}


def test_canonical_traversal_maps_a_renumbered_molecule_without_graph_search() -> None:
    source = _molecule("CCO")
    target = Chem.RenumberAtoms(source, [2, 1, 0])

    assert canonical_atom_index_mapping(source, target) == (2, 1, 0)


def test_canonical_traversal_rejects_a_stereochemical_difference() -> None:
    source = _molecule("F/C=C/F")
    target = _molecule("F/C=C\\F")

    assert canonical_atom_index_mapping(source, target) is None


def test_canonical_traversal_preserves_stereo_under_atom_renumbering() -> None:
    source = _molecule("F[C@H](Cl)Br")
    target = Chem.RenumberAtoms(source, [1, 3, 0, 2])

    assert canonical_atom_index_mapping(source, target) == (2, 0, 3, 1)


def test_complete_reaction_translation_handles_different_atom_orders_and_map_labels() -> None:
    source_reactant = _molecule("CCO")
    source_product = _molecule("CCO")
    target_reactant = _molecule("OCC")
    target_product = _molecule("OCC")
    source = _reaction([(source_reactant, [1, 2, 3])], [(source_product, [1, 2, 3])])
    target = _reaction([(target_reactant, [30, 20, 10])], [(target_product, [30, 20, 10])])

    translation = canonical_reaction_atom_map_translation(source, target)

    assert translation is not None
    assert translation.source_to_target == {1: 10, 2: 20, 3: 30}


def test_reaction_transform_composes_with_frame_to_geometry_permutation() -> None:
    source_reactant = _molecule("CCO")
    source_product = _molecule("CCO")
    target_reactant = _molecule("OCC")
    target_product = _molecule("OCC")
    source = _reaction([(source_reactant, [1, 2, 3])], [(source_product, [1, 2, 3])])
    target = _reaction([(target_reactant, [30, 20, 10])], [(target_product, [30, 20, 10])])

    assert reaction_atom_maps_in_geometry_order(source, target, [2, 0, 1]) == [20, 30, 10]


def test_complete_reaction_translation_accepts_equivalent_symmetric_hydrogen_swap() -> None:
    ethane = Chem.AddHs(_molecule("CC"))
    maps = list(range(1, ethane.GetNumAtoms() + 1))
    swapped_product_maps = maps.copy()
    # Hydrogens 2 and 3 are attached to the same methyl carbon and chemically
    # symmetry-equivalent; their historical labels cannot be recovered.
    swapped_product_maps[2], swapped_product_maps[3] = (
        swapped_product_maps[3],
        swapped_product_maps[2],
    )
    source = _reaction([(ethane, maps)], [(ethane, maps)])
    target = _reaction([(ethane, maps)], [(ethane, swapped_product_maps)])

    translation = canonical_reaction_atom_map_translation(source, target)

    assert translation is not None
    assert set(translation.source_to_target) == set(maps)
    assert set(translation.source_to_target.values()) == set(maps)


def test_complete_reaction_translation_rejects_a_non_symmetric_map_link_change() -> None:
    ethanol = _molecule("CCO")
    source = _reaction([(ethanol, [1, 2, 3])], [(ethanol, [1, 2, 3])])
    target = _reaction([(ethanol, [1, 2, 3])], [(ethanol, [2, 1, 3])])

    assert canonical_reaction_atom_map_translation(source, target) is None
    assert (
        canonical_reaction_atom_map_translation(
            source,
            target,
            include_stereochemistry=False,
        )
        is None
    )


def test_complete_reaction_translation_rejects_stereo_loss_or_change() -> None:
    reactant = _molecule("F/C=C/F")
    product = _molecule("F/C=C/F")
    changed_product = _molecule("F/C=C\\F")
    maps = [1, 2, 3, 4]
    source = _reaction([(reactant, maps)], [(product, maps)])
    target = _reaction([(reactant, maps)], [(changed_product, maps)])

    assert canonical_reaction_atom_map_translation(source, target) is None

    # Concrete stereo variants may share an abstract logical participant.
    # The topology-transfer path can ignore stereo for correspondence while
    # keeping the target's isomeric structure in its serialized projection.
    translated = canonical_reaction_atom_map_translation(
        source,
        target,
        include_stereochemistry=False,
    )
    assert translated is not None
    assert translated.source_to_target == dict(zip(maps, maps, strict=True))
