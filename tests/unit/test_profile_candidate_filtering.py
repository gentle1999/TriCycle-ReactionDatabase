from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy.dialects import postgresql

from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence import (
    _endpoint_geometries_by_participant,
)


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def all(self) -> list[Any]:
        return self.rows


class _Session:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.statements: list[Any] = []

    def exec(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self.rows)


def _load(
    geometries: list[Any],
    *,
    exact_topology_id: UUID | None = None,
) -> tuple[dict[UUID, tuple[Any, ...]], UUID, UUID, _Session]:
    participant_id = uuid4()
    exact_topology_id = exact_topology_id or uuid4()
    logical_topology_id = uuid4()
    project_id = uuid4()
    session = _Session(geometries)
    participant_row = (
        SimpleNamespace(id=participant_id, concrete_topology_id=exact_topology_id),
        SimpleNamespace(id=uuid4(), topology_id=logical_topology_id),
    )
    result = _endpoint_geometries_by_participant(
        cast(Any, session),
        (participant_row,),
        project_id=project_id,
    )
    return result, participant_id, exact_topology_id, session


def _assert_query_is_scoped_to_exact_topology(session: _Session, exact_topology_id: UUID) -> None:
    assert len(session.statements) == 1
    statement = session.statements[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled).lower()
    parameter_values = [
        value
        for parameter in compiled.params.values()
        for value in (parameter if isinstance(parameter, (tuple, list, set)) else (parameter,))
    ]

    assert "geometry.topology_id" in sql
    assert "molecular_topology" not in sql
    assert "logical_participant_concrete_topology" not in sql
    assert exact_topology_id in parameter_values


def test_endpoint_loader_rejects_sibling_topology_even_if_it_has_eligible_geometry() -> None:
    sibling_geometry = SimpleNamespace(id=uuid4(), topology_id=uuid4())

    result, participant_id, exact_topology_id, session = _load([sibling_geometry])

    _assert_query_is_scoped_to_exact_topology(session, exact_topology_id)
    assert result[participant_id] == ()


def test_endpoint_loader_keeps_only_geometry_from_exact_topology() -> None:
    exact_topology_id = uuid4()
    sibling_topology_id = uuid4()
    exact_geometry = SimpleNamespace(id=uuid4(), topology_id=exact_topology_id)
    sibling_geometry = SimpleNamespace(id=uuid4(), topology_id=sibling_topology_id)

    result, participant_id, actual_exact_topology_id, session = _load(
        [exact_geometry, sibling_geometry],
        exact_topology_id=exact_topology_id,
    )

    _assert_query_is_scoped_to_exact_topology(session, actual_exact_topology_id)
    assert result[participant_id] == (exact_geometry,)
