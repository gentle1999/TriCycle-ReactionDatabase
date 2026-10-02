from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from rdkit import Chem

from tricycle_reaction_db.application.services import reactions, topology_abstraction
from tricycle_reaction_db.domain.enums import LogicalReactionParticipantSide as Side

PROJECT_ID = UUID("00000000-0000-7000-8000-000000000001")
FORMULA_ID = UUID("00000000-0000-7000-8000-000000000002")


def _topology(topology_id: int, smiles: str = "CCO") -> SimpleNamespace:
    molecule = Chem.MolFromSmiles(smiles)
    assert molecule is not None
    return SimpleNamespace(
        id=UUID(int=topology_id),
        project_id=PROJECT_ID,
        formula_id=FORMULA_ID,
        atom_count=3,
        mol=molecule,
    )


def test_map_transfer_uses_canonical_traversal_without_graph_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_topology = _topology(101)
    target_topology = _topology(102)
    target_topology.mol = Chem.MolFromSmiles("OCC")
    assert target_topology.mol is not None
    logical_topology = _topology(103)
    logical_participant = SimpleNamespace(topology=logical_topology)

    def fail_if_graph_matching_runs(*_args, **_kwargs):
        raise AssertionError("canonical traversal must not invoke graph matching")

    monkeypatch.setattr(
        topology_abstraction, "topology_abstraction_mapping_witness", fail_if_graph_matching_runs
    )
    monkeypatch.setattr(topology_abstraction, "find_topology_matches", fail_if_graph_matching_runs)

    transferred = reactions._transferred_atom_maps(
        cast(Any, object()),
        logical_participant=cast(Any, logical_participant),
        source_topology=cast(Any, source_topology),
        source_atom_maps=(11, 22, 33),
        target_topology=cast(Any, target_topology),
    )

    assert transferred == [33, 22, 11]


def test_complete_transfer_rejects_local_transform_that_changes_reaction_map_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_reactant = _topology(201, "CCO")
    target_reactant = _topology(202, "CCO")
    target_reactant.mol = Chem.RenumberAtoms(source_reactant.mol, [1, 0, 2])
    source_product = _topology(203, "CCO")
    logical_reactant = _topology(204, "CCO")
    logical_product = _topology(205, "CCO")
    topologies_by_id = {
        topology.id: topology
        for topology in (
            source_reactant,
            target_reactant,
            source_product,
            logical_reactant,
            logical_product,
        )
    }
    monkeypatch.setattr(
        reactions,
        "_resolve_topology_value",
        lambda _session, value: value if hasattr(value, "mol") else topologies_by_id[value],
    )
    monkeypatch.setattr(
        reactions,
        "_transferred_atom_maps",
        lambda _session, *, source_atom_maps, **_kwargs: list(source_atom_maps),
    )
    source_participants = [
        SimpleNamespace(
            id=UUID(int=topology_id),
            side=side,
            template_index=0,
            concrete_topology_id=topology.id,
            logical_reaction_participant=SimpleNamespace(topology=logical_topology),
            atom_map_numbers=[1, 2, 3],
        )
        for topology_id, side, topology, logical_topology in (
            (211, Side.REACTANT, source_reactant, logical_reactant),
            (212, Side.PRODUCT, source_product, logical_product),
        )
    ]
    mapped_reaction = SimpleNamespace(id=UUID(int=213), participants=source_participants)

    with pytest.raises(ValueError, match="atom-map links"):
        reactions.transfer_mapped_reaction_to_concrete_topologies(
            cast(Any, object()),
            cast(Any, mapped_reaction),
            {
                (Side.REACTANT, 0): target_reactant,
                (Side.PRODUCT, 0): source_product,
            },
        )
