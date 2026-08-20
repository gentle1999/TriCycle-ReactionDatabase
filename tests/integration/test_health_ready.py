import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import make_url

from tricycle_reaction_db.api.app import create_app
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.session import dispose_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


@pytest.mark.asyncio
async def test_ready_health_endpoint_reports_rdkit_database() -> None:
    transport = ASGITransport(app=create_app())
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/ready")
    finally:
        await dispose_engine()

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["database"] == make_url(get_settings().database_url).database
    assert payload["postgresql_version"].startswith("18.")
    assert payload["rdkit_extension_version"] == "4.8.0"
