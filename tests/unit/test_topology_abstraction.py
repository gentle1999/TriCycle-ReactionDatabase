from rdkit import Chem

from tricycle_reaction_db.application.services.topology_abstraction import (
    StereoFeature,
    assigned_stereo_features,
    clear_stereo_features,
    find_stereo_abstraction_match,
    stereo_abstraction_projection,
)
from tricycle_reaction_db.ingestion.normalization import normalize_topology


def _two_center_molecule() -> Chem.Mol:
    molecule = Chem.MolFromSmiles("F[C@H](Cl)[C@H](Br)I")
    assert molecule is not None
    return molecule


def test_stereo_projection_clears_only_requested_features() -> None:
    molecule = _two_center_molecule()
    features = assigned_stereo_features(molecule)

    assert features == (StereoFeature("atom", 1), StereoFeature("atom", 3))
    projection = stereo_abstraction_projection(molecule, (features[0],))

    assert projection.cleared_features == (features[0],)
    assert assigned_stereo_features(projection.molecule) == (features[1],)


def test_two_center_specialization_is_a_dag_diamond() -> None:
    molecule = _two_center_molecule()
    features = assigned_stereo_features(molecule)
    one_center_a = clear_stereo_features(molecule, (features[0],))
    one_center_b = clear_stereo_features(molecule, (features[1],))
    zero_center = clear_stereo_features(molecule, features)

    full_to_a = find_stereo_abstraction_match(molecule, one_center_a)
    full_to_b = find_stereo_abstraction_match(molecule, one_center_b)
    a_to_zero = find_stereo_abstraction_match(one_center_a, zero_center)
    b_to_zero = find_stereo_abstraction_match(one_center_b, zero_center)

    assert full_to_a is not None
    assert full_to_b is not None
    assert a_to_zero is not None
    assert b_to_zero is not None
    assert full_to_a.abstracted_feature_count == 1
    assert full_to_b.abstracted_feature_count == 1
    assert a_to_zero.abstracted_feature_count == 1
    assert b_to_zero.abstracted_feature_count == 1
    assert find_stereo_abstraction_match(one_center_a, one_center_b) is None


def test_clear_stereo_features_removes_ez_and_directional_bonds() -> None:
    molecule = Chem.MolFromSmiles("C/C=C/C")
    assert molecule is not None
    features = assigned_stereo_features(molecule)

    assert features == (StereoFeature("bond", 1),)
    projected = clear_stereo_features(molecule, features)
    double_bond = projected.GetBondWithIdx(1)

    assert double_bond.GetStereo() == Chem.BondStereo.STEREONONE
    assert all(bond.GetBondDir() == Chem.BondDir.NONE for bond in projected.GetBonds())
    match = find_stereo_abstraction_match(molecule, projected)
    assert match is not None
    assert match.abstracted_bond_indices == (1,)


def test_only_explicit_abstraction_metadata_marks_an_upstream() -> None:
    molecule = _two_center_molecule()
    ordinary = normalize_topology(
        molecule,
        add_hydrogens=False,
        reconstruction_method="tests/ordinary-topology",
        reconstruction_version="1",
    )
    marked = normalize_topology(
        clear_stereo_features(molecule, (assigned_stereo_features(molecule)[0],)),
        add_hydrogens=False,
        reconstruction_method="topology/stereo-abstraction",
        reconstruction_version="1",
        reconstruction_metadata={"is_stereo_abstraction_upstream": True},
    )

    assert ordinary.topology.is_stereo_abstraction_upstream is False
    assert marked.topology.is_stereo_abstraction_upstream is True
