from types import SimpleNamespace
from uuid import uuid4

import pytest
from rdkit import Chem

from tricycle_reaction_db.application.services.reaction_topology_representatives import (
    _maps_on_equivalent_topology,
)
from tricycle_reaction_db.application.services.reactions import mapped_smiles_for_topology


@pytest.fixture(autouse=True)
def forbid_subgraph_search(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("canonical correspondence only")

    for method in ("GetSubstructMatch", "GetSubstructMatches", "HasSubstructMatch"):
        monkeypatch.setattr(Chem.Mol, method, forbidden)


def fixture():
    mol = Chem.MolFromSmiles("F/C=C/Cl")
    project = uuid4()
    topology = SimpleNamespace(mol=mol, project_id=project, atom_count=4)
    participant = SimpleNamespace(
        atom_map_numbers=[7, 8, 9, 10],
        mapped_smiles=mapped_smiles_for_topology(topology, [7, 8, 9, 10]),
    )
    return mol, topology, participant


def test_representative_transfer_preserves_labelled_stereo_graph_in_new_atom_order():
    mol, source, participant = fixture()
    target = SimpleNamespace(
        mol=Chem.RenumberAtoms(mol, [3, 1, 0, 2]), project_id=source.project_id, atom_count=4
    )
    assert _maps_on_equivalent_topology(participant, source, target) == [10, 8, 7, 9]


@pytest.mark.parametrize("smiles", ["F/C=C\\Cl", "FC=CF", "[18F]/C=C/Cl"])
def test_representative_transfer_rejects_changed_stereo_connectivity_or_isotope(smiles):
    _, source, participant = fixture()
    target = SimpleNamespace(
        mol=Chem.MolFromSmiles(smiles), project_id=source.project_id, atom_count=4
    )
    assert _maps_on_equivalent_topology(participant, source, target) is None


def test_representative_transfer_does_not_cross_projects():
    mol, source, participant = fixture()
    target = SimpleNamespace(mol=mol, project_id=uuid4(), atom_count=4)
    assert _maps_on_equivalent_topology(participant, source, target) is None


def test_endpoint_mapping_persistence_validates_geometry_order_not_topology_order(monkeypatch):
    from tricycle_reaction_db.application.dtos.reactions import (
        MappedReactionNodeGeometryMappingRecord,
    )
    from tricycle_reaction_db.application.services import reactions
    from tricycle_reaction_db.domain.enums import MappedReactionNodeRole

    molecule = Chem.MolFromSmiles("CCO")
    topology = SimpleNamespace(mol=molecule, atom_count=3)
    expected = mapped_smiles_for_topology(topology, [1, 2, 3])
    geometry = SimpleNamespace(
        mol=Chem.RenumberAtoms(molecule, [2, 0, 1]), atom_count=3, topology=topology
    )
    participant = SimpleNamespace(atom_map_numbers=[1, 2, 3], mapped_smiles=expected)
    binding = SimpleNamespace(
        id=uuid4(),
        geometry=geometry,
        mapped_reaction_participant=participant,
        mapped_reaction_node=SimpleNamespace(
            role=MappedReactionNodeRole.REACTANT, mapped_reaction=SimpleNamespace()
        ),
    )
    monkeypatch.setattr(reactions, "_logical_map_numbers_for_reaction", lambda *a, **kw: {1, 2, 3})
    monkeypatch.setattr(reactions, "_new_entity", lambda *a, **kw: SimpleNamespace(**kw))
    monkeypatch.setattr(reactions, "_flush_new_entity", lambda *a, **kw: None)
    result = reactions.persist_mapped_reaction_node_geometry_mapping(
        None,
        binding,
        MappedReactionNodeGeometryMappingRecord(
            geometry_atom_map_numbers=[3, 1, 2],
            mapped_smiles=expected,
            mapping_method="canonical",
            mapping_version="1",
            verified=True,
        ),
        identity_is_new=True,
    )
    assert result.geometry_atom_map_numbers == [3, 1, 2]
