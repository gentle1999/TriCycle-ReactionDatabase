from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import UUID

import pytest

from tricycle_reaction_db.application.services import database_statistics

PROJECT_ID = UUID("00000000-0000-7000-0000-000000000001")


class _FakeConnection:
    def __init__(self, *, fail_on_analyze: bool = False) -> None:
        self.statements: list[str] = []
        self.committed = False
        self.rolled_back = False
        self.fail_on_analyze = fail_on_analyze

    async def execute(self, statement: object) -> None:
        sql = str(statement)
        self.statements.append(sql)
        if self.fail_on_analyze and sql.startswith("ANALYZE"):
            raise RuntimeError("database unavailable")

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _FakeEngine:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def connect(self):
        yield self.connection


@pytest.mark.asyncio
async def test_project_statistics_refresh_analyzes_the_allow_list_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection()
    monkeypatch.setattr(database_statistics, "engine", _FakeEngine(connection))

    assert await database_statistics.refresh_project_statistics(
        [PROJECT_ID, PROJECT_ID],
        reason="test",
    )

    assert connection.committed is True
    assert connection.rolled_back is False
    assert connection.statements[0] == "SET LOCAL statement_timeout = '10min'"
    assert connection.statements[1:] == [
        f"ANALYZE {table} ({', '.join(columns)})"
        for table, columns in database_statistics.PROJECT_STATISTICS_ANALYZE_TARGETS
    ]


@pytest.mark.asyncio
async def test_statistics_refresh_is_best_effort_after_database_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(fail_on_analyze=True)
    monkeypatch.setattr(database_statistics, "engine", _FakeEngine(connection))

    assert (
        await database_statistics.refresh_project_statistics([PROJECT_ID], reason="test")
    ) is False
    assert connection.committed is False
    assert connection.rolled_back is True
