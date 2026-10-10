import csv
import io
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from tricycle_reaction_db.application.services import reaction_thermodynamic_analytics as analytics
from tricycle_reaction_db.application.services.reaction_thermodynamic_analytics import (
    ReactionThermodynamicAnalyticsService,
    _level_label,
)
from tricycle_reaction_db.domain.enums import MappedReactionKind


class _Stream:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = iter(rows)

    def __aiter__(self) -> AsyncIterator[tuple[Any, ...]]:
        return self

    async def __anext__(self) -> tuple[Any, ...]:
        try:
            return next(self._rows)
        except StopIteration as error:
            raise StopAsyncIteration from error


class _Session:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    async def stream(self, _statement: object) -> _Stream:
        return _Stream(self._rows)


class _SessionContext:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._session = _Session(rows)

    async def __aenter__(self) -> _Session:
        return self._session

    async def __aexit__(self, *_args: object) -> None:
        return None


def test_level_label_distinguishes_composite_levels() -> None:
    shared = ["DFT", "DFT", None, "B3LYP", "def2-SVP"]
    assert _level_label(shared, shared) == ("B3LYP/def2-SVP")
    assert _level_label(
        ["CC", "CCSD(T)", None, "DLPNO-CCSD(T)", "def2-TZVP"],
        shared,
    ) == ("B3LYP/def2-SVP//DLPNO-CCSD(T)/def2-TZVP")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frontend_base_url", ["https://reactions.example", "https://reactions.example/"]
)
async def test_export_csv_preserves_profile_columns_and_quotes_smiles(
    monkeypatch: pytest.MonkeyPatch,
    frontend_base_url: str,
) -> None:
    project_id = uuid4()
    mapped_id = uuid4()
    logical_id = uuid4()
    rows = [
        (
            mapped_id,
            logical_id,
            "path-1",
            MappedReactionKind.CURATED,
            "[CH3:1],[OH:2]>>[CH3:1][OH:2]",
            "a" * 64,
            "thermodynamic-profile-v1",
            ["DFT", "DFT", None, "B3LYP", "def2-SVP"],
            ["DFT", "DFT", None, "B3LYP", "def2-SVP"],
            298.15,
            1.0,
            10.0,
            20.0,
            30.0,
            55.0,
            12.25,
            13.5,
            -2.0,
            -3.25,
        )
    ]
    monkeypatch.setattr(
        "tricycle_reaction_db.application.services.reaction_thermodynamic_analytics.session_factory",
        lambda: _SessionContext(rows),
    )
    payload = "".join(
        [
            chunk
            async for chunk in ReactionThermodynamicAnalyticsService._export_csv_rows(
                True,
                project_id=project_id,
                frontend_base_url=frontend_base_url,
            )
        ]
    )
    exported = list(csv.DictReader(io.StringIO(payload)))

    assert len(exported) == 1
    assert exported[0]["mapped_reaction_id"] == str(mapped_id)
    assert exported[0]["mapped_reaction_smiles"] == rows[0][4]
    assert exported[0]["level_of_theory"] == "B3LYP/def2-SVP"
    assert exported[0]["reactants_running_time_seconds"] == "10.0"
    assert exported[0]["transition_state_running_time_seconds"] == "20.0"
    assert exported[0]["products_running_time_seconds"] == "30.0"
    assert exported[0]["total_running_time_seconds"] == "55.0"
    assert exported[0]["activation_gibbs_free_energy_kcal_mol"] == "13.5"
    assert exported[0]["reaction_gibbs_free_energy_kcal_mol"] == "-3.25"
    assert exported[0]["mapped_reaction_url"] == (
        f"https://reactions.example/mapped-reactions/{mapped_id}?project_id={project_id}"
    )


@pytest.mark.asyncio
async def test_export_csv_uses_the_same_visible_profile_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_project_id = uuid4()
    scope = object()
    predicate = object()
    observed: dict[str, Any] = {}

    async def resolve_scope(*, project_id: object) -> object:
        observed["project_id"] = project_id
        return scope

    async def profile_predicate(
        _session: object,
        requested_scope: object,
        **filters: object,
    ) -> object:
        observed["scope"] = requested_scope
        observed["filters"] = filters
        return predicate

    def export_rows(
        requested_predicate: object,
        *,
        project_id: object,
        frontend_base_url: str,
        limit: int | None = None,
        offset: int = 0,
    ) -> AsyncIterator[str]:
        observed["export"] = (requested_predicate, project_id, frontend_base_url, limit, offset)

        async def chunks() -> AsyncIterator[str]:
            yield "mapped_reaction_id\n"

        return chunks()

    class _PredicateSessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(analytics, "query_visibility_scope", resolve_scope)
    monkeypatch.setattr(analytics, "session_factory", _PredicateSessionContext)
    monkeypatch.setattr(analytics, "_profile_predicate", profile_predicate)
    monkeypatch.setattr(
        analytics,
        "get_settings",
        lambda: SimpleNamespace(oidc_frontend_url="https://reactions.example"),
    )
    monkeypatch.setattr(
        ReactionThermodynamicAnalyticsService,
        "_export_csv_rows",
        staticmethod(export_rows),
    )

    stream = await ReactionThermodynamicAnalyticsService.export_csv(
        requested_project_id,
        filter_expression='{"field":"reaction_hash","value":"test"}',
        limit=10,
        offset=20,
    )

    assert [chunk async for chunk in stream] == ["mapped_reaction_id\n"]
    assert observed == {
        "project_id": requested_project_id,
        "scope": scope,
        "filters": {
            "filter_expression": '{"field":"reaction_hash","value":"test"}',
            "has_activation_gibbs_free_energy": None,
            "has_reaction_gibbs_free_energy": None,
        },
        "export": (predicate, requested_project_id, "https://reactions.example", 10, 20),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("filtered", [False, True])
async def test_csv_routes_use_the_frontend_origin_behind_the_proxy(
    monkeypatch: pytest.MonkeyPatch,
    filtered: bool,
) -> None:
    from fastapi import Request

    from tricycle_reaction_db.api import core
    from tricycle_reaction_db.api.routes import auth

    project_id = uuid4()
    observed: dict[str, Any] = {}

    async def export_csv(**options: Any) -> AsyncIterator[str]:
        observed.update(options)

        async def chunks() -> AsyncIterator[str]:
            yield "mapped_reaction_url\n"

        return chunks()

    monkeypatch.setattr(
        ReactionThermodynamicAnalyticsService, "export_csv", staticmethod(export_csv)
    )
    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: SimpleNamespace(
            environment="development",
            auth_mode="development",
            oidc_frontend_url="http://127.0.0.1:5173",
        ),
    )
    request = Request(
        {
            "type": "http",
            "scheme": "http",
            "path": "/api/mapped-reactions/thermodynamics/export.csv",
            "headers": [
                (b"host", b"api:8000"),
                (b"x-forwarded-host", b"reactions.example"),
                (b"x-forwarded-proto", b"https"),
            ],
        }
    )
    if filtered:
        response = await core.export_filtered_mapped_reaction_thermodynamics(
            request, core.ReactionThermodynamicAnalyticsQuery(project_id=project_id)
        )
    else:
        response = await core.export_mapped_reaction_thermodynamics(request, project_id)

    assert response.media_type == "text/csv; charset=utf-8"
    assert observed["project_id"] == project_id
    assert observed["frontend_base_url"] == "https://reactions.example/"
