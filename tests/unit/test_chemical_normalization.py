import numpy as np
import pytest
from molgr.utils.converter import METAL_UNPAIRED_ELECTRONS_PROP
from pydantic import ValidationError
from rdkit import Chem
from rdkit.Chem import AllChem, rdDepictor
from rdkit.Geometry import Point3D

from tricycle_reaction_db.application.dtos import (
    GeometryRecord,
    MolecularTopologyRecord,
    NormalizedMoleculeRecord,
)
from tricycle_reaction_db.application.services.artifact_uploads import _mapped_reaction_smiles
from tricycle_reaction_db.application.services.reactions import mapped_smiles_for_topology
from tricycle_reaction_db.domain.enums import StereoStatus, TopologySanitizationStatus
from tricycle_reaction_db.ingestion.normalization import (
    StereoProjectionError,
    _canonical_isomeric_smiles_signature,
    ensure_serializable_double_bond_stereochemistry,
    infer_molgr_stereochemistry_from_3d,
    normalize_molecule,
    normalize_molgr_stereochemistry,
    normalize_topology,
    normalize_topology_with_mapping,
    validate_serializable_double_bond_stereochemistry,
)


def _explicit_molecule() -> Chem.Mol:
    mol = Chem.AddHs(Chem.MolFromSmiles("[CH3:8][C@H:2](F)Cl"))
    mol.SetProp("source", "discarded")
    mol.GetAtomWithIdx(0).SetProp("annotation", "discarded")
    return mol


def _coordinates(atom_count: int) -> np.ndarray:
    return np.arange(atom_count * 3, dtype=np.float64).reshape(atom_count, 3) / 10


def _molop_n_diene_with_source_coordinates() -> Chem.Mol:
    """Build the source-order graph from the reported MolOP diene frame."""

    molecule = Chem.RWMol()
    for atomic_number in (6, 6, 7, 7, 6, 6, 7, 7, *([1] * 10)):
        atom = Chem.Atom(atomic_number)
        atom.SetNoImplicit(True)
        molecule.AddAtom(atom)
    for begin, end, bond_type in (
        (10, 0, Chem.BondType.SINGLE),
        (13, 5, Chem.BondType.SINGLE),
        (15, 5, Chem.BondType.SINGLE),
        (3, 2, Chem.BondType.SINGLE),
        (3, 12, Chem.BondType.SINGLE),
        (3, 11, Chem.BondType.SINGLE),
        (5, 4, Chem.BondType.SINGLE),
        (5, 14, Chem.BondType.SINGLE),
        (4, 6, Chem.BondType.DOUBLE),
        (4, 1, Chem.BondType.SINGLE),
        (6, 7, Chem.BondType.SINGLE),
        (1, 2, Chem.BondType.DOUBLE),
        (1, 0, Chem.BondType.SINGLE),
        (7, 17, Chem.BondType.SINGLE),
        (7, 16, Chem.BondType.SINGLE),
        (0, 9, Chem.BondType.SINGLE),
        (0, 8, Chem.BondType.SINGLE),
    ):
        molecule.AddBond(begin, end, bond_type)
    result = molecule.GetMol()
    conformer = Chem.Conformer(result.GetNumAtoms())
    conformer.Set3D(True)
    for atom_index, coordinates in enumerate(
        (
            (-1.76327, -1.63010, 0.05416),
            (-0.97871, -0.36352, -0.00037),
            (-1.67802, 0.72697, 0.03676),
            (-1.13729, 1.94404, -0.09989),
            (0.46114, -0.42304, -0.04225),
            (1.16006, -1.74338, -0.06377),
            (1.14355, 0.67427, -0.00180),
            (2.48778, 0.65068, 0.00909),
            (-1.43531, -2.25475, 0.88471),
            (-2.81942, -1.39767, 0.18943),
            (-1.65240, -2.19425, -0.87192),
            (-1.70850, 2.64603, 0.35586),
            (-0.15879, 2.00921, 0.17820),
            (2.04833, -1.67985, -0.69183),
            (1.47306, -2.02508, 0.94211),
            (0.52275, -2.53150, -0.45937),
            (2.89701, -0.08360, 0.57981),
            (2.84907, 1.55378, 0.29116),
        ),
    ):
        conformer.SetAtomPosition(atom_index, Point3D(*coordinates))
    result.AddConformer(conformer, assignId=False)
    return result


def _assert_source_mapping_matches_topology(
    source: Chem.Mol,
    topology: Chem.Mol,
    source_to_topology: list[int],
) -> None:
    atom_count = source.GetNumAtoms()
    assert topology.GetNumAtoms() == atom_count
    assert sorted(source_to_topology) == list(range(atom_count))
    for source_index, topology_index in enumerate(source_to_topology):
        source_atom = source.GetAtomWithIdx(source_index)
        topology_atom = topology.GetAtomWithIdx(topology_index)
        assert topology_atom.GetAtomicNum() == source_atom.GetAtomicNum()
        assert topology_atom.GetFormalCharge() == source_atom.GetFormalCharge()
        assert topology_atom.GetNumRadicalElectrons() == source_atom.GetNumRadicalElectrons()
    for source_bond in source.GetBonds():
        topology_bond = topology.GetBondBetweenAtoms(
            source_to_topology[source_bond.GetBeginAtomIdx()],
            source_to_topology[source_bond.GetEndAtomIdx()],
        )
        assert topology_bond is not None
        assert topology_bond.GetBondType() == source_bond.GetBondType()


