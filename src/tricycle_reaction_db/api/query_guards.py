"""Transport guards for query budgets, rate limits, and stable errors."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from tricycle_reaction_db.application.query_cost import (
    QueryBudgetExceeded,
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
    if path.startswith("/api/upload-batches/") and (
        "/files/" in path or path.endswith("/files")
    ):
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

    @application.exception_handler(QueryStatementTimeout)
    async def query_timeout_error(
        _request: Request,
        error: QueryStatementTimeout,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": query_error_payload(error)},
        )


__all__ = ["QueryRateLimitMiddleware", "install_query_guards"]
