import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tricycle_reaction_db.api.routes import metrics as metrics_module


@pytest.mark.asyncio
async def test_internal_metrics_are_prometheus_formatted_and_hidden_from_openapi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refresh_database_rows() -> None:
        return None

    monkeypatch.setattr(
        metrics_module,
        "_refresh_database_row_metrics",
        refresh_database_rows,
    )
    application = FastAPI()
    application.include_router(metrics_module.router)

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/internal/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "tricycle_database_pool_connections" in response.text
    assert 'tricycle_database_pool_connections{state="overflow"} 0.0' in response.text
    assert "/internal/metrics" not in application.openapi()["paths"]
