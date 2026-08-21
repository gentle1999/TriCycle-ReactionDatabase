"""OIDC login, browser sessions, and current-account routes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import quote, urljoin, urlsplit
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from tricycle_reaction_db.api.authentication import get_authenticated_principal
from tricycle_reaction_db.application.dtos import (
    AuditEventView,
    AuthConfigView,
    CurrentUserView,
    McpAccessTokenCreate,
    McpAccessTokenCreateResult,
    McpAccessTokenView,
    ProjectInvitationView,
    UserProfileUpdate,
)
from tricycle_reaction_db.application.dtos import (
    SessionView as SessionViewDTO,
)
from tricycle_reaction_db.application.services.audit import AuditService
from tricycle_reaction_db.application.services.authentication import (
    AuthenticatedPrincipal,
    AuthenticationError,
    AuthenticationService,
    McpAccessTokenInfo,
)
from tricycle_reaction_db.application.services.authorization import (
    AuthorizationService,
    ProjectAccessDeniedError,
)
from tricycle_reaction_db.application.services.invitations import (
    InvitationConflictError,
    InvitationNotFoundError,
    InvitationService,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.core.observability import OIDC_CALLBACKS

router = APIRouter(prefix="/api/auth", tags=["authentication"])
Principal = Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)]


def _safe_return_to(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/reactions"
    return value


def _frontend_url(path: str, request: Request | None = None) -> str:
    settings = get_settings()
    frontend_origin = settings.oidc_frontend_url.rstrip("/")
    configured_host = urlsplit(frontend_origin).hostname
    if (
        request is not None
        and settings.environment != "production"
        and (
            settings.auth_mode == "development"
            or configured_host in {"localhost", "127.0.0.1", "::1"}
        )
    ):
        forwarded_host = request.headers.get("x-forwarded-host")
        host = (forwarded_host or request.headers.get("host") or "").split(",", 1)[0].strip()
        forwarded_proto = request.headers.get("x-forwarded-proto")
        scheme = (forwarded_proto or request.url.scheme).split(",", 1)[0].strip().lower()
        if host and scheme in {"http", "https"}:
            frontend_origin = f"{scheme}://{host}"
    return urljoin(frontend_origin + "/", path.lstrip("/"))


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _callback_error_response(message: str, request: Request | None = None) -> RedirectResponse:
    OIDC_CALLBACKS.labels(outcome="failed").inc()
    response = RedirectResponse(
        _frontend_url(f"/login?error={quote(message[:160], safe='')}", request),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    response.delete_cookie("tricycle_oidc_state", path="/api/auth")
    return response


@router.get("/config", response_model=AuthConfigView)
async def auth_config() -> AuthConfigView:
    return AuthConfigView(
        oidc_enabled=get_settings().auth_mode == "oidc",
        login_path="/api/auth/login",
    )


@router.get("/login")
async def login(request: Request, return_to: str = "/reactions") -> RedirectResponse:
    settings = get_settings()
    destination = _safe_return_to(return_to)
    if settings.auth_mode == "development":
        return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    state_cookie = jwt.encode(
        {
            "state": state,
            "nonce": nonce,
            "code_verifier": code_verifier,
            "return_to": destination,
            "exp": datetime.now(UTC) + timedelta(minutes=10),
        },
        settings.session_secret,
        algorithm="HS256",
    )
    try:
        authorization_url = await AuthenticationService.oidc_authorization_url(
            state=state,
            nonce=nonce,
            code_challenge=_pkce_challenge(code_verifier),
            redirect_uri=settings.oidc_redirect_uri or "",
        )
    except AuthenticationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error
    response = RedirectResponse(authorization_url, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        "tricycle_oidc_state",
        state_cookie,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        max_age=600,
        path="/api/auth",
    )
    return response


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    settings = get_settings()
    if error:
        return _callback_error_response(error, request)
    state_cookie = request.cookies.get("tricycle_oidc_state")
    try:
        if not code or not state or not state_cookie:
            raise AuthenticationError("OIDC callback is missing code or state")
        state_payload = jwt.decode(state_cookie, settings.session_secret, algorithms=["HS256"])
        if state_payload.get("state") != state:
            raise AuthenticationError("OIDC callback state mismatch")
        code_verifier = state_payload.get("code_verifier")
        if not isinstance(code_verifier, str) or not code_verifier:
            raise AuthenticationError("OIDC callback is missing PKCE verifier")
        claims, id_token = await AuthenticationService.exchange_oidc_code(
            code=code,
            code_verifier=code_verifier,
            redirect_uri=settings.oidc_redirect_uri or "",
        )
        if claims.get("nonce") != state_payload.get("nonce"):
            raise AuthenticationError("OIDC callback nonce mismatch")
        principal = await AuthenticationService.principal_from_oidc_claims(claims)
        raw_token, auth_session = await AuthenticationService.create_session(
            principal,
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )
        await AuditService.record(
            action="auth.login",
            entity_type="auth_session",
            entity_id=auth_session.id,
            actor_user_id=principal.user_id,
            metadata={"issuer": principal.issuer, "subject": principal.subject},
        )
    except (AuthenticationError, jwt.PyJWTError) as callback_error:
        return _callback_error_response(str(callback_error), request)
    OIDC_CALLBACKS.labels(outcome="succeeded").inc()
    response = RedirectResponse(
        _frontend_url(str(state_payload.get("return_to") or "/reactions"), request),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    response.set_cookie(
        key=settings.session_cookie_name,
        value=raw_token,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
        max_age=settings.session_ttl_seconds,
    )
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=AuthenticationService.csrf_token(raw_token),
        httponly=False,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path="/",
        max_age=settings.session_ttl_seconds,
    )
    if id_token is not None:
        response.set_cookie(
            key="tricycle_oidc_id_token",
            value=id_token,
            httponly=True,
            secure=settings.session_cookie_secure,
            samesite="lax",
            path="/api/auth/logout",
            max_age=settings.session_ttl_seconds,
        )
    response.delete_cookie("tricycle_oidc_state", path="/api/auth")
    return response


@router.get("/me", response_model=CurrentUserView)
async def current_user(principal: Principal) -> CurrentUserView:
    try:
        return await AuthorizationService.current_user_view(principal)
    except ProjectAccessDeniedError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error


def _mcp_access_token_view(token: McpAccessTokenInfo) -> McpAccessTokenView:
    return McpAccessTokenView(
        id=token.id,
        name=token.name,
        created_at=token.created_at,
        expires_at=token.expires_at,
        last_used_at=token.last_used_at,
    )


@router.get("/mcp-tokens", response_model=list[McpAccessTokenView])
async def list_mcp_tokens(principal: Principal) -> list[McpAccessTokenView]:
    tokens = await AuthenticationService.list_mcp_access_tokens(principal.user_id)
    return [_mcp_access_token_view(token) for token in tokens]


@router.post(
    "/mcp-tokens",
    response_model=McpAccessTokenCreateResult,
    status_code=status.HTTP_201_CREATED,
)
async def create_mcp_token(
    payload: McpAccessTokenCreate,
    principal: Principal,
) -> McpAccessTokenCreateResult:
    raw_token, token = await AuthenticationService.create_mcp_access_token(
        principal,
        name=payload.name,
    )
    await AuditService.record(
        action="auth.mcp_token.created",
        entity_type="mcp_access_token",
        entity_id=token.id,
        actor_user_id=principal.user_id,
        metadata={"name": token.name},
    )
    return McpAccessTokenCreateResult(
        token=_mcp_access_token_view(token),
        access_token=raw_token,
    )


@router.delete("/mcp-tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_mcp_token(token_id: UUID, principal: Principal) -> None:
    revoked = await AuthenticationService.revoke_mcp_access_token(principal.user_id, token_id)
    if not revoked:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP token not found")
    await AuditService.record(
        action="auth.mcp_token.revoked",
        entity_type="mcp_access_token",
        entity_id=token_id,
        actor_user_id=principal.user_id,
    )


@router.patch("/me", response_model=CurrentUserView)
async def update_current_user(payload: UserProfileUpdate, principal: Principal) -> CurrentUserView:
    from tricycle_reaction_db.db.models import UserAccount
    from tricycle_reaction_db.db.session import session_factory

    async with session_factory() as session:
        user = await session.get(UserAccount, principal.user_id, with_for_update=True)
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
        user.display_name = payload.display_name.strip()
        await session.commit()
    await AuditService.record(
        action="account.profile_updated",
        entity_type="user_account",
        entity_id=principal.user_id,
        actor_user_id=principal.user_id,
        metadata={"display_name": payload.display_name.strip()},
    )
    return await AuthorizationService.current_user_view(principal)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response) -> None:
    raw_token = request.cookies.get(get_settings().session_cookie_name)
    principal = None
    if raw_token:
        with suppress(AuthenticationError):
            principal = await AuthenticationService.authenticate_session(raw_token)
    await AuthenticationService.revoke_session(raw_token)
    if principal is not None:
        await AuditService.record(
            action="auth.logout",
            entity_type="auth_session",
            actor_user_id=principal.user_id,
        )
    response.delete_cookie(get_settings().session_cookie_name, path="/")
    response.delete_cookie(get_settings().csrf_cookie_name, path="/")
    response.delete_cookie("tricycle_oidc_id_token", path="/api/auth/logout")


@router.get("/logout")
async def logout_redirect(
    request: Request,
    return_to: str = "/",
    csrf_token: str | None = None,
) -> RedirectResponse:
    settings = get_settings()
    raw_session_token = request.cookies.get(settings.session_cookie_name)
    if raw_session_token is not None:
        cookie_token = request.cookies.get(settings.csrf_cookie_name)
        expected_token = AuthenticationService.csrf_token(raw_session_token)
        if (
            csrf_token is None
            or cookie_token is None
            or not hmac.compare_digest(csrf_token, cookie_token)
            or not hmac.compare_digest(csrf_token, expected_token)
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF token is required for session-authenticated logout",
            )
    await AuthenticationService.revoke_session(raw_session_token)
    frontend_target = _frontend_url(_safe_return_to(return_to), request)
    provider_target: str | None = None
    if settings.auth_mode == "oidc":
        with suppress(AuthenticationError):
            provider_target = await AuthenticationService.oidc_logout_url(
                post_logout_redirect_uri=frontend_target,
                id_token_hint=request.cookies.get("tricycle_oidc_id_token"),
            )
    response = RedirectResponse(
        provider_target or frontend_target,
        status_code=status.HTTP_303_SEE_OTHER,
    )
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")
    response.delete_cookie("tricycle_oidc_id_token", path="/api/auth/logout")
    return response


@router.get("/sessions", response_model=list[SessionViewDTO])
async def list_sessions(
    request: Request,
    principal: Principal,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[SessionViewDTO]:
    sessions = await AuthenticationService.list_sessions(
        principal.user_id,
        current_token=request.cookies.get(get_settings().session_cookie_name),
        limit=limit,
        offset=offset,
    )
    return [SessionViewDTO.model_validate(item, from_attributes=True) for item in sessions]


@router.post("/sessions/revoke-all", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_all_sessions(request: Request, principal: Principal) -> None:
    await AuthenticationService.revoke_all_sessions(
        principal.user_id,
        except_token=request.cookies.get(get_settings().session_cookie_name),
    )
    await AuditService.record(
        action="auth.sessions.revoked_all",
        entity_type="auth_session",
        actor_user_id=principal.user_id,
    )


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(session_id: UUID, principal: Principal) -> None:
    from tricycle_reaction_db.db.models import AuthSession
    from tricycle_reaction_db.db.session import session_factory

    async with session_factory() as session:
        auth_session = await session.get(AuthSession, session_id, with_for_update=True)
        if auth_session is None or auth_session.user_id != principal.user_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found")
        auth_session.revoked_at = datetime.now(UTC)
        await session.commit()
    await AuditService.record(
        action="auth.session.revoked",
        entity_type="auth_session",
        entity_id=session_id,
        actor_user_id=principal.user_id,
    )


@router.post("/invitations/{token}/accept", response_model=ProjectInvitationView)
async def accept_invitation(token: str, principal: Principal) -> ProjectInvitationView:
    try:
        return await InvitationService.accept(token, principal)
    except InvitationNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except InvitationConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.get("/audit", response_model=list[AuditEventView])
async def list_account_audit(
    principal: Principal,
    limit: int = 50,
    offset: int = 0,
) -> list[AuditEventView]:
    return await AuditService.list_events(principal, limit=limit, offset=offset)


__all__ = ["router"]
