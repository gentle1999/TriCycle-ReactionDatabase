import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tricycle_reaction_db.api.app import create_app
from tricycle_reaction_db.api.query_guards import install_query_guards
from tricycle_reaction_db.api.routes import graphql as graphql_module
from tricycle_reaction_db.application.query_cost import (
    FixedWindowRateLimiter,
    QueryBudgetExceeded,
    validate_graphql_query_budget,
)
from tricycle_reaction_db.application.rate_limits import RateLimitBackendUnavailable


class _UnavailableRateLimiter:
    maximum_requests = 10

    async def check(self, _key: str) -> None:
        raise RateLimitBackendUnavailable


def test_graphql_depth_and_complexity_budgets_are_ast_based() -> None:
    with pytest.raises(QueryBudgetExceeded, match="depth 5 exceeds"):
        validate_graphql_query_budget(
            "{ A { b { c { d { e } } } } }",
            maximum_characters=1_000,
            maximum_tokens=100,
            maximum_depth=4,
            maximum_complexity=100,
        )

    with pytest.raises(QueryBudgetExceeded, match="complexity 11 exceeds"):
        validate_graphql_query_budget(
            "{ A { rows(limit: 200) { id } } }",
            maximum_characters=1_000,
            maximum_tokens=100,
            maximum_depth=10,
            maximum_complexity=9,
        )


def test_fixed_window_limiter_resets_and_bounds_keys() -> None:
    limiter = FixedWindowRateLimiter(
        maximum_requests=2,
        window_seconds=10,
        maximum_keys=2,
    )

    assert limiter.check("first", now=0).allowed is True
    assert limiter.check("first", now=1).allowed is True
    rejected = limiter.check("first", now=2)
    assert rejected.allowed is False
    assert rejected.retry_after_seconds == 8
    assert limiter.check("first", now=10).allowed is True
    assert limiter.check("second", now=10).allowed is True
    assert limiter.check("third", now=10).allowed is True
    assert len(limiter._windows) == 2


@pytest.mark.asyncio
async def test_http_rate_limit_has_stable_error_and_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tricycle_reaction_db.api.query_guards.get_settings",
        lambda: SimpleNamespace(
            query_rate_limit_requests=1,
            read_rate_limit_requests=2,
            upload_rate_limit_requests=3,
            upload_max_concurrency=2,
            molecule_query_rate_limit_requests=2,
            depiction_rate_limit_requests=2,
            query_rate_limit_window_seconds=30,
        ),
    )
    application = FastAPI()
    install_query_guards(application)

    @application.patch("/management")
    async def management() -> dict[str, bool]:
        return {"ok": True}

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        accepted = await client.patch("/management")
        rejected = await client.patch("/management")

    assert accepted.status_code == 200
    assert accepted.headers["X-RateLimit-Remaining"] == "0"
    assert accepted.headers["X-RateLimit-Policy"] == "management"
    assert accepted.headers["X-RateLimit-Limit"] == "1"
    assert rejected.status_code == 429
    assert rejected.headers["Retry-After"] == "30"
    assert rejected.headers["X-RateLimit-Remaining"] == "0"
    assert rejected.json()["detail"]["code"] == "query_rate_limit_exceeded"


@pytest.mark.asyncio
async def test_http_rate_limit_backend_failure_returns_503_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tricycle_reaction_db.api.query_guards.get_settings",
        lambda: SimpleNamespace(
            query_rate_limit_requests=10,
            read_rate_limit_requests=10,
            upload_rate_limit_requests=10,
            upload_max_concurrency=2,
            molecule_query_rate_limit_requests=10,
            depiction_rate_limit_requests=10,
            query_rate_limit_window_seconds=30,
        ),
    )
    monkeypatch.setattr(
        "tricycle_reaction_db.api.query_guards.create_rate_limiter",
        lambda **_kwargs: _UnavailableRateLimiter(),
    )
    application = FastAPI()
    install_query_guards(application)

    @application.get("/query")
    async def query() -> dict[str, bool]:
        pytest.fail("the request must not continue without an authoritative rate limit")

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/query")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["detail"]["code"] == "rate_limit_backend_unavailable"


@pytest.mark.asyncio
async def test_depiction_rate_limit_is_independent_from_query_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tricycle_reaction_db.api.query_guards.get_settings",
        lambda: SimpleNamespace(
            query_rate_limit_requests=1,
            read_rate_limit_requests=1,
            upload_rate_limit_requests=3,
            upload_max_concurrency=2,
            molecule_query_rate_limit_requests=2,
            depiction_rate_limit_requests=2,
            query_rate_limit_window_seconds=30,
        ),
    )
    application = FastAPI()
    install_query_guards(application)

    @application.get("/query")
    async def query() -> dict[str, bool]:
        return {"ok": True}

    @application.get("/api/depictions/geometry/example.svg")
    async def depiction() -> dict[str, bool]:
        return {"ok": True}

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        assert (await client.get("/query")).status_code == 200
        assert (await client.get("/query")).status_code == 429
        assert (await client.get("/api/depictions/geometry/example.svg")).status_code == 200
        assert (await client.get("/api/depictions/geometry/example.svg")).status_code == 200
        assert (await client.get("/api/depictions/geometry/example.svg")).status_code == 429


