import pytest
from molgr.utils.converter import METAL_UNPAIRED_ELECTRONS_PROP as METAL_SPIN
from rdkit import Chem

from tricycle_reaction_db.ingestion.normalization import (
    infer_molgr_stereochemistry_from_3d,
    normalize_topology,
    serialize_molecule_smiles,
)


def test_coordinate_assignment_discards_stale_cip_on_complete_coordination_graph(monkeypatch):
    molecule = Chem.AddHs(Chem.MolFromSmiles("[Fe]<-N/C=C/F"))
    molecule.GetAtomWithIdx(0).SetIntProp(METAL_SPIN, 4)
    conformer = Chem.Conformer(molecule.GetNumAtoms())
    conformer.Set3D(True)
    molecule.AddConformer(conformer)
    for atom in molecule.GetAtoms():
        atom.SetIntProp("_CIPRank", 999)
        atom.SetProp("_CIPCode", "stale")

    def assign(final_graph, **kwargs):
        assert all(not a.HasProp("_CIPRank") for a in final_graph.GetAtoms())
        assert all(not a.HasProp("_CIPCode") for a in final_graph.GetAtoms())
        assert final_graph.GetAtomWithIdx(0).GetIntProp(METAL_SPIN) == 4
        assert any(b.GetBondType() == Chem.BondType.DATIVE for b in final_graph.GetBonds())

    monkeypatch.setattr(Chem, "AssignStereochemistryFrom3D", assign)
    result = infer_molgr_stereochemistry_from_3d(molecule)
    assert result is not molecule
    assert molecule.GetAtomWithIdx(0).GetProp("_CIPCode") == "stale"


@pytest.mark.parametrize("metal", ["Al", "Ag", "Fe"])
def test_normalized_mol_retains_spin_while_smiles_round_trips(metal: str) -> None:
    molecule = Chem.AddHs(Chem.MolFromSmiles(f"[{metal}]<-N/C=C/F"))
    atom = molecule.GetAtomWithIdx(0)
    atom.SetIntProp(METAL_SPIN, 1)
    atom.SetNumRadicalElectrons(0)
    record = normalize_topology(
        molecule,
        add_hydrogens=False,
        reconstruction_method="molgr/cpp",
        reconstruction_version="test",
    ).topology
    restored_metal = next(a for a in record.mol.GetAtoms() if a.GetSymbol() == metal)
    assert restored_metal.GetIntProp(METAL_SPIN) == 1
    assert restored_metal.GetNumRadicalElectrons() == 0
    assert record.radical_electron_count == 1
    assert record.model_dump()["mol_atom_properties"][str(restored_metal.GetIdx())] == 1
    smiles = serialize_molecule_smiles(record.mol)
    parser = Chem.SmilesParserParams()
    parser.removeHs = False
    reparsed = Chem.MolFromSmiles(smiles, parser)
    assert reparsed is not None
    assert serialize_molecule_smiles(reparsed) == smiles
    assert any(b.GetBondType() == Chem.BondType.DATIVE for b in record.mol.GetBonds())
    assert molecule.GetAtomWithIdx(0).GetIntProp(METAL_SPIN) == 1


def test_metal_spin_is_part_of_topology_identity_and_survives_atom_reordering() -> None:
    molecule = Chem.AddHs(Chem.MolFromSmiles("[Fe]<-N/C=C/F"))

    def normalized(mol):
        return normalize_topology(
            mol,
            add_hydrogens=False,
            reconstruction_method="molgr/cpp",
            reconstruction_version="test",
        ).topology

    molecule.GetAtomWithIdx(0).SetIntProp(METAL_SPIN, 0)
    first = normalized(molecule)
    molecule.GetAtomWithIdx(0).SetIntProp(METAL_SPIN, 4)
    second = normalized(molecule)
    reordered = normalized(
        Chem.RenumberAtoms(molecule, list(reversed(range(molecule.GetNumAtoms()))))
    )
    assert first.graph_hash != second.graph_hash
    assert first.stereo_agnostic_graph_hash != second.stereo_agnostic_graph_hash
    assert second.graph_hash == reordered.graph_hash
    assert second.stereo_agnostic_graph_hash == reordered.stereo_agnostic_graph_hash


def test_ordinary_radical_mismatch_remains_an_error() -> None:
    molecule = Chem.MolFromSmiles("[CH3]")
    molecule.GetAtomWithIdx(0).SetNumRadicalElectrons(2)
    with pytest.raises(ValueError, match="radical-electron assignments"):
        serialize_molecule_smiles(molecule)


def test_trusted_boron_zero_radicals_survive_sidecar_and_identity() -> None:
    from tricycle_reaction_db.domain.mol_properties import (
        mol_atom_properties,
        restore_mol_atom_properties,
    )

    molecule = Chem.MolFromSmiles("C[N+](C)(C)[B-]Cl")
    boron = next(a for a in molecule.GetAtoms() if a.GetSymbol() == "B")
    assert boron.GetNumRadicalElectrons() == 2

    def normalize():
        return normalize_topology(
            molecule,
            add_hydrogens=True,
            reconstruction_method="molgr/cpp",
            reconstruction_version="test",
        ).topology

    before = normalize()
    boron.SetNumRadicalElectrons(0)
    record = normalize()
    assert before.graph_hash != record.graph_hash
    properties = mol_atom_properties(record.mol)
    restored = Chem.Mol(record.mol.ToBinary())
    target = next(a for a in restored.GetAtoms() if a.GetSymbol() == "B")
    target.SetNumRadicalElectrons(2)
    restore_mol_atom_properties(restored, properties)
    assert target.GetNumRadicalElectrons() == 0
    assert serialize_molecule_smiles(record.mol)


@pytest.mark.parametrize(
    "stereo,cleared", [(Chem.BondStereo.STEREOZ, True), (Chem.BondStereo.STEREOE, False)]
)
def test_coordination_ring_removes_only_topologically_implicit_cis(stereo, cleared):
    from tricycle_reaction_db.ingestion.normalization import _normalize_coordination_ring_stereo

    molecule = Chem.MolFromSmiles("[Rh]1<-CC=CCC->1")
    bond = next(b for b in molecule.GetBonds() if b.GetBondType() == Chem.BondType.DOUBLE)
    bond.SetStereoAtoms(bond.GetBeginAtomIdx() - 1, bond.GetEndAtomIdx() + 1)
    bond.SetStereo(stereo)
    _normalize_coordination_ring_stereo(molecule)
    assert (bond.GetStereo() == Chem.BondStereo.STEREONONE) == cleared
