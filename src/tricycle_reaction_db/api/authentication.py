"""HTTP authentication middleware and FastAPI principal dependency."""

from __future__ import annotations

import hmac
import re
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from tricycle_reaction_db.application.services.authentication import (
    AuthenticatedPrincipal,
    AuthenticationError,
    AuthenticationService,
    reset_current_principal,
    reset_request_context_active,
    set_current_principal,
    set_request_context_active,
)
from tricycle_reaction_db.core.config import get_settings

_PUBLIC_PATHS = {
    "/docs",
    "/docs/oauth2-redirect",
    "/openapi.json",
    "/redoc",
    "/internal/metrics",
    "/api/auth/config",
    "/api/auth/login",
    "/api/auth/callback",
}
_ANONYMOUS_ARTIFACT_PATH = re.compile(r"^/api/artifacts/[^/]+(?:/(?:preview|download))?$")
_ANONYMOUS_DEPICTION_PATH = re.compile(r"^/api/depictions/")
_bearer_scheme = HTTPBearer(auto_error=False)
BearerCredential = Annotated[
    HTTPAuthorizationCredentials | None,
    Security(_bearer_scheme),
]


def _is_public_request(request: Request) -> bool:
    return (
        request.method == "OPTIONS"
        or request.url.path in _PUBLIC_PATHS
        or (request.method == "GET" and request.url.path == "/api/auth/logout")
        or request.url.path.startswith("/health/")
    )


def _allows_anonymous_principal(request: Request) -> bool:
    if request.method != "GET":
        return False
    path = request.url.path
    return (
        path == "/api/artifacts"
        or _ANONYMOUS_ARTIFACT_PATH.fullmatch(path) is not None
        or _ANONYMOUS_DEPICTION_PATH.match(path) is not None
    )


def _is_mcp_request(request: Request) -> bool:
    return request.url.path == "/mcp" or request.url.path.startswith("/mcp/")


def _csrf_failure(request: Request, raw_session_token: str | None) -> str | None:
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    if raw_session_token is None or request.headers.get("authorization") is not None:
        return None
    settings = get_settings()
    cookie_token = request.cookies.get(settings.csrf_cookie_name)
    header_token = request.headers.get(settings.csrf_header_name)
    expected = AuthenticationService.csrf_token(raw_session_token)
    if cookie_token is None or header_token is None:
        return "CSRF token is required for session-authenticated state changes"
    if not (
        hmac.compare_digest(cookie_token, header_token)
        and hmac.compare_digest(cookie_token, expected)
    ):
        return "CSRF token is invalid"
    return None


class AuthenticationMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if _is_public_request(request):
            return await call_next(request)
        authorization = request.headers.get("authorization")
        session_token = request.cookies.get(get_settings().session_cookie_name)
        csrf_error = _csrf_failure(request, session_token)
        if csrf_error is not None:
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": csrf_error},
            )
        try:
            if (
                authorization is not None
                and authorization.startswith("Bearer mcp_")
                and not _is_mcp_request(request)
            ):
                raise AuthenticationError("MCP access token may only be used with /mcp/")
            if _allows_anonymous_principal(request):
                principal = (
                    await AuthenticationService.authenticate_optional(authorization)
                    if session_token is None
                    else await AuthenticationService.authenticate_optional(
                        authorization,
                        session_token,
                    )
                )
                if principal is None:
                    request_context_token = set_request_context_active()
                    try:
                        return await call_next(request)
                    finally:
                        reset_request_context_active(request_context_token)
            else:
                principal = (
                    await AuthenticationService.authenticate(authorization)
                    if session_token is None
                    else await AuthenticationService.authenticate(
                        authorization,
                        session_token=session_token,
                    )
                )
        except AuthenticationError as error:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": str(error)},
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.principal = principal
        request_context_token = set_request_context_active()
        context_token = set_current_principal(principal)
        try:
            return await call_next(request)
        finally:
            reset_current_principal(context_token)
            reset_request_context_active(request_context_token)


def get_authenticated_principal(
    request: Request,
    _: BearerCredential,
) -> AuthenticatedPrincipal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, AuthenticatedPrincipal):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


def get_optional_principal(
    request: Request,
    _: BearerCredential,
) -> AuthenticatedPrincipal | None:
    principal = getattr(request.state, "principal", None)
    return principal if isinstance(principal, AuthenticatedPrincipal) else None


__all__ = [
    "AuthenticationMiddleware",
    "get_authenticated_principal",
    "get_optional_principal",
]