def _indexed_graph_signature(molecule: Chem.Mol) -> tuple[object, ...]:
    atoms = tuple(
        (
            atom.GetAtomicNum(),
            atom.GetFormalCharge(),
            atom.GetNumRadicalElectrons(),
            int(atom.GetChiralTag()),
        )
        for atom in molecule.GetAtoms()
    )
    bonds = tuple(
        sorted(
            (
                min(bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()),
                max(bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()),
                str(bond.GetBondType()),
                int(bond.GetStereo()),
            )
            for bond in molecule.GetBonds()
        )
    )
    return atoms, bonds


def _unsanitizable_molecule() -> Chem.Mol:
    molecule = Chem.RWMol()
    carbon_index = molecule.AddAtom(Chem.Atom(6))
    for _ in range(5):
        hydrogen_index = molecule.AddAtom(Chem.Atom(1))
        molecule.AddBond(carbon_index, hydrogen_index, Chem.BondType.SINGLE)
    return molecule.GetMol()


def _unsanitizable_ring_molecule() -> Chem.Mol:
    molecule = Chem.RWMol()
    ring_atoms = [molecule.AddAtom(Chem.Atom(6)) for _ in range(3)]
    for begin, end in zip(ring_atoms, ring_atoms[1:] + ring_atoms[:1], strict=True):
        molecule.AddBond(begin, end, Chem.BondType.SINGLE)
    for _ in range(3):
        hydrogen_index = molecule.AddAtom(Chem.Atom(1))
        molecule.AddBond(ring_atoms[0], hydrogen_index, Chem.BondType.SINGLE)
    return molecule.GetMol()


def _molgr_metal_electron_molecule() -> Chem.Mol:
    """Small MolGR-like graph whose metal electron is stored as an atom property."""

    molecule = Chem.RWMol()
    oxygen = molecule.AddAtom(Chem.Atom(8))
    aluminum = molecule.AddAtom(Chem.Atom(13))
    molecule.AddBond(oxygen, aluminum, Chem.BondType.SINGLE)
    result = molecule.GetMol()
    for atom in result.GetAtoms():
        atom.SetNoImplicit(True)
        atom.SetNumRadicalElectrons(0)
    result.GetAtomWithIdx(oxygen).SetFormalCharge(1)
    result.GetAtomWithIdx(aluminum).SetIntProp(METAL_UNPAIRED_ELECTRONS_PROP, 1)
    return result


def test_normalization_separates_formula_topology_and_geometry() -> None:
    mol = _explicit_molecule()
    record = normalize_molecule(
        mol,
        _coordinates(mol.GetNumAtoms()),
        charge=0,
        multiplicity=1,
        reconstruction_method="test",
        reconstruction_version="1",
    )

    assert record.formula.hill_formula == "C2H4ClF"
    assert record.formula.atom_count == mol.GetNumAtoms()
    assert record.topology.formal_charge == 0
    assert record.topology.fragment_count == 1
    assert record.topology.stereo_status is StereoStatus.ASSIGNED
    assert record.topology.mol.GetNumConformers() == 0
    assert not record.topology.mol.HasProp("source")
    assert all(atom.GetAtomMapNum() == 0 for atom in record.topology.mol.GetAtoms())
    assert all(not atom.HasProp("annotation") for atom in record.topology.mol.GetAtoms())
    assert record.geometry.mol.GetNumConformers() == 1
    assert record.geometry.mol.GetConformer().Is3D()
    assert [atom.GetAtomicNum() for atom in record.geometry.mol.GetAtoms()] == [
        atom.GetAtomicNum() for atom in record.topology.mol.GetAtoms()
    ]
    assert all(atom.GetAtomMapNum() == 0 for atom in record.geometry.mol.GetAtoms())
    assert record.geometry.internal_coordinates.shape == (mol.GetNumAtoms(), 3)
    assert record.geometry.internal_coordinates.dtype == np.dtype("<f8")
    assert not record.geometry.internal_coordinates.flags.writeable
    np.testing.assert_array_equal(record.observed_coordinates, _coordinates(mol.GetNumAtoms()))
    assert sorted(record.observed_to_geometry_atom_indices) == list(range(mol.GetNumAtoms()))


def test_normalization_preserves_molgr_metal_unpaired_electrons() -> None:
    molecule = _molgr_metal_electron_molecule()
    record = normalize_molecule(
        molecule,
        _coordinates(molecule.GetNumAtoms()),
        charge=1,
        multiplicity=1,
        reconstruction_method="molgr/cpp",
        reconstruction_version="0.1.7",
    )

    assert record.topology.radical_electron_count == 1
    metal = next(atom for atom in record.topology.mol.GetAtoms() if atom.GetSymbol() == "Al")
    oxygen = next(atom for atom in record.topology.mol.GetAtoms() if atom.GetSymbol() == "O")
    assert metal.GetNumRadicalElectrons() == 0
    assert metal.GetIntProp(METAL_UNPAIRED_ELECTRONS_PROP) == 1
    assert oxygen.GetFormalCharge() == 1
    assert oxygen.GetNumRadicalElectrons() == 0


def test_topology_identity_is_independent_of_source_atom_order() -> None:
    mol = _explicit_molecule()
    coordinates = _coordinates(mol.GetNumAtoms())
    order = list(reversed(range(mol.GetNumAtoms())))
    reordered = Chem.RenumberAtoms(mol, order)

    first = normalize_molecule(
        mol,
        coordinates,
        charge=0,
        multiplicity=1,
        reconstruction_method="test",
        reconstruction_version="1",
    )
    second = normalize_molecule(
        reordered,
        coordinates[order],
        charge=0,
        multiplicity=1,
        reconstruction_method="test",
        reconstruction_version="1",
    )

    assert first.formula.composition_hash == second.formula.composition_hash
    assert first.topology.graph_hash == second.topology.graph_hash
    assert first.topology.canonical_isomeric_smiles == second.topology.canonical_isomeric_smiles
    assert [atom.GetAtomicNum() for atom in first.geometry.mol.GetAtoms()] == [
        atom.GetAtomicNum() for atom in first.topology.mol.GetAtoms()
    ]
    assert [atom.GetAtomicNum() for atom in second.geometry.mol.GetAtoms()] == [
        atom.GetAtomicNum() for atom in second.topology.mol.GetAtoms()
    ]
    np.testing.assert_array_equal(first.observed_coordinates, coordinates)
    np.testing.assert_array_equal(second.observed_coordinates, coordinates[order])


