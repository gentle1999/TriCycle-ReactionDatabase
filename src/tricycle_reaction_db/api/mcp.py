"""NexusX four-layer progressive-disclosure MCP transport."""

from collections.abc import Awaitable, Callable
from typing import Any, cast

from fastmcp.server.middleware import Middleware as FastMCPMiddleware
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools import ToolResult
from nexusx import create_use_case_graphql_mcp_server  # type: ignore[import-untyped]
from starlette.middleware import Middleware as ASGIMiddleware
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from tricycle_reaction_db.api.nexusx import config
from tricycle_reaction_db.application.query_cost import (
    QueryBudgetExceeded,
    QueryRateLimitExceeded,
    graphql_error_result,
    normalize_graphql_query_errors,
    validate_graphql_query_budget,
)
from tricycle_reaction_db.application.rate_limits import (
    RateLimitBackendUnavailable,
    create_rate_limiter,
)
from tricycle_reaction_db.application.services.authentication import (
    AuthenticationError,
    AuthenticationService,
    current_principal,
    request_context_active,
    reset_current_principal,
    reset_request_context_active,
    set_current_principal,
    set_request_context_active,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.core.observability import MCP_ACTIVE_CONNECTIONS, RATE_LIMIT_DECISIONS

ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class MCPAuthenticationMiddleware:
    """Authenticate MCP HTTP requests and populate the shared principal context."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def _authorization_header(scope: Scope) -> str | None:
        headers = cast(list[tuple[bytes, bytes]], scope.get("headers", []))
        for name, value in headers:
            if name.lower() == b"authorization":
                return value.decode("latin-1")
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if current_principal() is not None and request_context_active():
            await self.app(scope, receive, send)
            return
        try:
            principal = await AuthenticationService.authenticate(self._authorization_header(scope))
        except AuthenticationError as error:
            response = JSONResponse(
                status_code=401,
                content={"detail": str(error)},
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        scope.setdefault("state", {})["principal"] = principal
        request_context_token = set_request_context_active()
        principal_token = set_current_principal(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_principal(principal_token)
            reset_request_context_active(request_context_token)


class MCPBrowserInfoMiddleware:
    """Explain the Streamable HTTP endpoint when opened as a browser URL.

    Streamable HTTP uses ``POST`` for JSON-RPC messages.  A browser navigation
    sends ``GET`` with an HTML-oriented ``Accept`` header, which FastMCP
    correctly rejects in stateless mode.  Returning a small JSON contract here
    makes the frontend entry point useful without changing protocol requests.
    """

    def __init__(self, app: ASGIApp, *, endpoint: str = "/mcp/") -> None:
        self.app = app
        self.endpoint = endpoint

    @staticmethod
    def _accepts_event_stream(scope: Scope) -> bool:
        headers = cast(list[tuple[bytes, bytes]], scope.get("headers", []))
        return any(
            name.lower() == b"accept" and b"text/event-stream" in value.lower()
            for name, value in headers
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") == "GET"
            and not self._accepts_event_stream(scope)
        ):
            response = JSONResponse(
                content={
                    "service": "UseCase MCP",
                    "transport": "Streamable HTTP",
                    "endpoint": self.endpoint,
                    "method": "POST",
                    "message": "This endpoint is for MCP clients, not a browser UI.",
                    "request": {
                        "accept": "application/json, text/event-stream",
                        "content_type": "application/json",
                    },
                },
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class MCPMetricsMiddleware:
    """Track active Streamable HTTP requests without user or session labels."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        MCP_ACTIVE_CONNECTIONS.inc()
        try:
            await self.app(scope, receive, send)
        finally:
            MCP_ACTIVE_CONNECTIONS.dec()


class QueryGuardMiddleware(FastMCPMiddleware):
    """Apply the same GraphQL and request budgets to MCP compose execution."""

    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._limiter = create_rate_limiter(
            policy="mcp-read",
            maximum_requests=settings.read_rate_limit_requests,
            window_seconds=settings.query_rate_limit_window_seconds,
            backend=settings.rate_limit_backend,
            redis_url=settings.rate_limit_redis_url,
            key_prefix=settings.rate_limit_key_prefix,
        )

    @staticmethod
    def _result(payload: dict[str, Any]) -> ToolResult:
        return ToolResult(content=payload, structured_content=payload)

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: Any,
    ) -> ToolResult:
        principal = current_principal()
        try:
            key = (
                f"user:{principal.user_id}"
                if principal is not None
                else (
                    f"session:{context.fastmcp_context.session_id}"
                    if context.fastmcp_context is not None
                    else "in-process"
                )
            )
        except RuntimeError:
            key = "in-process"
        try:
            decision = await self._limiter.check(key)
        except RateLimitBackendUnavailable as backend_error:
            RATE_LIMIT_DECISIONS.labels(policy="mcp-read", outcome="backend_error").inc()
            return self._result(
                {
                    "data": None,
                    "errors": [
                        {
                            "message": backend_error.message,
                            "extensions": {"code": backend_error.code},
                        }
                    ],
                }
            )
        if not decision.allowed:
            RATE_LIMIT_DECISIONS.labels(policy="mcp-read", outcome="rejected").inc()
            rate_limit_error = QueryRateLimitExceeded(
                retry_after_seconds=decision.retry_after_seconds
            )
            return self._result(graphql_error_result(rate_limit_error))

        RATE_LIMIT_DECISIONS.labels(policy="mcp-read", outcome="allowed").inc()
        message = context.message
        if getattr(message, "name", None) == "compose_query":
            arguments = getattr(message, "arguments", None) or {}
            query = arguments.get("query")
            if isinstance(query, str):
                try:
                    validate_graphql_query_budget(
                        query,
                        maximum_characters=self._settings.graphql_max_query_characters,
                        maximum_tokens=self._settings.graphql_max_tokens,
                        maximum_depth=self._settings.graphql_max_depth,
                        maximum_complexity=self._settings.graphql_max_complexity,
                    )
                except QueryBudgetExceeded as error:
                    return self._result(graphql_error_result(error))

        result: ToolResult = await call_next(context)
        if (
            getattr(message, "name", None) == "compose_query"
            and isinstance(result, ToolResult)
            and isinstance(result.structured_content, dict)
        ):
            normalized = normalize_graphql_query_errors(result.structured_content)
            return self._result(normalized)
        return result


mcp_server = create_use_case_graphql_mcp_server(
    apps=[config],
    name=get_settings().mcp_server_name,
)
mcp_server.add_middleware(QueryGuardMiddleware())
mcp_http_app = mcp_server.http_app(
    path="/",
    middleware=[
        ASGIMiddleware(MCPAuthenticationMiddleware),
        ASGIMiddleware(MCPMetricsMiddleware),
        ASGIMiddleware(MCPBrowserInfoMiddleware),
    ],
    transport="streamable-http",
    stateless_http=True,
)
mcp_dedicated_app = mcp_server.http_app(
    path="/mcp",
    middleware=[
        ASGIMiddleware(MCPAuthenticationMiddleware),
        ASGIMiddleware(MCPMetricsMiddleware),
        ASGIMiddleware(MCPBrowserInfoMiddleware),
    ],
    transport="streamable-http",
    stateless_http=True,
)

__all__ = [
    "MCPAuthenticationMiddleware",
    "MCPBrowserInfoMiddleware",
    "MCPMetricsMiddleware",
    "QueryGuardMiddleware",
    "mcp_dedicated_app",
    "mcp_http_app",
    "mcp_server",
]
