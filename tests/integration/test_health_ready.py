import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import make_url

from tricycle_reaction_db.api.app import create_app
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.session import dispose_engine
from tricycle_reaction_db.storage.rustfs import RustFSObjectStore, RustFSSettings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.rustfs,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1"
        or os.getenv("TRICYCLE_RUN_RUSTFS_TESTS") != "1",
        reason="set database and RustFS integration flags to run health readiness tests",
    ),
]


@pytest.mark.asyncio
async def test_ready_health_endpoint_reports_database_and_object_storage() -> None:
    with RustFSObjectStore(RustFSSettings()) as store:
        store.ensure_bucket()

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
    assert payload["object_storage"] == "ok"
