import json
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import TextContent

from tricycle_reaction_db.api.mcp import QueryGuardMiddleware, _mcp_success, mcp_server
from tricycle_reaction_db.application.dtos import ProjectView
from tricycle_reaction_db.application.rate_limits import RateLimitBackendUnavailable
from tricycle_reaction_db.domain.enums import ProjectStatus


async def _call(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await mcp_server.call_tool(tool_name, arguments)
    content = result.content[0]
    assert isinstance(content, TextContent)
    return json.loads(content.text)


@pytest.mark.asyncio
async def test_mcp_exposes_query_and_import_control_tools() -> None:
    tools = await mcp_server.list_tools()

    assert {tool.name for tool in tools} == {
        "list_apps",
        "describe_compose_schema",
        "describe_compose_method",
        "compose_query",
        "create_project",
        "register_import_manifest",
        "start_import_job",
        "get_import_status",
        "list_import_failures",
        "retry_import_items",
        "pause_import",
        "resume_import",
        "cancel_import",
    }


@pytest.mark.asyncio
async def test_mcp_control_tools_require_transport_authentication() -> None:
    result = await _call(
        "start_import_job", {"import_job_id": "00000000-0000-7000-8000-000000000701"}
    )

    assert result == {
        "success": False,
        "error": {
            "code": "authentication_required",
            "message": "authenticated MCP principal is required",
        },
    }


def test_mcp_success_serializes_nested_pydantic_payloads() -> None:
    project_id = "00000000-0000-7000-8000-000000000702"
    project = ProjectView.model_validate(
        {
            "id": project_id,
            "organization_id": "00000000-0000-7000-8000-000000000703",
            "organization_slug": "example-org",
            "organization_name": "Example Organization",
            "slug": "example-project",
            "name": "Example Project",
            "status": ProjectStatus.ACTIVE,
        }
    )

    result = _mcp_success({"job": project, "items": [project]})

    assert result["data"] == {
        "job": {
            "id": project_id,
            "organization_id": "00000000-0000-7000-8000-000000000703",
            "organization_slug": "example-org",
            "organization_name": "Example Organization",
            "owner_user_id": None,
            "created_by_user_id": None,
            "slug": "example-project",
            "name": "Example Project",
            "data_source": {},
            "model_checkpoint": {},
            "calculation_protocol": {},
            "status": "active",
            "role": None,
            "organization_role": None,
            "permissions": [],
            "created_at": None,
        },
        "items": [
            {
                "id": project_id,
                "organization_id": "00000000-0000-7000-8000-000000000703",
                "organization_slug": "example-org",
                "organization_name": "Example Organization",
                "owner_user_id": None,
                "created_by_user_id": None,
                "slug": "example-project",
                "name": "Example Project",
                "data_source": {},
                "model_checkpoint": {},
                "calculation_protocol": {},
                "status": "active",
                "role": None,
                "organization_role": None,
                "permissions": [],
                "created_at": None,
            }
        ],
    }


@pytest.mark.asyncio
async def test_mcp_progressive_disclosure_describes_query_service() -> None:
    apps = await _call("list_apps", {})
    schema = await _call(
        "describe_compose_schema",
        {"app_name": "example-chemistry-database"},
    )
    method = await _call(
        "describe_compose_method",
        {
            "app_name": "example-chemistry-database",
            "service_name": "CalculationQueryService",
            "method_name": "get_calculation_frame",
        },
    )

    assert apps["success"] is True
    assert apps["data"]["apps"][0]["name"] == "example-chemistry-database"
    assert schema["success"] is True
    assert "CalculationQueryService" in {service["name"] for service in schema["data"]["services"]}
    assert method["success"] is True
    assert method["data"]["method"]["return_type"] == "CalculationFrameDetail"
    assert "ScientificArraySummary" in method["data"]["sdl"]


@pytest.mark.asyncio
async def test_mcp_describes_automatically_registered_chemistry_searches() -> None:
    schema = await _call(
        "describe_compose_schema",
        {"app_name": "example-chemistry-database"},
    )
    topology_method = await _call(
        "describe_compose_method",
        {
            "app_name": "example-chemistry-database",
            "service_name": "MolecularTopologyQueryService",
            "method_name": "search_topologies",
        },
    )

    service_names = {service["name"] for service in schema["data"]["services"]}
    assert "MolecularFormulaQueryService" in service_names
    assert "MolecularTopologyQueryService" in service_names
    assert topology_method["success"] is True
    assert topology_method["data"]["method"]["return_type"] == "MolecularTopologySearchPage!"
    assert "SimilarityMetric" in topology_method["data"]["sdl"]


@pytest.mark.asyncio
async def test_mcp_describes_advanced_and_operational_queries() -> None:
    schema = await _call(
        "describe_compose_schema",
        {"app_name": "example-chemistry-database"},
    )
    result_method = await _call(
        "describe_compose_method",
        {
            "app_name": "example-chemistry-database",
            "service_name": "CalculationResultQueryService",
            "method_name": "get_calculation_results",
        },
    )
    service_names = {service["name"] for service in schema["data"]["services"]}
    assert {
        "CalculationResultQueryService",
        "WorkflowManifestQueryService",
        "StorageGarbageCollectionQueryService",
        "MolecularTopologyDerivationQueryService",
    } <= service_names
    assert result_method["success"] is True
    assert result_method["data"]["method"]["return_type"] == "CalculationResultDetail"
    assert "ElectronicStateSetView" in result_method["data"]["sdl"]


@pytest.mark.asyncio
async def test_mcp_compose_query_uses_graphql_envelope() -> None:
    result = await _call(
        "compose_query",
        {
            "app_name": "example-chemistry-database",
            "query": "{ SystemService { info { name version } } }",
        },
    )

    assert result["errors"] == []
    assert result["data"]["SystemService"]["info"]["name"] == ("Example Chemistry Database")


@pytest.mark.asyncio
async def test_mcp_structure_budget_uses_same_error_code() -> None:
    structure = "C" * 16_385
    query = (
        "{ MappedReactionQueryService { list_mapped_reactions(project_id: "
        '"00000000-0000-7000-8000-000000000201", '
        f'similarity_reaction_smiles: "{structure}", limit: 1) '
        "{ page { total } } } }"
    )
    result = await _call(
        "compose_query",
        {"app_name": "example-chemistry-database", "query": query},
    )

    assert result["data"] is None
    assert result["errors"][0]["extensions"]["code"] == "query_budget_exceeded"


@pytest.mark.asyncio
async def test_mcp_requires_project_scope_for_project_owned_queries() -> None:
    result = await _call(
        "compose_query",
        {
            "app_name": "example-chemistry-database",
            "query": (
                "{ MappedReactionQueryService { "
                "list_mapped_reactions(limit: 1) { items { id } } } }"
            ),
        },
    )

    assert result["data"] is None
    assert result["errors"][0]["extensions"]["code"] == "project_scope_required"


@pytest.mark.asyncio
async def test_mcp_rate_limit_backend_failure_returns_error_without_fallback() -> None:
    middleware = QueryGuardMiddleware()

    class _UnavailableRateLimiter:
        maximum_requests = 10

        async def check(self, _key: str) -> None:
            raise RateLimitBackendUnavailable

    middleware._limiter = _UnavailableRateLimiter()  # type: ignore[assignment]
    context = SimpleNamespace(
        message=SimpleNamespace(name="compose_query", arguments={}),
        fastmcp_context=None,
    )

    async def call_next(_context: Any) -> None:
        pytest.fail("MCP execution must not continue when Redis is unavailable")

    result = await middleware.on_call_tool(context, call_next)

    assert result.structured_content == {
        "data": None,
        "errors": [
            {
                "message": "shared rate-limit backend is unavailable",
                "extensions": {"code": "rate_limit_backend_unavailable"},
            }
        ],
    }
