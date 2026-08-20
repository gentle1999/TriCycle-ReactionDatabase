from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from nexusx import ComposedErManager  # type: ignore[import-untyped]

from tricycle_reaction_db.api.apps import (
    core_api_app,
    database_dtos,
    database_entities,
    graphql_playground_app,
    mcp_app,
    paginated_graphql_app,
    use_case_rest_app,
    voyager_app,
    voyager_er_manager,
)
from tricycle_reaction_db.api.core import MolecularTopologyCoreDTO
from tricycle_reaction_db.api.run_services import SERVICES


async def _client(app):  # type: ignore[no-untyped-def]
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=True,
    )


def test_nexusx_demo_port_contract() -> None:
    assert [(service.port, service.name) for service in SERVICES] == [
        (8000, "Direct-list GraphQL"),
        (8005, "Paginated GraphQL"),
        (8006, "UseCase MCP"),
        (8008, "Voyager visualization"),
    ]


@pytest.mark.asyncio
async def test_graphql_apps_expose_distinct_direct_and_paginated_schemas() -> None:
    async with await _client(graphql_playground_app) as client:
        playground = await client.get("/graphql")
        playground_schema = await client.get("/graphql/schema")
        system = await client.post(
            "/graphql",
            json={"query": "{ SystemService { info { name } } }"},
        )
    async with await _client(paginated_graphql_app) as client:
        paginated = await client.get("/graphql")
        paginated_schema = await client.get("/graphql/schema")

    assert playground.status_code == paginated.status_code == 200
    assert "Direct-list GraphQL - Example Research Platform" in playground.text
    assert "GraphQLCatalogService" in playground.text
    assert "Paginated GraphQL - Example Research Platform" in paginated.text
    assert "ArtifactQueryService" in paginated.text
    assert "GraphQLCatalogService" in playground_schema.text
    assert "list_artifacts(" in playground_schema.text
    assert "ArtifactQueryService" not in playground_schema.text
    assert "type Mutation" not in playground_schema.text
    assert "ArtifactQueryService" in paginated_schema.text
    assert "GraphQLCatalogService" not in paginated_schema.text
    assert "list_artifacts(" in paginated_schema.text
    assert "): ArtifactPage!" in paginated_schema.text
    assert system.json()["data"]["SystemService"]["info"]["name"] == ("Example Chemistry Database")


@pytest.mark.asyncio
async def test_core_and_use_case_rest_apps_keep_different_http_contracts() -> None:
    async with await _client(core_api_app) as client:
        core_root = await client.get("/")
        core_openapi = (await client.get("/openapi.json")).json()
        proxied_docs = await client.get(
            "/docs",
            headers={"x-forwarded-prefix": "/nexusx/core"},
        )
    async with await _client(use_case_rest_app) as client:
        use_case_root = await client.get("/")
        use_case_openapi = (await client.get("/openapi.json")).json()

    assert core_root.json()["mode"] == "DefineSubset + ErManager + Resolver"
    assert set(core_openapi["paths"]["/api/topologies"]) == {"get"}
    assert "mol" not in cast(Any, MolecularTopologyCoreDTO).model_fields
    assert set(
        use_case_openapi["paths"]["/api/calculation_query_service/list_calculation_frames"]
    ) == {"post"}
    assert set(
        use_case_openapi["paths"]["/api/molecular_formula_query_service/search_formulas"]
    ) == {"post"}
    assert set(
        use_case_openapi["paths"]["/api/molecular_topology_query_service/search_topologies"]
    ) == {"post"}
    topology_request = use_case_openapi["components"]["schemas"][
        "MolecularTopologyQueryServiceSearchTopologiesRequest"
    ]
    assert topology_request["properties"]["similarity_metric"]["default"] == "tanimoto"
    assert "CalculationQueryService" in use_case_root.json()["services"]
    assert "MolecularFormulaQueryService" in use_case_root.json()["services"]
    assert "MolecularTopologyQueryService" in use_case_root.json()["services"]
    assert "url: '/nexusx/core/openapi.json'" in proxied_docs.text


@pytest.mark.asyncio
async def test_voyager_renders_use_cases_and_compatible_database_entities() -> None:
    async with await _client(voyager_app) as client:
        root = await client.get("/")
        voyager = await client.get("/voyager")
        dot = await client.get("/voyager/dot")
        er_diagram = await client.post("/voyager/er-diagram", json={})

    assert voyager.status_code == dot.status_code == er_diagram.status_code == 200
    assert "nexusx-voyager-static" in voyager.text
    assert isinstance(voyager_er_manager, ComposedErManager)
    assert root.json()["entity_count"] == len(database_entities(voyager_compatible=True))
    assert root.json()["composite_relationship_models_omitted"] == 4
    assert "CalculationFrame" in dot.text
    use_case_payload = dot.json()
    system_info = next(
        schema for schema in use_case_payload["schemas"] if schema["name"] == "SystemInfo"
    )
    assert system_info["module"] == "example-chemistry-postgresql"
    assert 'label = "  example-chemistry-postgresql"' in use_case_payload["dot"]
    assert 'fillcolor = "#E3F2FD"' in use_case_payload["dot"]
    er_payload = er_diagram.json()
    assert {schema["module"] for schema in er_payload["schemas"]} == {
        "example-chemistry-postgresql"
    }
    assert 'label = "  example-chemistry-postgresql"' in er_payload["dot"]
    assert 'fillcolor = "#E3F2FD"' in er_payload["dot"]
    assert database_dtos()


@pytest.mark.asyncio
async def test_dedicated_mcp_app_uses_demo_compatible_path() -> None:
    async with await _client(mcp_app) as client:
        response = await client.get("/mcp")
        protocol_get = await client.get(
            "/mcp",
            headers={"accept": "application/json, text/event-stream"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "service": "UseCase MCP",
        "transport": "Streamable HTTP",
        "endpoint": "/mcp/",
        "method": "POST",
        "message": "This endpoint is for MCP clients, not a browser UI.",
        "request": {
            "accept": "application/json, text/event-stream",
            "content_type": "application/json",
        },
    }
    assert protocol_get.status_code == 405
    assert "POST" in protocol_get.headers["allow"]
