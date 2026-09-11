"""Transport guards for query budgets, rate limits, and stable errors."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from graphql import parse
from graphql.language import ast
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from tricycle_reaction_db.application.query_cost import (
    QueryBudgetExceeded,
    QueryProjectScopeRequired,
    QueryRateLimitExceeded,
    QueryStatementTimeout,
    query_error_payload,
)
from tricycle_reaction_db.application.rate_limits import (
    AsyncRateLimiter,
    RateLimitBackendUnavailable,
    create_rate_limiter,
)
from tricycle_reaction_db.application.services.authentication import AuthenticatedPrincipal
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.core.observability import RATE_LIMIT_DECISIONS, UPLOAD_OPERATIONS

logger = logging.getLogger(__name__)

_PROJECT_SCOPE_EXEMPT_SERVICES = frozenset(
    {
        "SystemService",
        "StorageGarbageCollectionQueryService",
    }
)

_EXEMPT_PATHS = {
    "/docs",
    "/docs/oauth2-redirect",
    "/graphql/schema",
    "/openapi.json",
    "/redoc",
    "/internal/metrics",
}

_MOLECULE_QUERY_PATHS = {
    "/api/formulas/search",
    "/api/topologies",
    "/api/topologies/search",
    "/api/chemistry/representations",
    "/api/chemistry/reactions",
    "/api/chemistry/reactions/validate",
}
_MOLECULE_QUERY_PREFIXES = (
    "/api/geometry_query_service/",
    "/api/molecular_formula_detail_query_service/",
    "/api/molecular_formula_query_service/",
    "/api/molecular_topology_derivation_query_service/",
    "/api/molecular_topology_detail_query_service/",
    "/api/molecular_topology_query_service/",
)


def _is_molecule_query(path: str) -> bool:
    return path in _MOLECULE_QUERY_PATHS or path.startswith(_MOLECULE_QUERY_PREFIXES)


def _is_upload_request(method: str, path: str) -> bool:
    if method != "POST":
        return False
    if path in {"/api/artifacts", "/api/artifacts/batch", "/api/artifacts/validate"}:
        return True
    if path.startswith("/api/upload-batches/") and ("/files/" in path or path.endswith("/files")):
        return True
    relative = path.removeprefix("/api/artifacts/")
    return relative != path and relative.endswith("/reparse") and relative.count("/") == 1


def _is_read_request(method: str, path: str) -> bool:
    if method == "GET":
        return True
    return method == "POST" and (
        path in {"/graphql", "/graphql-playground"}
        or (path.startswith("/api/") and "_query_service/" in path)
    )


def project_scoped_use_case_methods(app_config: Any) -> dict[str, frozenset[str]]:
    """Return use-case fields that must receive an explicit project scope.

    The generated NexusX routes and GraphQL schema are transport adapters over
    the same service classes.  Keeping this check derived from the service
    signatures makes adding a new project-owned query fail at application
    startup if it forgets to expose ``project_id``.
    """

    scoped: dict[str, frozenset[str]] = {}
    for service_cls in app_config.services:
        service_name = service_cls.__name__
        methods = getattr(service_cls, "__use_case_methods__", {})
        if service_name in _PROJECT_SCOPE_EXEMPT_SERVICES:
            continue
        scoped_methods: set[str] = set()
        for method_name, metadata in methods.items():
            method_kind = metadata.get("kind", "query") if isinstance(metadata, dict) else "query"
            if method_kind not in {"query", "mutation"}:
                continue
            method = getattr(service_cls, method_name)
            parameters = inspect.signature(method).parameters
            project_parameter = parameters.get("project_id")
            if project_parameter is None:
                raise RuntimeError(
                    f"{service_name}.{method_name} must expose project_id for a project-scoped "
                    "NexusX operation"
                )
            if project_parameter.default is not inspect.Parameter.empty:
                raise RuntimeError(
                    f"{service_name}.{method_name}.project_id must be required for a "
                    "project-scoped NexusX operation"
                )
            scoped_methods.add(method_name)
        if scoped_methods:
            scoped[service_name] = frozenset(scoped_methods)
    return scoped


async def require_project_query_scope(request: Request) -> None:
    """Reject generated REST operations whose JSON body omits ``project_id``."""

    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="project_id is required for project-owned queries",
        ) from error
    if not isinstance(payload, dict):
        missing_project_scope = True
    else:
        project_id = payload.get("project_id")
        missing_project_scope = project_id is None or project_id == ""
    if missing_project_scope:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="project_id is required for project-owned queries",
        )


def _selected_graphql_fields(
    selection_set: ast.SelectionSetNode | None,
    fragments: dict[str, ast.FragmentDefinitionNode],
    active_fragments: frozenset[str] = frozenset(),
) -> list[ast.FieldNode]:
    if selection_set is None:
        return []
    fields: list[ast.FieldNode] = []
    for selection in selection_set.selections:
        if isinstance(selection, ast.FieldNode):
            fields.append(selection)
        elif isinstance(selection, ast.InlineFragmentNode):
            fields.extend(
                _selected_graphql_fields(
                    selection.selection_set,
                    fragments,
                    active_fragments,
                )
            )
        elif isinstance(selection, ast.FragmentSpreadNode):
            fragment_name = selection.name.value
            if fragment_name in active_fragments:
                continue
            fragment = fragments.get(fragment_name)
            if fragment is not None:
                fields.extend(
                    _selected_graphql_fields(
                        fragment.selection_set,
                        fragments,
                        active_fragments | {fragment_name},
                    )
                )
    return fields


def validate_graphql_project_scope(
    query: str,
    scoped_methods: dict[str, frozenset[str]],
) -> None:
    """Require a literal, non-null project_id on every project-owned root field."""

    try:
        document = parse(query)
    except Exception:
        # The normal GraphQL executor will return the syntax error.  This
        # guard must not turn malformed documents into an HTTP 500.
        return
    fragments = {
        definition.name.value: definition
        for definition in document.definitions
        if isinstance(definition, ast.FragmentDefinitionNode)
    }
    for definition in document.definitions:
        if not isinstance(definition, ast.OperationDefinitionNode):
            continue
        for service_field in _selected_graphql_fields(definition.selection_set, fragments):
            service_name = service_field.name.value
            method_names = scoped_methods.get(service_name)
            if method_names is None:
                continue
            for method_field in _selected_graphql_fields(service_field.selection_set, fragments):
                method_name = method_field.name.value
                if method_name not in method_names:
                    continue
                project_argument = next(
                    (
                        argument
                        for argument in method_field.arguments
                        if argument.name.value == "project_id"
                    ),
                    None,
                )
                if project_argument is None or isinstance(
                    project_argument.value,
                    (ast.NullValueNode, ast.VariableNode),
                ):
                    raise QueryProjectScopeRequired()


class QueryRateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        settings = get_settings()
        self._query_limiter = create_rate_limiter(
            policy="management",
            maximum_requests=settings.query_rate_limit_requests,
            window_seconds=settings.query_rate_limit_window_seconds,
            backend=getattr(settings, "rate_limit_backend", "memory"),
            redis_url=getattr(settings, "rate_limit_redis_url", None),
            key_prefix=getattr(settings, "rate_limit_key_prefix", "reaction-database"),
        )
        self._read_limiter = create_rate_limiter(
            policy="read",
            maximum_requests=settings.read_rate_limit_requests,
            window_seconds=settings.query_rate_limit_window_seconds,
            backend=getattr(settings, "rate_limit_backend", "memory"),
            redis_url=getattr(settings, "rate_limit_redis_url", None),
            key_prefix=getattr(settings, "rate_limit_key_prefix", "reaction-database"),
        )
        self._upload_limiter = create_rate_limiter(
            policy="upload",
            maximum_requests=settings.upload_rate_limit_requests,
            window_seconds=settings.query_rate_limit_window_seconds,
            backend=getattr(settings, "rate_limit_backend", "memory"),
            redis_url=getattr(settings, "rate_limit_redis_url", None),
            key_prefix=getattr(settings, "rate_limit_key_prefix", "reaction-database"),
        )
        self._upload_slots = asyncio.Semaphore(settings.upload_max_concurrency)
        self._molecule_query_limiter = create_rate_limiter(
            policy="molecule-query",
            maximum_requests=settings.molecule_query_rate_limit_requests,
            window_seconds=settings.query_rate_limit_window_seconds,
            backend=getattr(settings, "rate_limit_backend", "memory"),
            redis_url=getattr(settings, "rate_limit_redis_url", None),
            key_prefix=getattr(settings, "rate_limit_key_prefix", "reaction-database"),
        )
        self._depiction_limiter = create_rate_limiter(
            policy="depiction",
            maximum_requests=settings.depiction_rate_limit_requests,
            window_seconds=settings.query_rate_limit_window_seconds,
            backend=getattr(settings, "rate_limit_backend", "memory"),
            redis_url=getattr(settings, "rate_limit_redis_url", None),
            key_prefix=getattr(settings, "rate_limit_key_prefix", "reaction-database"),
        )

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if (
            request.method == "OPTIONS"
            or request.url.path in _EXEMPT_PATHS
            or request.url.path.startswith("/health/")
        ):
            return await call_next(request)
        principal = getattr(request.state, "principal", None)
        if isinstance(principal, AuthenticatedPrincipal):
            key = f"user:{principal.user_id}"
        else:
            client = request.client.host if request.client is not None else "unknown"
            key = f"client:{client}"
        method = request.method
        path = request.url.path
        is_depiction = method == "GET" and path.startswith("/api/depictions/")
        limiter: AsyncRateLimiter
        if is_depiction:
            policy = "depiction"
            limiter = self._depiction_limiter
        elif _is_molecule_query(path):
            policy = "molecule-query"
            limiter = self._molecule_query_limiter
        elif _is_upload_request(method, path):
            policy = "upload"
            limiter = self._upload_limiter
        elif _is_read_request(method, path):
            policy = "read"
            limiter = self._read_limiter
        else:
            policy = "management"
            limiter = self._query_limiter
        try:
            decision = await limiter.check(key)
        except RateLimitBackendUnavailable as backend_error:
            RATE_LIMIT_DECISIONS.labels(policy=policy, outcome="backend_error").inc()
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "detail": {
                        "code": backend_error.code,
                        "message": backend_error.message,
                    }
                },
                headers={"Retry-After": "1", "Cache-Control": "no-store"},
            )
        if not decision.allowed:
            RATE_LIMIT_DECISIONS.labels(policy=policy, outcome="rejected").inc()
            rate_limit_error = QueryRateLimitExceeded(
                retry_after_seconds=decision.retry_after_seconds
            )
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": query_error_payload(rate_limit_error)},
                headers={
                    "Retry-After": str(decision.retry_after_seconds),
                    "X-RateLimit-Limit": str(limiter.maximum_requests),
                    "X-RateLimit-Policy": policy,
                    "X-RateLimit-Remaining": "0",
                },
            )
        RATE_LIMIT_DECISIONS.labels(policy=policy, outcome="allowed").inc()
        try:
            if policy == "upload":
                async with self._upload_slots:
                    response = await call_next(request)
            else:
                response = await call_next(request)
        except Exception:
            if policy == "upload":
                UPLOAD_OPERATIONS.labels(outcome="failed").inc()
            raise
        if policy == "upload":
            outcome = "succeeded" if response.status_code < 400 else "failed"
            UPLOAD_OPERATIONS.labels(outcome=outcome).inc()
        response.headers["X-RateLimit-Limit"] = str(limiter.maximum_requests)
        response.headers["X-RateLimit-Policy"] = policy
        response.headers["X-RateLimit-Remaining"] = str(decision.remaining)
        return response


def install_query_guards(application: FastAPI) -> None:
    application.add_middleware(QueryRateLimitMiddleware)

    @application.exception_handler(QueryBudgetExceeded)
    async def query_budget_error(
        _request: Request,
        error: QueryBudgetExceeded,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            content={"detail": query_error_payload(error)},
        )

    @application.exception_handler(QueryProjectScopeRequired)
    async def query_project_scope_error(
        _request: Request,
        error: QueryProjectScopeRequired,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": query_error_payload(error)},
        )

    @application.exception_handler(QueryStatementTimeout)
    async def query_timeout_error(
        request: Request,
        error: QueryStatementTimeout,
    ) -> JSONResponse:
        # The public response intentionally contains no SQL or bound values.
        # The route is safe, low-cardinality context for finding which
        # interactive query needs an index/plan fix in server logs.
        logger.warning(
            "database statement timeout method=%s path=%s",
            request.method,
            request.url.path,
            extra={
                "query_error_code": error.code,
                "query_method": request.method,
                "query_path": request.url.path,
            },
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": query_error_payload(error)},
        )


__all__ = [
    "QueryRateLimitMiddleware",
    "install_query_guards",
    "project_scoped_use_case_methods",
    "require_project_query_scope",
    "validate_graphql_project_scope",
]