@pytest.mark.asyncio
async def test_molecule_query_rate_limit_is_independent_from_general_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tricycle_reaction_db.api.query_guards.get_settings",
        lambda: SimpleNamespace(
            query_rate_limit_requests=1,
            read_rate_limit_requests=1,
            upload_rate_limit_requests=3,
            upload_max_concurrency=2,
            molecule_query_rate_limit_requests=2,
            depiction_rate_limit_requests=3,
            query_rate_limit_window_seconds=30,
        ),
    )
    application = FastAPI()
    install_query_guards(application)

    @application.get("/query")
    async def query() -> dict[str, bool]:
        return {"ok": True}

    @application.post("/api/geometry_query_service/list_geometries")
    async def geometry_query() -> dict[str, bool]:
        return {"ok": True}

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        assert (await client.get("/query")).status_code == 200
        assert (await client.get("/query")).status_code == 429
        assert (await client.post("/api/geometry_query_service/list_geometries")).status_code == 200
        assert (await client.post("/api/geometry_query_service/list_geometries")).status_code == 200
        assert (await client.post("/api/geometry_query_service/list_geometries")).status_code == 429


@pytest.mark.asyncio
async def test_read_upload_and_management_requests_use_separate_policies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tricycle_reaction_db.api.query_guards.get_settings",
        lambda: SimpleNamespace(
            query_rate_limit_requests=1,
            read_rate_limit_requests=2,
            upload_rate_limit_requests=3,
            upload_max_concurrency=2,
            molecule_query_rate_limit_requests=4,
            depiction_rate_limit_requests=5,
            query_rate_limit_window_seconds=30,
        ),
    )
    application = FastAPI()
    install_query_guards(application)

    @application.get("/api/auth/me")
    async def session() -> dict[str, bool]:
        return {"ok": True}

    @application.post("/api/artifacts")
    async def upload() -> dict[str, bool]:
        return {"ok": True}

    @application.patch("/api/artifacts/example")
    async def update() -> dict[str, bool]:
        return {"ok": True}

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        read = await client.get("/api/auth/me")
        upload = await client.post("/api/artifacts")
        management = await client.patch("/api/artifacts/example")

    assert (read.headers["X-RateLimit-Policy"], read.headers["X-RateLimit-Limit"]) == (
        "read",
        "2",
    )
    assert (
        upload.headers["X-RateLimit-Policy"],
        upload.headers["X-RateLimit-Limit"],
    ) == ("upload", "3")
    assert (
        management.headers["X-RateLimit-Policy"],
        management.headers["X-RateLimit-Limit"],
    ) == ("management", "1")


@pytest.mark.asyncio
async def test_parallel_uploads_are_bounded_without_becoming_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tricycle_reaction_db.api.query_guards.get_settings",
        lambda: SimpleNamespace(
            query_rate_limit_requests=10,
            read_rate_limit_requests=10,
            upload_rate_limit_requests=10,
            upload_max_concurrency=2,
            molecule_query_rate_limit_requests=10,
            depiction_rate_limit_requests=10,
            query_rate_limit_window_seconds=30,
        ),
    )
    application = FastAPI()
    install_query_guards(application)
    release = asyncio.Event()
    slots_filled = asyncio.Event()
    active = 0
    peak = 0

    @application.post("/api/artifacts")
    async def upload() -> dict[str, bool]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            slots_filled.set()
        try:
            await release.wait()
            return {"ok": True}
        finally:
            active -= 1

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        requests = [asyncio.create_task(client.post("/api/artifacts")) for _ in range(3)]
        await asyncio.wait_for(slots_filled.wait(), timeout=1)
        await asyncio.sleep(0)
        assert peak == 2
        release.set()
        responses = await asyncio.gather(*requests)

    assert [response.status_code for response in responses] == [200, 200, 200]


@pytest.mark.asyncio
async def test_graphql_transport_rejects_over_budget_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        graphql_module,
        "get_settings",
        lambda: SimpleNamespace(
            environment="test",
            graphql_max_query_characters=20_000,
            graphql_max_tokens=2_000,
            graphql_max_depth=2,
            graphql_max_complexity=250,
        ),
    )
    application = FastAPI()
    application.include_router(
        graphql_module.create_graphql_router(
            graphql_module.config,
            graphql_module.schema,
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/graphql",
            json={"query": "{ SystemService { info { name } } }"},
        )

    assert response.status_code == 400
    assert response.json()["errors"][0]["extensions"]["code"] == "query_budget_exceeded"


@pytest.mark.asyncio
async def test_rest_structure_budget_uses_stable_error_code() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/mapped_reaction_query_service/list_mapped_reactions",
            json={"similarity_reaction_smiles": "C" * 16_385, "limit": 1},
        )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "query_budget_exceeded"
