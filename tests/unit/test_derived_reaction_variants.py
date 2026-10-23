"""Generated assignments reuse configurations without collapsing observed paths."""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from rdkit import Chem

from tricycle_reaction_db.application.services import reaction_mapping_resolution as resolution
from tricycle_reaction_db.application.services.canonical_reaction_identity import (
    canonical_reaction_identity,
    derived_reaction_variant_key,
)
from tricycle_reaction_db.domain.enums import LogicalReactionParticipantSide as Side


@pytest.fixture(autouse=True)
def prohibit_subgraph_mapping(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("mapping must use canonical representations, never subgraph search")

    for method in ("GetSubstructMatch", "GetSubstructMatches", "HasSubstructMatch"):
        monkeypatch.setattr(Chem.Mol, method, forbidden)


def fixture_components():
    data = json.loads(
        (
            Path(__file__).parents[1]
            / "fixtures/reaction_normalization/derived_stereo_assignments.json"
        ).read_text()
    )
    parser = Chem.SmilesParserParams()
    parser.removeHs = False
    results = []
    for reaction in data["mapped_smiles"]:
        components = {}
        for side, smiles in zip((Side.REACTANT, Side.PRODUCT), reaction.split(">>"), strict=True):
            molecule = Chem.MolFromSmiles(smiles, parser)
            maps = [a.GetAtomMapNum() for a in molecule.GetAtoms()]
            for atom in molecule.GetAtoms():
                atom.SetAtomMapNum(0)
            components[(side, 0)] = (molecule, maps)
        results.append(components)
    return results


def strict_identity(components):
    return canonical_reaction_identity(
        {
            side: [value for (s, _), value in components.items() if s == side]
            for side in (Side.REACTANT, Side.PRODUCT)
        }
    ).smiles


def test_derived_example_reuses_configuration_but_observed_identity_stays_strict():
    first, _, _, derived = fixture_components()
    assert derived_reaction_variant_key(first) == derived_reaction_variant_key(derived)
    assert strict_identity(first) != strict_identity(derived)


def test_distinct_endpoint_stereo_configurations_remain_distinct():
    first, ee, zz, _ = fixture_components()
    assert len({derived_reaction_variant_key(c) for c in (first, ee, zz)}) == 3


def test_same_endpoints_do_not_collapse_distinct_connectivity_correspondence():
    ring = Chem.MolFromSmiles("[C]1[C][C][C][C][C]1")
    labels = list(range(1, 7))
    source = {(side, 0): (ring, labels) for side in (Side.REACTANT, Side.PRODUCT)}
    different = {**source, (Side.PRODUCT, 0): (ring, [2, 1, 3, 4, 5, 6])}
    assert derived_reaction_variant_key(source) != derived_reaction_variant_key(different)


def test_expansion_finds_existing_variant_instead_of_creating_another(monkeypatch):
    first, _, zz, derived = fixture_components()
    project = uuid4()
    existing = SimpleNamespace(id=uuid4(), project_id=project)
    topologies = {}
    participants = []
    for (side, index), (mol, maps) in first.items():
        topology_id = uuid4()
        topologies[topology_id] = SimpleNamespace(mol=mol)
        participants.append(
            SimpleNamespace(
                concrete_topology_id=topology_id,
                side=side,
                template_index=index,
                atom_map_numbers=maps,
            )
        )
    monkeypatch.setattr(resolution, "_source_participants", lambda *a, **kw: participants)
    monkeypatch.setattr(resolution, "_complete_mapped_reaction", lambda *a, **kw: True)
    monkeypatch.setattr(
        resolution, "_molecular_topology_by_id", lambda _s, tid, **kw: topologies[tid]
    )
    assert (
        resolution._existing_derived_variant(None, (existing,), derived, project_id=project)
        is existing
    )
    assert resolution._existing_derived_variant(None, (existing,), zz, project_id=project) is None
    assert (
        resolution._existing_derived_variant(None, (existing,), derived, project_id=uuid4()) is None
    )


def test_geometry_reconciliation_uses_coordinates_order_and_preserves_verified_transform():
    from tricycle_reaction_db.application.services.reaction_geometry_reconciliation import (
        _participant_geometry_atom_maps,
    )
    from tricycle_reaction_db.application.services.reactions import mapped_smiles_for_geometry

    molecule = Chem.MolFromSmiles("CCO")
    topology = SimpleNamespace(mol=molecule)
    original = SimpleNamespace(mol=molecule, atom_count=3, topology=topology)
    reordered = SimpleNamespace(
        mol=Chem.RenumberAtoms(molecule, [2, 0, 1]), atom_count=3, topology=topology
    )
    participant = SimpleNamespace(
        atom_map_numbers=[1, 2, 3],
        concrete_topology=topology,
        mapped_smiles=mapped_smiles_for_geometry(original, [1, 2, 3]),
    )
    assert _participant_geometry_atom_maps(participant, reordered) == [3, 1, 2]
    existing = SimpleNamespace(geometry_atom_map_numbers=[3, 1, 2], verified=True)
    assert _participant_geometry_atom_maps(participant, reordered, existing_mapping=existing) == [
        3,
        1,
        2,
    ]


def test_geometry_reconciliation_rejects_invalid_existing_transform():

    from tricycle_reaction_db.application.services.reaction_geometry_reconciliation import (
        _participant_geometry_atom_maps,
    )
    from tricycle_reaction_db.application.services.reactions import mapped_smiles_for_geometry

    molecule = Chem.MolFromSmiles("CCO")
    geometry = SimpleNamespace(mol=molecule, atom_count=3)
    participant = SimpleNamespace(mapped_smiles=mapped_smiles_for_geometry(geometry, [1, 2, 3]))
    existing = SimpleNamespace(geometry_atom_map_numbers=[3, 2, 1], verified=True)
    with pytest.raises(ValueError, match="fails stereo graph validation"):
        _participant_geometry_atom_maps(participant, geometry, existing_mapping=existing)
