import pytest
from httpx import ASGITransport, AsyncClient

from tricycle_reaction_db.api.app import create_app


@pytest.mark.asyncio
async def test_live_health_endpoint() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["version"]


@pytest.mark.asyncio
async def test_nexusx_system_info_route() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/system_service/info", json={})

    assert response.status_code == 200
    assert response.json()["name"] == "Example Chemistry Database"
    assert response.json()["version"]