def test_unsanitizable_topology_retains_searchable_connectivity_and_geometry(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    molecule = _unsanitizable_molecule()
    coordinates = _coordinates(molecule.GetNumAtoms())

    def forbidden_sanitize(*_: object, **__: object) -> object:
        raise AssertionError("fallback topology must not be sanitized")

    monkeypatch.setattr(Chem, "SanitizeMol", forbidden_sanitize)

    record = normalize_molecule(
        molecule,
        coordinates,
        charge=0,
        multiplicity=2,
        reconstruction_method="molgr/openbabel-fallback",
        reconstruction_version="1",
        reconstruction_metadata={"molgr_status": "suspicious_fallback"},
    )

    assert record.topology.sanitization_status is TopologySanitizationStatus.FAILED
    assert "AtomValenceException" in (record.topology.sanitization_error or "")
    assert record.topology.mol.HasSubstructMatch(Chem.MolFromSmarts("[#6]-[#1]"))
    assert record.geometry.atom_count == molecule.GetNumAtoms()
    assert record.topology_derivation.reconstruction_metadata == {
        "molgr_status": "suspicious_fallback",
        "topology_sanitization_status": "failed",
        "topology_sanitization_error": record.topology.sanitization_error,
    }


def test_unsanitizable_fallback_initializes_ring_info_for_postgresql_rdkit() -> None:
    molecule = _unsanitizable_ring_molecule()

    record = normalize_molecule(
        molecule,
        _coordinates(molecule.GetNumAtoms()),
        charge=0,
        multiplicity=2,
        reconstruction_method="molgr/openbabel-fallback",
        reconstruction_version="1",
        reconstruction_metadata={"molgr_status": "suspicious_fallback"},
    )

    assert record.topology.sanitization_status is TopologySanitizationStatus.FAILED
    assert record.topology.mol.GetRingInfo().NumRings() == 1
    assert record.geometry.mol.GetRingInfo().NumRings() == 1
    assert Chem.Mol(record.topology.mol.ToBinary()).GetRingInfo().NumRings() == 1
    assert Chem.Mol(record.geometry.mol.ToBinary()).GetRingInfo().NumRings() == 1


def test_suspicious_molgr_fallback_still_validates_e_z_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Chem.AddHs(Chem.MolFromSmiles("F/C=C/F"))
    rdDepictor.Compute2DCoords(source)
    for bond in source.GetBonds():
        bond.SetBondDir(Chem.BondDir.NONE)

    def forbidden_sanitize(*_: object, **__: object) -> object:
        raise AssertionError("suspicious MolGR fallback must not be sanitized")

    monkeypatch.setattr(Chem, "SanitizeMol", forbidden_sanitize)
    record = normalize_topology(
        source,
        add_hydrogens=False,
        reconstruction_method="molgr/openbabel-fallback",
        reconstruction_version="0.1.8",
        reconstruction_metadata={"molgr_status": "suspicious_fallback"},
    )

    double_bond = next(
        bond
        for bond in record.topology.mol.GetBonds()
        if bond.GetStereo() in (Chem.BondStereo.STEREOE, Chem.BondStereo.STEREOZ)
    )
    assert double_bond.GetStereo() is Chem.BondStereo.STEREOE


def test_molgr_normalization_trusts_input_graph_without_sanitizing(monkeypatch) -> None:
    molecule = _unsanitizable_ring_molecule()
    for atom in molecule.GetAtoms():
        atom.SetNoImplicit(True)

    def forbidden_sanitize(*_: object, **__: object) -> object:
        raise AssertionError("MolGR graphs must not be sanitized")

    monkeypatch.setattr(Chem, "SanitizeMol", forbidden_sanitize)

    record = normalize_molecule(
        molecule,
        _coordinates(molecule.GetNumAtoms()),
        charge=0,
        multiplicity=2,
        reconstruction_method="molgr/cpp",
        reconstruction_version="0.1.7",
    )

    assert record.topology.sanitization_status is TopologySanitizationStatus.SANITIZED
    assert record.topology.sanitization_error is None
    assert record.topology.mol.GetRingInfo().NumRings() == 1
    assert record.geometry.mol.GetRingInfo().NumRings() == 1


def test_trusted_ts_endpoint_normalization_does_not_sanitize(monkeypatch) -> None:
    molecule = _unsanitizable_ring_molecule()
    for atom in molecule.GetAtoms():
        atom.SetNoImplicit(True)

    def forbidden_sanitize(*_: object, **__: object) -> object:
        raise AssertionError("trusted TS endpoint graphs must not be sanitized")

    monkeypatch.setattr(Chem, "SanitizeMol", forbidden_sanitize)

    record, source_to_topology = normalize_topology_with_mapping(
        molecule,
        add_hydrogens=False,
        reconstruction_method="molop/possible_pre_post_ts",
        reconstruction_version="test",
        reconstruction_metadata={"topology_source_trusted": True},
    )

    assert record.topology.sanitization_status is TopologySanitizationStatus.SANITIZED
    assert record.topology.mol.GetRingInfo().NumRings() == 1
    assert sorted(source_to_topology) == list(range(molecule.GetNumAtoms()))


def test_trusted_molgr_e_z_stereo_gets_serializable_direction_metadata() -> None:
    assigned = Chem.MolFromSmiles("F/C=C/F")
    assert assigned is not None
    rdDepictor.Compute2DCoords(assigned)
    incomplete = Chem.MolFromMolBlock(
        Chem.MolToMolBlock(assigned),
        sanitize=False,
        removeHs=False,
        strictParsing=True,
    )
    assert incomplete is not None
    assigned_double_bond = next(
        bond
        for bond in assigned.GetBonds()
        if bond.GetStereo() in (Chem.BondStereo.STEREOE, Chem.BondStereo.STEREOZ)
    )
    incomplete_double_bond = incomplete.GetBondWithIdx(assigned_double_bond.GetIdx())
    incomplete_double_bond.SetStereo(assigned_double_bond.GetStereo())
    incomplete_double_bond.SetStereoAtoms(*list(assigned_double_bond.GetStereoAtoms()))
    for bond in incomplete.GetBonds():
        bond.SetBondDir(Chem.BondDir.NONE)

    incomplete_smiles = Chem.MolToSmiles(
        incomplete,
        canonical=True,
        isomericSmiles=True,
        allHsExplicit=True,
    )
    assert "/" not in incomplete_smiles and "\\" not in incomplete_smiles

    repaired = ensure_serializable_double_bond_stereochemistry(incomplete)
    repaired_smiles = Chem.MolToSmiles(
        repaired,
        canonical=True,
        isomericSmiles=True,
        allHsExplicit=True,
    )

    assert "/" in repaired_smiles or "\\" in repaired_smiles
    repaired_double_bond = repaired.GetBondWithIdx(incomplete_double_bond.GetIdx())
    assert repaired_double_bond.GetStereo() is assigned_double_bond.GetStereo()
    assert list(repaired_double_bond.GetStereoAtoms()) == list(
        assigned_double_bond.GetStereoAtoms()
    )

    normalized = normalize_molecule(
        incomplete,
        np.asarray(incomplete.GetConformer().GetPositions(), dtype=np.float64),
        charge=0,
        multiplicity=1,
        reconstruction_method="molgr/cpp",
        reconstruction_version="0.1.8",
    )
    assert normalized.topology.canonical_isomeric_smiles is not None
    assert (
        "/" in normalized.topology.canonical_isomeric_smiles
        or "\\" in normalized.topology.canonical_isomeric_smiles
    )
    geometry_smiles = Chem.MolToSmiles(
        normalized.geometry.mol,
        canonical=True,
        isomericSmiles=True,
        allHsExplicit=True,
    )
    assert "/" in geometry_smiles or "\\" in geometry_smiles


def test_coordinate_stereo_boundary_is_separate_from_smiles_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Chem.AddHs(Chem.MolFromSmiles("F/C=C/[C@H](Cl)Br"))
    assert source is not None
    assert AllChem.EmbedMolecule(source, randomSeed=17) == 0
    source_double_bond = next(
        bond for bond in source.GetBonds() if bond.GetBondType() == Chem.BondType.DOUBLE
    )
    source_stereo = source_double_bond.GetStereo()
    for bond in source.GetBonds():
        bond.SetBondDir(Chem.BondDir.NONE)

    calls: list[str] = []
    assign_from_3d = Chem.AssignStereochemistryFrom3D

    def assign_wrapper(*args: object, **kwargs: object) -> None:
        calls.append("assign3d")
        assign_from_3d(*args, **kwargs)

    def forbidden_direction_discovery(*_: object, **__: object) -> None:
        raise AssertionError("coordinate stereo inference must not use SMILES directions")

    monkeypatch.setattr(Chem, "AssignStereochemistryFrom3D", assign_wrapper)
    monkeypatch.setattr(Chem, "DetectBondStereochemistry", forbidden_direction_discovery)
    monkeypatch.setattr(Chem, "SetBondStereoFromDirections", forbidden_direction_discovery)

    normalized = infer_molgr_stereochemistry_from_3d(source)

    assert calls == ["assign3d"]
    assert normalized is not source
    normalized_double_bond = normalized.GetBondWithIdx(source_double_bond.GetIdx())
    assert normalized_double_bond.GetStereo() in {
        Chem.BondStereo.STEREOE,
        Chem.BondStereo.STEREOZ,
        Chem.BondStereo.STEREONONE,
    }
    # The coordinate pass is allowed to replace stale graph stereo. The source
    # object itself remains untouched and is not reused as a writer scratchpad.
    assert source_double_bond.GetStereo() is source_stereo

    normalized_again = normalize_molgr_stereochemistry(normalized)
    assert calls == ["assign3d"]
    assert normalized_again.GetBondWithIdx(source_double_bond.GetIdx()).GetStereo() == (
        normalized_double_bond.GetStereo()
    )


def test_serialization_projection_never_reinfers_from_a_conformer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    molecule = Chem.AddHs(Chem.MolFromSmiles("F/C=C/F"))
    assert molecule is not None
    assert AllChem.EmbedMolecule(molecule, randomSeed=17) == 0
    double_bond = next(
        bond for bond in molecule.GetBonds() if bond.GetBondType() == Chem.BondType.DOUBLE
    )
    expected_stereo = double_bond.GetStereo()
    for bond in molecule.GetBonds():
        bond.SetBondDir(Chem.BondDir.NONE)

    def forbidden_assign(*_: object, **__: object) -> None:
        raise AssertionError("SMILES projection must not infer stereo from coordinates")

    monkeypatch.setattr(Chem, "AssignStereochemistryFrom3D", forbidden_assign)
    repaired = ensure_serializable_double_bond_stereochemistry(molecule)
    serialized = Chem.MolToSmiles(
        repaired,
        canonical=True,
        isomericSmiles=True,
        allHsExplicit=True,
    )
    parser = Chem.SmilesParserParams()
    parser.removeHs = False
    reparsed = Chem.MolFromSmiles(serialized, parser)
    assert reparsed is not None
    reparsed_double_bond = next(
        bond for bond in reparsed.GetBonds() if bond.GetBondType() == Chem.BondType.DOUBLE
    )
    assert reparsed_double_bond.GetStereo() is expected_stereo


def test_trusted_e_z_projection_accepts_atom_order_equivalent_smiles() -> None:
    # This conjugated, explicit-H graph reproduces the MolGR/RDKit boundary:
    # the source BondStereo values are assigned, while the atom-order
    # projection makes the local serialized E/Z map disagree with those
    # values. The complete canonical isomeric projection is still lossless.
    smiles = (
        "[H][C]([H])=[C]([H])[H]."
        "[H][O][C](=[O])/[C](=[C]([H])\\[C]([H])=[C]"
        "(\\[C](=[O])[O][H])[C]([H])([H])[H])[C]([H])([H])[H]"
    )
    parser = Chem.SmilesParserParams()
    parser.removeHs = False
    molecule = Chem.MolFromSmiles(smiles, parser)
    assert molecule is not None

    record = normalize_molecule(
        molecule,
        _coordinates(molecule.GetNumAtoms()),
        charge=0,
        multiplicity=1,
        reconstruction_method="molgr/cpp",
        reconstruction_version="0.1.8",
    )

    assert record.topology.stereo_status is StereoStatus.ASSIGNED
    assert record.topology.identity_schema_version == "topology-identity-v1"
    assert record.geometry.mol.GetNumConformers() == 1
    assert "stereo_projection" not in record.topology_derivation.reconstruction_metadata
    assert "/" in (record.topology.canonical_isomeric_smiles or "") or "\\" in (
        record.topology.canonical_isomeric_smiles or ""
    )


def test_symmetric_e_z_assignments_compare_by_molecular_identity() -> None:
    # In hexa-2,4-diene, reversing the identical ends exchanges the two
    # terminal double bonds.  [E,Z] and [Z,E] are therefore the same molecule,
    # while [E,E] is not.
    ez = _canonical_isomeric_smiles_signature("C/C=C/C=C\\C")
    ze = _canonical_isomeric_smiles_signature("C/C=C\\C=C\\C")
    ee = _canonical_isomeric_smiles_signature("C/C=C/C=C/C")

    assert ez == ze
    assert ez != ee


def test_mapped_symmetric_topology_is_standardized_after_map_removal() -> None:
    # This is the same failure mode as the persisted diene duplicate: the two
    # equivalent source traversals carry different atom maps and opposite
    # slash directions on the symmetric conjugated system.  Topology identity
    # must be computed only after maps have been removed and the graph has been
    # canonicalized.
    parser = Chem.SmilesParserParams()
    parser.removeHs = False
    source_smiles = (
        "[H][O][C](=[O])/[C](=[C]([H])\\[C]([H])=[C]"
        "(\\[C](=[O])[O][H])[C]([H])([H])[H])[C]([H])([H])[H]"
    )
    equivalent_smiles = (
        "[H][O][C](=[O])/[C](=[C]([H])/[C]([H])=[C]"
        "(\\[C](=[O])[O][H])[C]([H])([H])[H])[C]([H])([H])[H]"
    )

    normalized = []
    for offset, smiles in ((100, source_smiles), (200, equivalent_smiles)):
        molecule = Chem.MolFromSmiles(smiles, parser)
        assert molecule is not None
        assert AllChem.EmbedMolecule(molecule, randomSeed=17) == 0
        for atom_index, atom in enumerate(molecule.GetAtoms()):
            atom.SetAtomMapNum(offset + atom_index)
        record = normalize_topology(
            molecule,
            add_hydrogens=False,
            reconstruction_method="molgr/cpp",
            reconstruction_version="0.1.8",
        )
        normalized.append(record)

    first, second = normalized
    assert first.topology.canonical_isomeric_smiles == second.topology.canonical_isomeric_smiles
    assert first.topology.graph_hash == second.topology.graph_hash
    assert first.topology.identity_schema_version == "topology-identity-v1"
    assert second.topology.identity_schema_version == "topology-identity-v1"
    assert all(atom.GetAtomMapNum() == 0 for atom in first.topology.mol.GetAtoms())
    assert all(atom.GetAtomMapNum() == 0 for atom in second.topology.mol.GetAtoms())


def test_coordinate_authoritative_diene_survives_topology_projection_and_mapping() -> None:
    # The source atom/bond order and coordinates are taken from the MolOP frame
    # that previously became Z/Z during the topology projection.  The actual
    # source geometry is Z on source edge 1--2 and E on source edge 4--6.
    source = _molop_n_diene_with_source_coordinates()
    record, source_to_topology = normalize_topology_with_mapping(
        source,
        add_hydrogens=False,
        reconstruction_method="molgr/cpp",
        reconstruction_version="0.1.8",
    )

    expected_source_stereo = {
        frozenset((1, 2)): Chem.BondStereo.STEREOZ,
        frozenset((4, 6)): Chem.BondStereo.STEREOE,
    }
    for source_edge, expected in expected_source_stereo.items():
        topology_edge = record.topology.mol.GetBondBetweenAtoms(
            *(source_to_topology[index] for index in source_edge)
        )
        assert topology_edge is not None
        assert topology_edge.GetStereo() is expected

    source_maps = list(range(7, 25))
    topology_maps = [0] * record.topology.atom_count
    for source_index, topology_index in enumerate(source_to_topology):
        topology_maps[topology_index] = source_maps[source_index]
    assert mapped_smiles_for_topology(record.topology, topology_maps) == (
        "[C:7]([C:8](=[N:9]/[N:10]([H:18])[H:19])/[C:11]"
        "([C:12]([H:20])([H:21])[H:22])=[N:13]/[N:14]([H:23])[H:24])"
        "([H:15])([H:16])[H:17]"
    )


def test_initial_mapped_reaction_projection_preserves_source_diene_geometry() -> None:
    """Reaction-map serialization must preserve MolGR's selected control atoms."""

    source = _molop_n_diene_with_source_coordinates()
    inferred = infer_molgr_stereochemistry_from_3d(source)

    mapped_reaction = _mapped_reaction_smiles(inferred, Chem.Mol(inferred))

    expected = (
        "[C:1]([C:2](=[N:3]/[N:4]([H:12])[H:13])/[C:5]"
        "([C:6]([H:14])([H:15])[H:16])=[N:7]/[N:8]([H:17])[H:18])"
        "([H:9])([H:10])[H:11]"
    )
    assert mapped_reaction == f"{expected}>>{expected}"


@pytest.mark.parametrize(
    ("source_smiles", "expected_stereo"),
    [
        ("C/C=C/C", Chem.BondStereo.STEREOE),
        ("C/C=C\\C", Chem.BondStereo.STEREOZ),
    ],
)
def test_stale_double_bond_direction_cannot_flip_serialized_e_z(
    source_smiles: str,
    expected_stereo: Chem.BondStereo,
) -> None:
    molecule = Chem.AddHs(Chem.MolFromSmiles(source_smiles))
    directional_bond = next(
        bond for bond in molecule.GetBonds() if bond.GetBondDir() != Chem.BondDir.NONE
    )
    directional_bond.SetBondDir(
        Chem.BondDir.ENDDOWNRIGHT
        if directional_bond.GetBondDir() == Chem.BondDir.ENDUPRIGHT
        else Chem.BondDir.ENDUPRIGHT
    )

    repaired = ensure_serializable_double_bond_stereochemistry(molecule)
    serialized = Chem.MolToSmiles(
        repaired,
        canonical=True,
        isomericSmiles=True,
        allHsExplicit=True,
    )
    reparsed = Chem.MolFromSmiles(serialized)
    assert reparsed is not None
    double_bond = next(
        bond for bond in reparsed.GetBonds() if bond.GetBondType() == Chem.BondType.DOUBLE
    )
    assert double_bond.GetStereo() == expected_stereo


def test_substituted_double_bond_clears_provisional_neighbor_directions() -> None:
    molecule = Chem.AddHs(Chem.MolFromSmiles("C/C(Cl)=C(Br)/C"))
    double_bond = next(
        bond for bond in molecule.GetBonds() if bond.GetBondType() == Chem.BondType.DOUBLE
    )
    # Use a non-CIP stereo-atom pair to force the direction constraint solver
    # after SetDoubleBondNeighborDirections has populated provisional flags.
    double_bond.SetStereoAtoms(0, 4)
    double_bond.SetStereo(Chem.BondStereo.STEREOE)
    for bond in molecule.GetBonds():
        bond.SetBondDir(Chem.BondDir.NONE)

    repaired = ensure_serializable_double_bond_stereochemistry(molecule)
    serialized = Chem.MolToSmiles(
        repaired,
        canonical=True,
        isomericSmiles=True,
        allHsExplicit=True,
    )
    parser = Chem.SmilesParserParams()
    parser.removeHs = False
    reparsed = Chem.MolFromSmiles(serialized, parser)
    assert reparsed is not None
    reparsed_double_bond = next(
        bond for bond in reparsed.GetBonds() if bond.GetBondType() == Chem.BondType.DOUBLE
    )
    # The source deliberately chooses methyl/Br as the stereo-atom pair.  A
    # canonical SMILES traversal is allowed to use Cl/Br instead; in that
    # representation the same physical geometry is spelled Z, not E.
    assert reparsed_double_bond.GetStereo() == Chem.BondStereo.STEREOZ


def test_stereo_validation_accepts_physical_projection_and_rejects_flip() -> None:
    parser = Chem.SmilesParserParams()
    parser.removeHs = False
    source = Chem.MolFromSmiles("C/C(Cl)=C(Br)/C", parser)
    flipped = Chem.MolFromSmiles("C/C(Cl)=C(Br)\\C", parser)
    assert source is not None
    assert flipped is not None

    projected = ensure_serializable_double_bond_stereochemistry(source)
    # The validator accepts the writer's physical projection, even when its
    # canonical traversal chooses a different slash/control-atom spelling.
    validate_serializable_double_bond_stereochemistry(source, projected)

    with pytest.raises(
        StereoProjectionError,
        match="changed the source E/Z control-atom relationship",
    ):
        validate_serializable_double_bond_stereochemistry(source, flipped)


def test_trusted_molgr_normalization_preserves_e_z_stereochemistry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trans_source = Chem.AddHs(Chem.MolFromSmiles("F/C=C/F"))
    cis_source = Chem.AddHs(Chem.MolFromSmiles("F/C=C\\F"))
    chiral_source = Chem.AddHs(Chem.MolFromSmiles("C[C@H](O)F"))
    Chem.AssignStereochemistry(chiral_source, cleanIt=True, force=True)
    assert chiral_source.GetAtomWithIdx(1).HasProp("_CIPRank")
    assert chiral_source.GetAtomWithIdx(1).GetProp("_CIPCode") == "R"

    def forbidden_sanitize(*_: object, **__: object) -> object:
        raise AssertionError("trusted MolGR graphs must not be sanitized")

    def forbidden_stereo_assignment(*_: object, **__: object) -> object:
        raise AssertionError("trusted MolGR graphs must not rebuild stereochemistry")

    def forbidden_stereo_discovery(*_: object, **__: object) -> object:
        raise AssertionError("trusted MolGR graphs must not rediscover stereochemistry")

    monkeypatch.setattr(Chem, "SanitizeMol", forbidden_sanitize)
    monkeypatch.setattr(Chem, "AssignStereochemistry", forbidden_stereo_assignment)
    monkeypatch.setattr(Chem, "FindPotentialStereo", forbidden_stereo_discovery)

    normalized = []
    for molecule in (trans_source, cis_source):
        record, source_to_topology = normalize_topology_with_mapping(
            molecule,
            add_hydrogens=False,
            reconstruction_method="molgr/cpp",
            reconstruction_version="0.1.8",
        )
        normalized.append(record)
        _assert_source_mapping_matches_topology(
            molecule,
            record.topology.mol,
            source_to_topology,
        )

    trans, cis = normalized
    assert trans.topology.canonical_isomeric_smiles is not None
    assert cis.topology.canonical_isomeric_smiles is not None
    assert "/" in trans.topology.canonical_isomeric_smiles
    assert "\\" in cis.topology.canonical_isomeric_smiles
    assert trans.topology.graph_hash != cis.topology.graph_hash
    assert trans.topology.stereo_status is StereoStatus.ASSIGNED
    assert cis.topology.stereo_status is StereoStatus.ASSIGNED

    chiral_record, chiral_mapping = normalize_topology_with_mapping(
        chiral_source,
        add_hydrogens=False,
        reconstruction_method="molgr/cpp",
        reconstruction_version="0.1.8",
    )
    chiral_atom = chiral_record.topology.mol.GetAtomWithIdx(chiral_mapping[1])
    assert chiral_atom.HasProp("_CIPRank")
    assert chiral_atom.GetProp("_CIPCode") == "R"
    assert chiral_record.topology.mol.GetIntProp("_StereochemDone") == 1

    # NormalizedMoleculeRecord validation independently checks the topology and
    # geometry serialization and may assign stereo on its temporary copies.
    monkeypatch.undo()
    full_record = normalize_molecule(
        chiral_source,
        _coordinates(chiral_source.GetNumAtoms()),
        charge=0,
        multiplicity=1,
        reconstruction_method="molgr/cpp",
        reconstruction_version="0.1.8",
    )
    geometry_atom = full_record.geometry.mol.GetAtomWithIdx(
        full_record.observed_to_geometry_atom_indices[1]
    )
    assert geometry_atom.HasProp("_CIPRank")
    assert geometry_atom.GetProp("_CIPCode") == "R"
    assert full_record.geometry.mol.GetIntProp("_StereochemDone") == 1


def test_trusted_normalization_preserves_enhanced_stereo_groups() -> None:
    source = Chem.MolFromSmiles("F[C@H](Cl)[C@H](Br)I |&1:1,3|")
    assert source is not None
    assert len(source.GetStereoGroups()) == 1

    record = normalize_topology(
        source,
        add_hydrogens=False,
        reconstruction_method="molgr/cpp",
        reconstruction_version="0.1.8",
    )

    groups = record.topology.mol.GetStereoGroups()
    assert len(groups) == 1
    assert groups[0].GetGroupType() is Chem.StereoGroupType.STEREO_AND
    assert len(groups[0].GetAtoms()) == 2


def test_trusted_molgr_projection_is_stable_across_source_atom_order() -> None:
    source = Chem.AddHs(Chem.MolFromSmiles("F/C=C(/[C@H](Cl)Br)I"))
    Chem.AssignStereochemistry(source, cleanIt=True, force=True)
    reverse_order = list(reversed(range(source.GetNumAtoms())))
    reordered = Chem.RenumberAtoms(source, reverse_order)

    first, first_mapping = normalize_topology_with_mapping(
        source,
        add_hydrogens=False,
        reconstruction_method="molgr/cpp",
        reconstruction_version="0.1.8",
    )
    second, second_mapping = normalize_topology_with_mapping(
        reordered,
        add_hydrogens=False,
        reconstruction_method="molgr/cpp",
        reconstruction_version="0.1.8",
    )

    assert first.topology.graph_hash == second.topology.graph_hash
    assert first.topology.canonical_isomeric_smiles == second.topology.canonical_isomeric_smiles
    assert _indexed_graph_signature(first.topology.mol) == _indexed_graph_signature(
        second.topology.mol
    )
    _assert_source_mapping_matches_topology(source, first.topology.mol, first_mapping)
    _assert_source_mapping_matches_topology(reordered, second.topology.mol, second_mapping)
    # Persistence reuses the first projection for the shared graph hash.  A
    # mapping produced by any later source order must remain valid against it.
    _assert_source_mapping_matches_topology(reordered, first.topology.mol, second_mapping)


def test_graph_only_normalization_adds_implicit_hydrogens_without_geometry() -> None:
    implicit = Chem.MolFromSmiles("C=C")
    explicit = Chem.AddHs(Chem.Mol(implicit))
    calculated = normalize_molecule(
        explicit,
        _coordinates(explicit.GetNumAtoms()),
        charge=0,
        multiplicity=1,
        reconstruction_method="test",
        reconstruction_version="1",
    )
    graph_only = normalize_topology(
        implicit,
        add_hydrogens=True,
        reconstruction_method="rdkit/reaction-representation",
        reconstruction_version="test",
    )

    assert graph_only.formula.hill_formula == "C2H4"
    assert graph_only.topology.graph_hash == calculated.topology.graph_hash
    assert graph_only.topology.mol.GetNumConformers() == 0


def test_normalization_rejects_non_coordinate_bearing_topology_and_bad_coordinates() -> None:
    implicit_hydrogen_mol = Chem.MolFromSmiles("CC")
    with pytest.raises(ValueError, match="hydrogen explicitly"):
        normalize_molecule(
            implicit_hydrogen_mol,
            np.zeros((2, 3)),
            charge=0,
            multiplicity=1,
            reconstruction_method="test",
            reconstruction_version="1",
        )


def test_dtos_enforce_distinct_topology_and_geometry_mol_contracts() -> None:
    source = _explicit_molecule()
    record = normalize_molecule(
        source,
        _coordinates(source.GetNumAtoms()),
        charge=0,
        multiplicity=1,
        reconstruction_method="test",
        reconstruction_version="1",
    )

    mapped_topology = Chem.Mol(record.topology.mol)
    mapped_topology.GetAtomWithIdx(0).SetAtomMapNum(1)
    with pytest.raises(ValidationError, match="must not contain atom maps"):
        MolecularTopologyRecord(
            **record.topology.model_dump(exclude={"mol"}),
            mol=mapped_topology,
        )

    two_dimensional_geometry = Chem.Mol(record.geometry.mol)
    two_dimensional_geometry.GetConformer().Set3D(False)
    with pytest.raises(ValidationError, match="must be three-dimensional"):
        GeometryRecord(
            **record.geometry.model_dump(exclude={"mol"}),
            mol=two_dimensional_geometry,
        )

    invalid_internal = np.array(record.geometry.internal_coordinates, copy=True)
    invalid_internal[1, 0] += 0.1
    with pytest.raises(ValidationError, match="internal_coordinate_hash"):
        GeometryRecord(
            **record.geometry.model_dump(exclude={"internal_coordinates"}),
            internal_coordinates=invalid_internal,
        )


def test_normalized_record_validates_source_to_topology_element_mapping() -> None:
    source = _explicit_molecule()
    record = normalize_molecule(
        source,
        _coordinates(source.GetNumAtoms()),
        charge=0,
        multiplicity=1,
        reconstruction_method="test",
        reconstruction_version="1",
    )
    mismatched_indices = list(record.observed_to_geometry_atom_indices)
    carbon_source_index = record.observed_atomic_numbers.index(6)
    fluorine_source_index = record.observed_atomic_numbers.index(9)
    mismatched_indices[carbon_source_index], mismatched_indices[fluorine_source_index] = (
        mismatched_indices[fluorine_source_index],
        mismatched_indices[carbon_source_index],
    )
    with pytest.raises(ValidationError, match="does not map source atoms onto Topology"):
        NormalizedMoleculeRecord(
            **record.model_dump(exclude={"observed_to_geometry_atom_indices"}),
            observed_to_geometry_atom_indices=mismatched_indices,
        )

    explicit = _explicit_molecule()
    with pytest.raises(ValueError, match="coordinates must have shape"):
        normalize_molecule(
            explicit,
            np.zeros((explicit.GetNumAtoms(), 2)),
            charge=0,
            multiplicity=1,
            reconstruction_method="test",
            reconstruction_version="1",
        )


def test_topology_derivation_identity_is_independent_from_graph_identity() -> None:
    source = _explicit_molecule()
    coordinates = _coordinates(source.GetNumAtoms())
    first = normalize_molecule(
        source,
        coordinates,
        charge=0,
        multiplicity=1,
        reconstruction_method="molgr/cpp",
        reconstruction_version="1",
        reconstruction_metadata={"config": "first"},
    )
    second = normalize_molecule(
        source,
        coordinates,
        charge=0,
        multiplicity=1,
        reconstruction_method="molgr/cpp",
        reconstruction_version="2",
        reconstruction_metadata={"config": "second"},
    )

    assert first.topology.graph_hash == second.topology.graph_hash
    assert first.topology_derivation.provenance_hash != second.topology_derivation.provenance_hash


def test_normalized_record_rejects_graph_charge_and_spin_inconsistency() -> None:
    source = _explicit_molecule()
    record = normalize_molecule(
        source,
        _coordinates(source.GetNumAtoms()),
        charge=0,
        multiplicity=1,
        reconstruction_method="test",
        reconstruction_version="1",
    )

    isotope_geometry_mol = Chem.Mol(record.geometry.mol)
    carbon_index = next(
        atom.GetIdx() for atom in isotope_geometry_mol.GetAtoms() if atom.GetAtomicNum() == 6
    )
    isotope_geometry_mol.GetAtomWithIdx(carbon_index).SetIsotope(13)
    isotope_geometry = GeometryRecord(
        **record.geometry.model_dump(exclude={"mol"}),
        mol=isotope_geometry_mol,
    )
    with pytest.raises(ValidationError, match="does not match Topology"):
        NormalizedMoleculeRecord(
            **record.model_dump(exclude={"geometry"}),
            geometry=isotope_geometry,
        )

    with pytest.raises(ValidationError, match="charge does not match"):
        NormalizedMoleculeRecord(
            **record.model_dump(exclude={"charge"}),
            charge=1,
        )

    inconsistent_spin_geometry = GeometryRecord(
        **record.geometry.model_dump(exclude={"mol", "multiplicity"}),
        mol=record.geometry.mol,
        multiplicity=2,
    )
    with pytest.raises(ValidationError, match="inconsistent parity"):
        NormalizedMoleculeRecord(
            **record.model_dump(exclude={"geometry", "multiplicity"}),
            geometry=inconsistent_spin_geometry,
            multiplicity=2,
        )
