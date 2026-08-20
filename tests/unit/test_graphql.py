from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from tricycle_reaction_db.api.app import create_app
from tricycle_reaction_db.api.routes import graphql as graphql_module


@pytest.mark.asyncio
async def test_graphql_executes_system_use_case() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/graphql",
            json={"query": "{ SystemService { info { name version environment } } }"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["errors"] == []
    assert payload["data"]["SystemService"]["info"]["name"] == ("Example Chemistry Database")


@pytest.mark.asyncio
async def test_graphql_uses_nexusx_strict_selection_validation() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        unknown_field = await client.post(
            "/graphql",
            json={"query": "{ SystemService { info { name missing_field } } }"},
        )
        scalar_subselection = await client.post(
            "/graphql",
            json={"query": "{ SystemService { info { name { child } } } }"},
        )

    assert unknown_field.status_code == scalar_subselection.status_code == 200
    unknown_payload = unknown_field.json()
    scalar_payload = scalar_subselection.json()
    assert unknown_payload["data"] is None
    assert "Unknown field 'missing_field'" in unknown_payload["errors"][0]["message"]
    assert scalar_payload["data"] is None
    assert "cannot have sub-selection" in scalar_payload["errors"][0]["message"]


@pytest.mark.asyncio
async def test_graphql_supports_http_introspection() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/graphql",
            json={"query": "{ __schema { queryType { name } mutationType { name } } }"},
        )

    assert response.status_code == 200
    schema = response.json()["data"]["__schema"]
    assert schema["queryType"]["name"] == "Query"
    assert schema["mutationType"]["name"] == "Mutation"


@pytest.mark.asyncio
async def test_graphql_rejects_variables_with_standard_error_envelope() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/graphql",
            json={
                "query": "query Info($unused: String) { SystemService { info { name } } }",
                "variables": {"unused": "value"},
            },
        )

    assert response.status_code == 400
    payload = response.json()
    assert payload["data"] is None
    assert "does not support variables" in payload["errors"][0]["message"]


@pytest.mark.asyncio
async def test_graphiql_is_development_only(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        development = await client.get("/graphql")

        monkeypatch.setattr(
            graphql_module,
            "get_settings",
            lambda: SimpleNamespace(environment="production"),
        )
        production = await client.get("/graphql")

    assert development.status_code == 200
    assert "/graphql" in development.text
    assert production.status_code == 404


@pytest.mark.asyncio
async def test_graphiql_uses_the_forwarded_frontend_prefix() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/graphql",
            headers={"x-forwarded-prefix": "/nexusx"},
        )

    assert response.status_code == 200
    assert "url: '/nexusx/graphql'" in response.text


@pytest.mark.asyncio
async def test_graphiql_accepts_a_distinct_public_proxy_prefix() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/graphql",
            headers={
                "x-forwarded-prefix": "/nexusx",
                "x-nexusx-graphiql-prefix": "/paginated-graphql",
            },
        )

    assert response.status_code == 200
    assert "url: '/nexusx/paginated-graphql'" in response.text


@pytest.mark.asyncio
async def test_combined_app_exposes_distinct_direct_and_paginated_graphiql() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        direct = await client.get(
            "/graphql-playground",
            headers={"x-forwarded-prefix": "/nexusx"},
        )
        direct_schema = await client.get("/graphql-playground/schema")
        paginated = await client.get("/graphql")
        paginated_schema = await client.get("/graphql/schema")

    assert direct.status_code == paginated.status_code == 200
    assert "<title>Direct-list GraphQL - Example Research Platform</title>" in direct.text
    assert "url: '/nexusx/graphql'" in direct.text
    assert "GraphQLCatalogService" in direct.text
    assert "<title>Paginated GraphQL - Example Research Platform</title>" in paginated.text
    assert "ArtifactQueryService" in paginated.text
    assert "GraphQLCatalogService" in direct_schema.text
    assert "GraphQLCatalogService" not in paginated_schema.text


@pytest.mark.asyncio
async def test_graphql_schema_endpoint_exposes_allowlisted_mutation_sdl() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/graphql/schema")

    assert response.status_code == 200
    assert "type Query" in response.text
    assert "type Mutation" in response.text
    assert "create_reaction(" in response.text
    assert "reaction: String!" in response.text
    assert "CreateReactionCommand" not in response.text


@pytest.mark.asyncio
async def test_graphql_structure_budget_uses_stable_error_code() -> None:
    transport = ASGITransport(app=create_app())
    structure = "C" * 16_385
    query = (
        "{ MappedReactionQueryService { list_mapped_reactions("
        f'similarity_reaction_smiles: "{structure}", limit: 1) '
        "{ page { total } } } }"
    )
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/graphql", json={"query": query})

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"] is None
    assert payload["errors"][0]["extensions"]["code"] == "query_budget_exceeded"
