"""Reaction identity must survive a simultaneous permutation of both endpoints."""

import random

import pytest
from rdkit import Chem

from tricycle_reaction_db.application.services.canonical_reaction_identity import (
    canonical_reaction_identity,
)
from tricycle_reaction_db.domain.enums import LogicalReactionParticipantSide as Side
from tricycle_reaction_db.domain.explicit_hydrogens import require_explicit_hydrogens


def identity(reactant, product, labels=None):
    labels = labels or list(range(1, reactant.GetNumAtoms() + 1))
    return canonical_reaction_identity(
        {
            Side.REACTANT: [(reactant, labels)],
            Side.PRODUCT: [(product, labels)],
        }
    )


def test_hydrogenation_is_invariant_to_source_atom_order():
    reactant = Chem.AddHs(Chem.MolFromSmiles("C=C.[H][H]"))
    product = Chem.RWMol(reactant)
    product.GetBondBetweenAtoms(0, 1).SetBondType(Chem.BondType.SINGLE)
    product.RemoveBond(2, 3)
    product.AddBond(0, 2, Chem.BondType.SINGLE)
    product.AddBond(1, 3, Chem.BondType.SINGLE)
    product = product.GetMol()
    Chem.SanitizeMol(product)
    expected = identity(reactant, product)
    generator = random.Random(71)
    for _ in range(50):
        order = list(range(reactant.GetNumAtoms()))
        generator.shuffle(order)
        observed = identity(Chem.RenumberAtoms(reactant, order), Chem.RenumberAtoms(product, order))
        assert observed.smiles == expected.smiles
        assert sorted(observed.source_map_to_canonical.values()) == list(range(1, len(order) + 1))
    assert observed.source_map_to_canonical != expected.source_map_to_canonical


@pytest.mark.parametrize("smiles", ["F[C@H](Cl)Br", "F/C=C/F", "CCO.CCO", "[2H]C([H])([H])[H]"])
def test_identity_preserves_stereo_isotope_and_disconnected_symmetry(smiles):
    molecule = Chem.AddHs(Chem.MolFromSmiles(smiles))
    expected = identity(molecule, molecule).smiles
    generator = random.Random(92)
    for _ in range(20):
        order = list(range(molecule.GetNumAtoms()))
        generator.shuffle(order)
        changed = Chem.RenumberAtoms(molecule, order)
        assert identity(changed, changed).smiles == expected


@pytest.mark.parametrize("smiles", ["C", "[CH4]", "[NH4+]", "c1ccccc1"])
def test_hydrogen_counts_are_not_atom_vertices(smiles):
    with pytest.raises(ValueError, match="atom vertex"):
        require_explicit_hydrogens(Chem.MolFromSmiles(smiles))
    require_explicit_hydrogens(Chem.AddHs(Chem.MolFromSmiles(smiles)))


def test_individually_symmetric_atom_classes_do_not_prove_joint_correspondence():
    from tricycle_reaction_db.application.services.canonical_atom_mapping import (
        canonical_reaction_atom_map_translation,
    )

    ring = Chem.MolFromSmiles("[C]1[C][C][C][C][C]1")
    labels = list(range(1, 7))
    source = {Side.REACTANT: [(ring, labels)], Side.PRODUCT: [(ring, labels)]}
    # All carbons have the same rooted identity on each side, but swapping
    # just two adjacent atoms is not an automorphism of the entire ring.
    target = {Side.REACTANT: [(ring, labels)], Side.PRODUCT: [(ring, [2, 1, 3, 4, 5, 6])]}
    assert canonical_reaction_atom_map_translation(source, target) is None


def test_database_mol_binding_rejects_hydrogen_loss_after_dto_validation():
    from sqlalchemy.dialects.postgresql import dialect

    from tricycle_reaction_db.db.types.annotated_mol import AnnotatedRdkitMol

    processor = AnnotatedRdkitMol(return_type="mol").bind_processor(dialect())
    molecule = Chem.AddHs(Chem.MolFromSmiles("CO"))
    restored = Chem.Mol(processor(molecule))
    assert restored.GetNumAtoms() == molecule.GetNumAtoms()
    with pytest.raises(ValueError, match="hydrogen explicitly"):
        processor(Chem.RemoveHs(molecule))


def test_cached_cip_annotations_do_not_change_reaction_identity():
    molecule = Chem.AddHs(Chem.MolFromSmiles("F[C@H](Cl)[C@@H](Br)I"))
    expected = identity(molecule, molecule).smiles
    copy = Chem.Mol(molecule)
    for atom in copy.GetAtoms():
        for name in ("_CIPCode", "_CIPRank"):
            if atom.HasProp(name):
                atom.ClearProp(name)
    assert identity(copy, copy).smiles == expected


