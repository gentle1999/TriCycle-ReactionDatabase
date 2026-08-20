"""Standard GraphQL HTTP transport for the NexusX UseCase compose schema."""

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from nexusx import UseCaseAppConfig  # type: ignore[import-untyped]
from nexusx.graphiql import GRAPHIQL_HTML  # type: ignore[import-untyped]
from nexusx.use_case.compose_executor import (  # type: ignore[import-untyped]
    compose_introspect,
    execute_compose_query,
    is_introspection_query,
)
from pydantic import BaseModel, ConfigDict, Field

from tricycle_reaction_db.api.nexusx import config, schema
from tricycle_reaction_db.application.query_cost import (
    QueryBudgetExceeded,
    QueryStatementTimeout,
    graphql_error_result,
    normalize_graphql_query_errors,
    validate_graphql_query_budget,
)
from tricycle_reaction_db.core.config import get_settings

DIRECT_PLAYGROUND_QUERY = """# 快速浏览：直接取得一个小型数组，不需要处理分页对象。
# 点击 Execute Query 执行；右上角 Docs 可展开其他字段。
# NexusX Compose 不支持 GraphQL variables，请将参数直接写在查询中。
{
  GraphQLCatalogService {
    list_artifacts(limit: 5) {
      id
      original_filename
      artifact_kind
      size_bytes
    }
  }
}"""

PAGINATED_QUERY = """# 分页查询：items 是本页数据，page 给出总数、limit 和 offset。
# 点击 Execute Query 执行；修改 limit/offset 后再次执行可继续翻页。
# NexusX Compose 不支持 GraphQL variables，请将参数直接写在查询中。
{
  ArtifactQueryService {
    list_artifacts(limit: 5, offset: 0) {
      items {
        id
        original_filename
        artifact_kind
        size_bytes
      }
      page {
        total
        limit
        offset
      }
    }
  }
}"""


class GraphQLRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    query: str = Field(min_length=1, max_length=20_000)
    variables: dict[str, Any] | None = None
    operation_name: str | None = Field(default=None, alias="operationName")


def create_graphql_router(
    app_config: UseCaseAppConfig,
    compose_schema: Any,
    *,
    prefix: str = "/graphql",
    graphiql_prefix: str | None = None,
    graphiql_title: str = "GraphiQL - NexusX",
    default_query: str = PAGINATED_QUERY,
) -> APIRouter:
    """Create one standard GraphQL transport for a NexusX compose schema."""

    graphql_router = APIRouter(prefix=prefix, tags=["GraphQL"])

    @graphql_router.post("")
    async def graphql_endpoint(payload: GraphQLRequest) -> JSONResponse:
        settings = get_settings()
        try:
            validate_graphql_query_budget(
                payload.query,
                maximum_characters=settings.graphql_max_query_characters,
                maximum_tokens=settings.graphql_max_tokens,
                maximum_depth=settings.graphql_max_depth,
                maximum_complexity=settings.graphql_max_complexity,
            )
        except QueryBudgetExceeded as error:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=graphql_error_result(error),
            )
        if payload.variables:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "data": None,
                    "errors": [
                        {
                            "message": (
                                "NexusX compose GraphQL does not support variables; "
                                "inline arguments in the query string."
                            )
                        }
                    ],
                },
            )
        if is_introspection_query(payload.query):
            result = compose_introspect(compose_schema, payload.query)
        else:
            try:
                result = await execute_compose_query(
                    app=app_config,
                    schema=compose_schema,
                    query=payload.query,
                    context={},
                )
            except QueryStatementTimeout as error:
                return JSONResponse(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    content=graphql_error_result(error),
                )
        return JSONResponse(content=jsonable_encoder(normalize_graphql_query_errors(result)))

    @graphql_router.get("", response_class=HTMLResponse, include_in_schema=False)
    async def graphiql(request: Request) -> HTMLResponse:
        if get_settings().environment == "production":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        root_path = str(request.scope.get("root_path", "")).rstrip("/")
        public_prefix = request.headers.get("x-nexusx-graphiql-prefix")
        if public_prefix is None:
            public_prefix = prefix if graphiql_prefix is None else graphiql_prefix
        graphql_url = f"{root_path}{public_prefix}" or public_prefix
        html = GRAPHIQL_HTML.replace(
            "const fetcher = createGraphiQLFetcher({ url: '{graphql_url}' });",
            "const fetcher = createGraphiQLFetcher({ url: "
            f"'{graphql_url}'"
            " });\n"
            f"    const defaultQuery = {json.dumps(default_query)};",
        )
        html = html.replace("{graphql_url}", graphql_url)
        html = html.replace("<title>GraphiQL - nexusx</title>", f"<title>{graphiql_title}</title>")
        html = html.replace(
            "fetcher: fetcher,",
            "fetcher: fetcher,\n        defaultQuery: defaultQuery,",
        )
        return HTMLResponse(html)

    @graphql_router.get("/schema", response_class=PlainTextResponse)
    async def graphql_schema() -> PlainTextResponse:
        return PlainTextResponse(compose_schema.render_sdl())

    return graphql_router


router = create_graphql_router(
    config,
    schema,
    graphiql_title=f"Paginated GraphQL - {get_settings().brand_name}",
    default_query=PAGINATED_QUERY,
)

__all__ = [
    "DIRECT_PLAYGROUND_QUERY",
    "PAGINATED_QUERY",
    "GraphQLRequest",
    "create_graphql_router",
    "router",
]
