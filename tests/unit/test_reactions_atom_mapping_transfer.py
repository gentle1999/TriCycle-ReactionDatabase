from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from rdkit import Chem

from tricycle_reaction_db.application.services import reactions, topology_abstraction

PROJECT_ID = UUID("00000000-0000-7000-8000-000000000001")
FORMULA_ID = UUID("00000000-0000-7000-8000-000000000002")


def _topology(topology_id: int) -> SimpleNamespace:
    molecule = Chem.MolFromSmiles("CCO")
    assert molecule is not None
    return SimpleNamespace(
        id=UUID(int=topology_id),
        project_id=PROJECT_ID,
        formula_id=FORMULA_ID,
        atom_count=3,
        mol=molecule,
    )


def test_map_transfer_uses_unique_trusted_dag_correspondences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_topology = _topology(101)
    target_topology = _topology(102)
    logical_topology = _topology(103)
    logical_participant = SimpleNamespace(topology=logical_topology)

    def mapping_witness(_session, specific, general, **_kwargs):
        assert general is logical_topology
        if specific is source_topology:
            return (1, 0, 2)
        if specific is target_topology:
            return (0, 2, 1)
        raise AssertionError("unexpected topology mapping request")

    def fail_if_graph_matching_runs(*_args, **_kwargs):
        raise AssertionError("trusted DAG mappings should avoid full graph matching")

    monkeypatch.setattr(
        topology_abstraction,
        "topology_abstraction_mapping_witness",
        mapping_witness,
    )
    monkeypatch.setattr(topology_abstraction, "find_topology_matches", fail_if_graph_matching_runs)

    transferred = reactions._transferred_atom_maps(
        cast(Any, object()),
        logical_participant=cast(Any, logical_participant),
        source_topology=cast(Any, source_topology),
        source_atom_maps=(11, 22, 33),
        target_topology=cast(Any, target_topology),
    )

    assert transferred == [22, 33, 11]