@pytest.mark.parametrize("smiles", ["CCO.CCO", "F[C@H](Cl)Br", "F/C=C/F", "N->[Cu+2]<-N", "[2H]C"])
def test_canonical_index_survives_reaction_parser_roundtrip(smiles):
    from tricycle_reaction_db.application.services.mapped_geometry_atom_order import (
        parse_mapped_reaction_smiles,
    )

    molecule = Chem.AddHs(Chem.MolFromSmiles(smiles))
    expected = identity(molecule, molecule).smiles
    parsed = parse_mapped_reaction_smiles(expected)
    components = {
        side: [(mol, [atom.GetAtomMapNum() for atom in mol.GetAtoms()]) for mol in templates]
        for side, templates in (
            (Side.REACTANT, parsed.GetReactants()),
            (Side.PRODUCT, parsed.GetProducts()),
        )
    }
    assert canonical_reaction_identity(components).smiles == expected


def test_symmetric_ring_reaction_identity_survives_joint_atom_permutations():
    parser = Chem.SmilesParserParams()
    parser.removeHs = False
    reaction = (
        "[C:1]1([H:11])([H:12])[C:2]([H:13])([H:14])[C:3]([H:15])([H:16])[C:4]2=[C:5"
        "]([C:6]1([H:17])[H:18])[C:7]([H:19])([H:20])[C:8]([H:21])([H:22])[C:9]([H:2"
        "3])([H:24])[C:10]2([H:25])[H:26].[F:27][C:28]([F:29])=[C:30]([C:31](=[C:32]"
        "([F:33])[F:34])[H:36])[H:35]>>[C:1]1([H:11])([H:12])[C:2]([H:13])([H:14])[C"
        ":3]([H:15])([H:16])[C@:4]23[C@:5]([C:6]1([H:17])[H:18])([C:7]([H:19])([H:20"
        "])[C:8]([H:21])([H:22])[C:9]([H:23])([H:24])[C:10]2([H:25])[H:26])[C:28]([F"
        ":27])([F:29])[C:30]([H:35])=[C:31]([H:36])[C:32]3([F:33])[F:34]"
    )
    endpoints = [Chem.MolFromSmiles(side, parser) for side in reaction.split(">>")]
    endpoints = [
        Chem.RenumberAtoms(
            mol,
            sorted(
                range(mol.GetNumAtoms()),
                key=lambda index: mol.GetAtomWithIdx(index).GetAtomMapNum(),
            ),
        )
        for mol in endpoints
    ]
    expected = identity(*endpoints).smiles
    generator = random.Random(17)
    for _ in range(20):
        order = list(range(endpoints[0].GetNumAtoms()))
        generator.shuffle(order)
        assert identity(*(Chem.RenumberAtoms(mol, order) for mol in endpoints)).smiles == expected


@pytest.mark.parametrize("smiles", ["[CH4]", "[NH4+]", "c1ccccc1", "[C]", "[Fe+2]"])
def test_hydrogen_validation_preserves_unsanitized_source_graph(smiles):
    molecule = Chem.MolFromSmiles(smiles, sanitize=False)
    before = molecule.ToBinary()
    if smiles in {"[C]", "[Fe+2]"}:
        require_explicit_hydrogens(molecule)
    else:
        with pytest.raises(ValueError, match="atom vertex"):
            require_explicit_hydrogens(molecule)
    assert molecule.ToBinary() == before


def test_hydrogen_validation_cache_rechecks_mutated_graph():
    from tricycle_reaction_db.domain.explicit_hydrogens import (
        _require_explicit_hydrogens_from_graph,
    )

    _require_explicit_hydrogens_from_graph.cache_clear()
    molecule = Chem.AddHs(Chem.MolFromSmiles("CO"))
    require_explicit_hydrogens(molecule)
    require_explicit_hydrogens(Chem.Mol(molecule))
    assert _require_explicit_hydrogens_from_graph.cache_info().hits == 1
    original = molecule.ToBinary(Chem.PropertyPickleOptions.AllProps)
    require_explicit_hydrogens(molecule)
    assert molecule.ToBinary(Chem.PropertyPickleOptions.AllProps) == original
    molecule.GetAtomWithIdx(0).SetNumExplicitHs(1)
    with pytest.raises(ValueError, match="atom vertex"):
        require_explicit_hydrogens(molecule)
    # Failures must not be memoized or conceal a subsequent repair.
    molecule.GetAtomWithIdx(0).SetNumExplicitHs(0)
    require_explicit_hydrogens(molecule)


def test_hydrogen_validation_cache_rechecks_no_implicit_flag():
    molecule = Chem.MolFromSmiles("[C]")
    require_explicit_hydrogens(molecule)
    molecule.GetAtomWithIdx(0).SetNumRadicalElectrons(0)
    molecule.GetAtomWithIdx(0).SetNoImplicit(False)
    with pytest.raises(ValueError, match="atom vertex"):
        require_explicit_hydrogens(molecule)
