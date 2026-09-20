from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import and_
from sqlalchemy.dialects import postgresql
from sqlmodel import col, select

from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence import (
    _eligible_endpoint_candidate_geometries_statement,
    _endpoint_geometries_by_participant,
    _graph_hash_proves_endpoint_compatibility,
    _TopologyCompatibilityMetadata,
)
from tricycle_reaction_db.db.models import CalculationFrame, MolecularTopology
from tricycle_reaction_db.domain.enums import OptimizationStatus


def test_equal_versioned_graph_hash_proves_stereo_agnostic_compatibility() -> None:
    endpoint = _TopologyCompatibilityMetadata(
        topology_id=uuid4(),
        formula_id=uuid4(),
        atom_count=12,
        formal_charge=0,
        fragment_count=1,
        stereo_agnostic_graph_hash="a" * 64,
    )
    same_graph = _TopologyCompatibilityMetadata(
        topology_id=uuid4(),
        formula_id=endpoint.formula_id,
        atom_count=endpoint.atom_count,
        formal_charge=endpoint.formal_charge,
        fragment_count=endpoint.fragment_count,
        stereo_agnostic_graph_hash=endpoint.stereo_agnostic_graph_hash,
    )
    legacy_without_hash = _TopologyCompatibilityMetadata(
        topology_id=uuid4(),
        formula_id=endpoint.formula_id,
        atom_count=endpoint.atom_count,
        formal_charge=endpoint.formal_charge,
        fragment_count=endpoint.fragment_count,
        stereo_agnostic_graph_hash=None,
    )

    assert _graph_hash_proves_endpoint_compatibility(endpoint, same_graph)
    assert not _graph_hash_proves_endpoint_compatibility(endpoint, legacy_without_hash)
    assert not _graph_hash_proves_endpoint_compatibility(legacy_without_hash, same_graph)


def test_endpoint_candidate_query_filters_hash_and_eligibility_before_loading_geometry() -> None:
    project_id = uuid4()
    candidate_topology_id = uuid4()
    formula_id = uuid4()
    graph_hash = "a" * 64
    eligible_geometry_ids = select(col(CalculationFrame.geometry_id)).where(
        col(CalculationFrame.optimization_status) == OptimizationStatus.CONVERGED
    )
    candidate_metadata = and_(
        col(MolecularTopology.id).in_((candidate_topology_id,)),
        col(MolecularTopology.formula_id) == formula_id,
        col(MolecularTopology.atom_count) == 12,
        col(MolecularTopology.formal_charge) == 0,
        col(MolecularTopology.fragment_count) == 1,
        col(MolecularTopology.stereo_agnostic_graph_hash) == graph_hash,
    )

    statement = _eligible_endpoint_candidate_geometries_statement(
        project_id=project_id,
        eligible_geometry_ids=eligible_geometry_ids,
        candidate_metadata_predicates=(candidate_metadata,),
    )
    sql = str(statement.compile(dialect=postgresql.dialect())).lower()

    assert "join molecular_topology" in sql
    assert "molecular_topology.stereo_agnostic_graph_hash" in sql
    assert "molecular_topology.formula_id" in sql
    assert "calculation_frame.optimization_status" in sql
    assert "thermochemistry_result" in sql
    assert "calculation_frame.negative_frequency_count" in sql
    assert "molecular_topology.mol" not in sql


def test_endpoint_profile_uses_hash_qualified_dag_geometry_without_loading_mol(
    monkeypatch: Any,
) -> None:
    endpoint_id = uuid4()
    source_id = uuid4()
    participant_id = uuid4()
    logical_participant_id = uuid4()
    project_id = uuid4()
    formula_id = uuid4()
    graph_hash = "b" * 64
    source_geometry = SimpleNamespace(id=uuid4(), topology_id=source_id)

    class _Result:
        def __init__(self, rows: list[Any]) -> None:
            self.rows = rows

        def all(self) -> list[Any]:
            return self.rows

    class _Session:
        def __init__(self) -> None:
            self.responses = [
                [],
                [],
                [(endpoint_id, formula_id, 12, 0, 1, graph_hash)],
                [(source_id, formula_id, 12, 0, 1, graph_hash, source_geometry)],
            ]
            self.statements: list[Any] = []

        def exec(self, statement: Any) -> _Result:
            self.statements.append(statement)
            return _Result(self.responses.pop(0))

    session = _Session()

    def load_molecules(*_: Any, **__: Any) -> Any:
        raise AssertionError("equal graph hashes should not load topology MOL values")

    monkeypatch.setattr(
        "tricycle_reaction_db.application.services.topology_abstraction.topology_dag_components_by_root",
        lambda *_args, **_kwargs: {endpoint_id: (endpoint_id, source_id)},
    )
    monkeypatch.setattr(
        "tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence._load_topology_molecules",
        load_molecules,
    )

    participant_row = (
        cast(Any, SimpleNamespace(id=participant_id, concrete_topology_id=endpoint_id)),
        cast(Any, SimpleNamespace(id=logical_participant_id, topology_id=endpoint_id)),
    )
    result = _endpoint_geometries_by_participant(
        cast(Any, session),
        (participant_row,),
        project_id=project_id,
    )

    assert result[participant_id] == (source_geometry,)
    assert len(session.statements) == 4
