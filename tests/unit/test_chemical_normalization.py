import numpy as np
import pytest
from molgr.utils.converter import METAL_UNPAIRED_ELECTRONS_PROP
from pydantic import ValidationError
from rdkit import Chem

from tricycle_reaction_db.application.dtos import (
    GeometryRecord,
    MolecularTopologyRecord,
    NormalizedMoleculeRecord,
)
from tricycle_reaction_db.domain.enums import StereoStatus, TopologySanitizationStatus
from tricycle_reaction_db.ingestion.normalization import (
    normalize_molecule,
    normalize_topology,
    normalize_topology_with_mapping,
)


def _explicit_molecule() -> Chem.Mol:
    mol = Chem.AddHs(Chem.MolFromSmiles("[CH3:8][C@H:2](F)Cl"))
    mol.SetProp("source", "discarded")
    mol.GetAtomWithIdx(0).SetProp("annotation", "discarded")
    return mol


def _coordinates(atom_count: int) -> np.ndarray:
    return np.arange(atom_count * 3, dtype=np.float64).reshape(atom_count, 3) / 10


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

    monkeypatch.setattr(Chem, "SanitizeMol", forbidden_sanitize)
    monkeypatch.setattr(Chem, "AssignStereochemistry", forbidden_stereo_assignment)

    normalized = []
    for molecule in (trans_source, cis_source):
        record, source_to_topology = normalize_topology_with_mapping(
            molecule,
            add_hydrogens=False,
            reconstruction_method="molgr/cpp",
            reconstruction_version="0.1.8",
        )
        normalized.append(record)
        assert source_to_topology == list(range(molecule.GetNumAtoms()))

    trans, cis = normalized
    assert trans.topology.canonical_isomeric_smiles is not None
    assert cis.topology.canonical_isomeric_smiles is not None
    assert "/" in trans.topology.canonical_isomeric_smiles
    assert "\\" in cis.topology.canonical_isomeric_smiles
    assert trans.topology.graph_hash != cis.topology.graph_hash
    assert trans.topology.stereo_status is StereoStatus.ASSIGNED
    assert cis.topology.stereo_status is StereoStatus.ASSIGNED

    chiral_record, _ = normalize_topology_with_mapping(
        chiral_source,
        add_hydrogens=False,
        reconstruction_method="molgr/cpp",
        reconstruction_version="0.1.8",
    )
    chiral_atom = chiral_record.topology.mol.GetAtomWithIdx(1)
    assert chiral_atom.HasProp("_CIPRank")
    assert chiral_atom.GetProp("_CIPCode") == "R"

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
    geometry_atom = full_record.geometry.mol.GetAtomWithIdx(1)
    assert geometry_atom.HasProp("_CIPRank")
    assert geometry_atom.GetProp("_CIPCode") == "R"


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
