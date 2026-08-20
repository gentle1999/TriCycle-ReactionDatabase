"""Authentication against development identity or an external OIDC issuer."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import ssl
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx
import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWTError
from sqlalchemy import and_, delete, or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from tricycle_reaction_db.core.config import Settings, get_settings
from tricycle_reaction_db.core.tls import verified_tls_context
from tricycle_reaction_db.db.models import (
    AuthSession,
    ExternalIdentity,
    McpAccessToken,
    UserAccount,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import UserStatus
from tricycle_reaction_db.domain.identity import (
    DEVELOPMENT_IDENTITY_ISSUER,
    DEVELOPMENT_IDENTITY_SUBJECT,
)


class AuthenticationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    user_id: UUID
    display_name: str
    primary_email: str | None
    is_service_account: bool
    issuer: str
    subject: str


@dataclass(frozen=True, slots=True)
class SessionView:
    id: UUID
    created_at: datetime | None
    expires_at: datetime
    last_seen_at: datetime
    user_agent: str | None
    ip_address: str | None
    current: bool


@dataclass(frozen=True, slots=True)
class McpAccessTokenInfo:
    id: UUID
    name: str
    created_at: datetime | None
    expires_at: datetime
    last_used_at: datetime | None


_principal_context: ContextVar[AuthenticatedPrincipal | None] = ContextVar(
    "tricycle_authenticated_principal",
    default=None,
)
_request_context_active: ContextVar[bool] = ContextVar(
    "tricycle_request_authentication_context",
    default=False,
)
_oidc_metadata_cache: dict[str, tuple[datetime, dict[str, str]]] = {}
_oidc_metadata_locks: dict[str, asyncio.Lock] = {}
_OIDC_METADATA_TTL = timedelta(minutes=5)
_SESSION_LAST_SEEN_INTERVAL = timedelta(minutes=5)
_MCP_TOKEN_LAST_USED_INTERVAL = timedelta(minutes=5)


def current_principal() -> AuthenticatedPrincipal | None:
    return _principal_context.get()


def set_current_principal(
    principal: AuthenticatedPrincipal,
) -> Token[AuthenticatedPrincipal | None]:
    return _principal_context.set(principal)


def reset_current_principal(token: Token[AuthenticatedPrincipal | None]) -> None:
    _principal_context.reset(token)


def set_request_context_active() -> Token[bool]:
    """Mark the current transport request as having passed authentication middleware."""

    return _request_context_active.set(True)


def reset_request_context_active(token: Token[bool]) -> None:
    _request_context_active.reset(token)


def request_context_active() -> bool:
    return _request_context_active.get()


def oidc_tls_context(*, ca_bundle: str | None) -> ssl.SSLContext:
    return verified_tls_context(ca_bundle=ca_bundle)


@lru_cache
def _jwk_client(url: str, ca_bundle: str | None) -> PyJWKClient:
    return PyJWKClient(
        url,
        cache_jwk_set=True,
        cache_keys=True,
        ssl_context=oidc_tls_context(ca_bundle=ca_bundle),
    )


def _decode_oidc_token(token: str, settings: Settings) -> dict[str, Any]:
    if not settings.oidc_jwks_url or not settings.oidc_audience or not settings.oidc_issuer:
        raise AuthenticationError("OIDC configuration is incomplete")
    try:
        key = (
            _jwk_client(
                settings.oidc_jwks_url,
                settings.oidc_ca_bundle,
            )
            .get_signing_key_from_jwt(token)
            .key
        )
        claims = jwt.decode(
            token,
            key=key,
            algorithms=[settings.oidc_algorithm],
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            options={"require": ["exp", "iat", "iss", "sub", "aud"]},
        )
    except PyJWTError as error:
        raise AuthenticationError("invalid OIDC access token") from error
    return dict(claims)


def _validated_oidc_metadata(payload: object, settings: Settings) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise AuthenticationError("OIDC discovery is not an object")
    issuer = settings.oidc_issuer.rstrip("/") if settings.oidc_issuer else None
    if payload.get("issuer") != issuer:
        raise AuthenticationError("OIDC discovery issuer does not match configuration")
    required = ("authorization_endpoint", "token_endpoint")
    if not all(isinstance(payload.get(key), str) for key in required):
        raise AuthenticationError("OIDC discovery is incomplete")
    metadata = {key: str(value) for key, value in payload.items() if isinstance(value, str)}
    if settings.environment == "production":
        methods = payload.get("code_challenge_methods_supported")
        if not isinstance(methods, list) or "S256" not in methods:
            raise AuthenticationError("OIDC provider does not advertise PKCE S256")
        for key in (*required, "end_session_endpoint"):
            value = metadata.get(key)
            if value is not None and urlsplit(value).scheme != "https":
                raise AuthenticationError(f"OIDC discovery {key} must use HTTPS")
    return metadata


def _claim_text(claims: dict[str, Any], key: str) -> str | None:
    value = claims.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _oidc_claim_identity(claims: dict[str, Any]) -> tuple[str, str, str | None, str]:
    issuer = _claim_text(claims, "iss")
    subject = _claim_text(claims, "sub")
    if issuer is None or subject is None:
        raise AuthenticationError("OIDC token is missing issuer or subject")
    email = _claim_text(claims, "email")
    display_name = (
        _claim_text(claims, "name") or _claim_text(claims, "preferred_username") or email or subject
    )
    return issuer, subject, email, display_name


def _principal_for_identity(
    user: UserAccount,
    identity: ExternalIdentity,
) -> AuthenticatedPrincipal:
    if user.id is None:
        raise RuntimeError("persisted UserAccount is missing its UUID")
    if user.status is not UserStatus.ACTIVE:
        raise AuthenticationError("user account is suspended")
    return AuthenticatedPrincipal(
        user_id=user.id,
        display_name=user.display_name,
        primary_email=user.primary_email,
        is_service_account=user.is_service_account,
        issuer=identity.issuer,
        subject=identity.subject,
    )


async def _existing_oidc_principal(issuer: str, subject: str) -> AuthenticatedPrincipal | None:
    async with session_factory() as session:
        row = (
            await session.exec(
                select(UserAccount, ExternalIdentity)
                .join(ExternalIdentity, col(ExternalIdentity.user_id) == col(UserAccount.id))
                .where(
                    col(ExternalIdentity.issuer) == issuer,
                    col(ExternalIdentity.subject) == subject,
                )
            )
        ).first()
        if row is None:
            return None
        user, identity = row
        return _principal_for_identity(user, identity)


async def _provision_oidc_principal(
    claims: dict[str, Any],
    *,
    synchronize_existing: bool,
) -> tuple[AuthenticatedPrincipal, bool]:
    issuer, subject, email, display_name = _oidc_claim_identity(claims)
    authenticated_at = datetime.now(UTC)

    try:
        async with session_factory() as session:
            row = (
                await session.exec(
                    select(UserAccount, ExternalIdentity)
                    .join(ExternalIdentity, col(ExternalIdentity.user_id) == col(UserAccount.id))
                    .where(
                        col(ExternalIdentity.issuer) == issuer,
                        col(ExternalIdentity.subject) == subject,
                    )
                )
            ).first()
            created = row is None
            if row is None:
                settings = get_settings()
                bootstrap_user = (
                    await session.get(UserAccount, settings.oidc_bootstrap_user_id)
                    if settings.oidc_bootstrap_user_id is not None
                    else None
                )
                bootstrap_subject_matches = settings.oidc_bootstrap_subject == subject
                bootstrap_email_matches = (
                    settings.environment != "production"
                    and bootstrap_user is not None
                    and email is not None
                    and bootstrap_user.primary_email is not None
                    and bootstrap_user.primary_email.strip().lower() == email.strip().lower()
                )
                if bootstrap_subject_matches and settings.oidc_bootstrap_user_id is not None:
                    if bootstrap_user is None:
                        raise AuthenticationError("configured OIDC bootstrap user does not exist")
                    user = bootstrap_user
                elif bootstrap_email_matches:
                    assert bootstrap_user is not None
                    user = bootstrap_user
                else:
                    user = UserAccount(
                        display_name=display_name,
                        primary_email=email,
                        status=UserStatus.ACTIVE,
                        is_service_account=False,
                        last_authenticated_at=authenticated_at,
                    )
                    session.add(user)
                    await session.flush()
                    if user.id is None:
                        raise RuntimeError("database did not assign UserAccount.id")
                if user.id is None:
                    raise RuntimeError("persisted UserAccount is missing its UUID")
                identity = ExternalIdentity(
                    user_id=user.id,
                    issuer=issuer,
                    subject=subject,
                    email=email,
                    claims=claims,
                    last_authenticated_at=authenticated_at,
                )
                session.add(identity)
            else:
                user, identity = row
                if synchronize_existing:
                    identity.email = email
                    identity.claims = claims
                    identity.last_authenticated_at = authenticated_at
                    user.display_name = display_name
                    user.primary_email = email
                    user.last_authenticated_at = authenticated_at
            principal = _principal_for_identity(user, identity)
            if created or synchronize_existing:
                await session.commit()
            return principal, created
    except IntegrityError:
        conflict_principal = await _existing_oidc_principal(issuer, subject)
        if conflict_principal is None:
            raise AuthenticationError("external identity provisioning conflict") from None
        return conflict_principal, False


async def _principal_from_oidc_login_claims(
    claims: dict[str, Any],
) -> AuthenticatedPrincipal:
    principal, created = await _provision_oidc_principal(claims, synchronize_existing=True)
    if created:
        await _record_oidc_provision(principal, source="authorization_code")
    return principal


async def _principal_from_oidc_bearer_claims(
    claims: dict[str, Any],
) -> AuthenticatedPrincipal:
    issuer, subject, _, _ = _oidc_claim_identity(claims)
    principal = await _existing_oidc_principal(issuer, subject)
    if principal is not None:
        return principal
    principal, created = await _provision_oidc_principal(
        claims,
        synchronize_existing=False,
    )
    if created:
        await _record_oidc_provision(principal, source="bearer")
    return principal


async def _record_oidc_provision(
    principal: AuthenticatedPrincipal,
    *,
    source: str,
) -> None:
    from tricycle_reaction_db.application.services.audit import AuditService

    await AuditService.record(
        action="auth.provision",
        entity_type="user_account",
        entity_id=principal.user_id,
        actor_user_id=principal.user_id,
        metadata={
            "issuer": principal.issuer,
            "subject": principal.subject,
            "provisioned_by": source,
            "initial_status": UserStatus.ACTIVE.value,
            "private_access": "none_until_membership",
        },
    )


class AuthenticationService:
    @staticmethod
    async def oidc_metadata() -> dict[str, str]:
        settings = get_settings()
        if not settings.oidc_issuer:
            raise AuthenticationError("OIDC issuer is not configured")
        issuer = settings.oidc_issuer.rstrip("/")
        now = datetime.now(UTC)
        cached = _oidc_metadata_cache.get(issuer)
        if cached is not None and cached[0] > now:
            return dict(cached[1])
        lock = _oidc_metadata_locks.setdefault(issuer, asyncio.Lock())
        async with lock:
            now = datetime.now(UTC)
            cached = _oidc_metadata_cache.get(issuer)
            if cached is not None and cached[0] > now:
                return dict(cached[1])
            try:
                async with httpx.AsyncClient(
                    timeout=10,
                    verify=oidc_tls_context(ca_bundle=settings.oidc_ca_bundle),
                ) as client:
                    response = await client.get(f"{issuer}/.well-known/openid-configuration")
                    response.raise_for_status()
                    payload = response.json()
            except (httpx.HTTPError, ValueError) as error:
                raise AuthenticationError("OIDC discovery failed") from error
            metadata = _validated_oidc_metadata(payload, settings)
            _oidc_metadata_cache[issuer] = (now + _OIDC_METADATA_TTL, metadata)
            return dict(metadata)

    @staticmethod
    async def oidc_authorization_url(
        *,
        state: str,
        nonce: str,
        code_challenge: str,
        redirect_uri: str,
    ) -> str:
        settings = get_settings()
        metadata = await AuthenticationService.oidc_metadata()
        if not settings.oidc_client_id:
            raise AuthenticationError("OIDC client ID is not configured")
        return f"{metadata['authorization_endpoint']}?{
            urlencode(
                {
                    'client_id': settings.oidc_client_id,
                    'response_type': 'code',
                    'scope': 'openid profile email',
                    'redirect_uri': redirect_uri,
                    'state': state,
                    'nonce': nonce,
                    'code_challenge': code_challenge,
                    'code_challenge_method': 'S256',
                }
            )
        }"

    @staticmethod
    async def oidc_logout_url(
        *,
        post_logout_redirect_uri: str,
        id_token_hint: str | None = None,
    ) -> str | None:
        """Build the RP-initiated logout URL when the provider supports it."""

        settings = get_settings()
        if settings.auth_mode != "oidc":
            return None
        metadata = await AuthenticationService.oidc_metadata()
        endpoint = metadata.get("end_session_endpoint")
        if endpoint is None:
            return None
        parameters = {
            "client_id": settings.oidc_client_id,
            "post_logout_redirect_uri": post_logout_redirect_uri,
        }
        if id_token_hint:
            parameters["id_token_hint"] = id_token_hint
        return f"{endpoint}?{urlencode(parameters)}"

    @staticmethod
    async def exchange_oidc_code(
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> tuple[dict[str, Any], str | None]:
        settings = get_settings()
        metadata = await AuthenticationService.oidc_metadata()
        if not settings.oidc_client_id or not settings.oidc_client_secret:
            raise AuthenticationError("OIDC client credentials are not configured")
        try:
            async with httpx.AsyncClient(
                timeout=15,
                verify=oidc_tls_context(ca_bundle=settings.oidc_ca_bundle),
            ) as client:
                response = await client.post(
                    metadata["token_endpoint"],
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "client_id": settings.oidc_client_id,
                        "client_secret": settings.oidc_client_secret,
                        "redirect_uri": redirect_uri,
                        "code_verifier": code_verifier,
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise AuthenticationError("OIDC authorization code exchange failed") from error
        id_token = payload.get("id_token")
        raw_id_token = id_token if isinstance(id_token, str) else None
        candidates = [
            token for token in (id_token, payload.get("access_token")) if isinstance(token, str)
        ]
        if not candidates:
            raise AuthenticationError("OIDC token response has no signed token")
        last_error: AuthenticationError | None = None
        for token in candidates:
            try:
                return _decode_oidc_token(token, settings), raw_id_token
            except AuthenticationError as error:
                last_error = error
        raise last_error or AuthenticationError("OIDC token response has no valid signed token")

    @staticmethod
    async def principal_from_oidc_claims(claims: dict[str, Any]) -> AuthenticatedPrincipal:
        return await _principal_from_oidc_login_claims(claims)

    @staticmethod
    def _token_hash(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    @staticmethod
    def csrf_token(raw_session_token: str) -> str:
        """Bind a readable double-submit token to the opaque session cookie."""

        return hmac.new(
            get_settings().session_secret.encode("utf-8"),
            raw_session_token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    async def create_session(
        principal: AuthenticatedPrincipal,
        *,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> tuple[str, AuthSession]:
        now = datetime.now(UTC)
        raw_token = secrets.token_urlsafe(48)
        session = AuthSession(
            user_id=principal.user_id,
            token_hash=AuthenticationService._token_hash(raw_token),
            expires_at=now + timedelta(seconds=get_settings().session_ttl_seconds),
            last_seen_at=now,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        async with session_factory() as database:
            database.add(session)
            await database.commit()
            await database.refresh(session)
        return raw_token, session

    @staticmethod
    async def authenticate_session(raw_token: str) -> AuthenticatedPrincipal:
        token_hash = AuthenticationService._token_hash(raw_token)
        now = datetime.now(UTC)
        async with session_factory() as database:
            row = (
                await database.exec(
                    select(AuthSession, UserAccount, ExternalIdentity)
                    .join(UserAccount, col(UserAccount.id) == col(AuthSession.user_id))
                    .join(ExternalIdentity, col(ExternalIdentity.user_id) == col(UserAccount.id))
                    .where(
                        col(AuthSession.token_hash) == token_hash,
                        col(AuthSession.revoked_at).is_(None),
                        col(AuthSession.expires_at) > now,
                        col(UserAccount.status) == UserStatus.ACTIVE,
                    )
                    .order_by(
                        col(ExternalIdentity.last_authenticated_at).desc().nulls_last(),
                        col(ExternalIdentity.id).desc(),
                    )
                    .limit(1)
                )
            ).first()
            if row is None:
                raise AuthenticationError("session is invalid or expired")
            auth_session, user, identity = row
            if auth_session.last_seen_at <= now - _SESSION_LAST_SEEN_INTERVAL:
                await database.execute(
                    update(AuthSession)
                    .where(
                        col(AuthSession.id) == auth_session.id,
                        col(AuthSession.last_seen_at) <= now - _SESSION_LAST_SEEN_INTERVAL,
                        col(AuthSession.revoked_at).is_(None),
                        col(AuthSession.expires_at) > now,
                    )
                    .values(last_seen_at=now)
                )
                await database.commit()
            return _principal_for_identity(user, identity)

    @staticmethod
    async def revoke_session(raw_token: str | None) -> None:
        if not raw_token:
            return
        async with session_factory() as database:
            auth_session = (
                await database.exec(
                    select(AuthSession).where(
                        AuthSession.token_hash == AuthenticationService._token_hash(raw_token),
                        col(AuthSession.revoked_at).is_(None),
                    )
                )
            ).first()
            if auth_session is not None:
                auth_session.revoked_at = datetime.now(UTC)
                await database.commit()

    @staticmethod
    async def revoke_all_sessions(user_id: UUID, *, except_token: str | None = None) -> None:
        async with session_factory() as database:
            sessions = (
                await database.exec(
                    select(AuthSession).where(
                        AuthSession.user_id == user_id,
                        col(AuthSession.revoked_at).is_(None),
                    )
                )
            ).all()
            now = datetime.now(UTC)
            except_hash = AuthenticationService._token_hash(except_token) if except_token else None
            for auth_session in sessions:
                if except_hash is None or auth_session.token_hash != except_hash:
                    auth_session.revoked_at = now
            await database.commit()

    @staticmethod
    async def list_sessions(
        user_id: UUID,
        *,
        current_token: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionView]:
        limit = min(max(limit, 1), 200)
        offset = max(offset, 0)
        now = datetime.now(UTC)
        async with session_factory() as database:
            sessions = (
                await database.exec(
                    select(AuthSession)
                    .where(
                        AuthSession.user_id == user_id,
                        col(AuthSession.revoked_at).is_(None),
                        AuthSession.expires_at > now,
                    )
                    .order_by(col(AuthSession.last_seen_at).desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
        current_hash = AuthenticationService._token_hash(current_token) if current_token else None
        views: list[SessionView] = []
        for item in sessions:
            if item.id is None:
                raise RuntimeError("persisted auth session is missing its UUID")
            views.append(
                SessionView(
                    id=item.id,
                    created_at=item.created_at,
                    expires_at=item.expires_at,
                    last_seen_at=item.last_seen_at,
                    user_agent=item.user_agent,
                    ip_address=item.ip_address,
                    current=item.token_hash == current_hash,
                )
            )
        return views

    @staticmethod
    async def create_mcp_access_token(
        principal: AuthenticatedPrincipal,
        *,
        name: str,
    ) -> tuple[str, McpAccessTokenInfo]:
        now = datetime.now(UTC)
        raw_token = f"mcp_{secrets.token_urlsafe(32)}"
        token = McpAccessToken(
            user_id=principal.user_id,
            name=name.strip(),
            token_hash=AuthenticationService._token_hash(raw_token),
            expires_at=now + timedelta(seconds=get_settings().mcp_token_ttl_seconds),
        )
        async with session_factory() as database:
            database.add(token)
            await database.commit()
            await database.refresh(token)
        if token.id is None:
            raise RuntimeError("database did not assign MCP access token UUID")
        return raw_token, McpAccessTokenInfo(
            id=token.id,
            name=token.name,
            created_at=token.created_at,
            expires_at=token.expires_at,
            last_used_at=token.last_used_at,
        )

    @staticmethod
    async def list_mcp_access_tokens(user_id: UUID) -> list[McpAccessTokenInfo]:
        now = datetime.now(UTC)
        async with session_factory() as database:
            tokens = (
                await database.exec(
                    select(McpAccessToken)
                    .where(
                        McpAccessToken.user_id == user_id,
                        col(McpAccessToken.revoked_at).is_(None),
                        col(McpAccessToken.expires_at) > now,
                    )
                    .order_by(col(McpAccessToken.created_at).desc())
                )
            ).all()
        views: list[McpAccessTokenInfo] = []
        for token in tokens:
            if token.id is None:
                raise RuntimeError("persisted MCP access token is missing its UUID")
            views.append(
                McpAccessTokenInfo(
                    id=token.id,
                    name=token.name,
                    created_at=token.created_at,
                    expires_at=token.expires_at,
                    last_used_at=token.last_used_at,
                )
            )
        return views

    @staticmethod
    async def revoke_mcp_access_token(user_id: UUID, token_id: UUID) -> bool:
        async with session_factory() as database:
            token = (
                await database.exec(
                    select(McpAccessToken).where(
                        McpAccessToken.id == token_id,
                        McpAccessToken.user_id == user_id,
                        col(McpAccessToken.revoked_at).is_(None),
                    )
                )
            ).first()
            if token is None:
                return False
            token.revoked_at = datetime.now(UTC)
            await database.commit()
            return True

    @staticmethod
    async def authenticate_mcp_access_token(raw_token: str) -> AuthenticatedPrincipal:
        token_hash = AuthenticationService._token_hash(raw_token)
        now = datetime.now(UTC)
        async with session_factory() as database:
            row = (
                await database.exec(
                    select(McpAccessToken, UserAccount, ExternalIdentity)
                    .join(UserAccount, col(UserAccount.id) == col(McpAccessToken.user_id))
                    .join(ExternalIdentity, col(ExternalIdentity.user_id) == col(UserAccount.id))
                    .where(
                        col(McpAccessToken.token_hash) == token_hash,
                        col(McpAccessToken.revoked_at).is_(None),
                        col(McpAccessToken.expires_at) > now,
                        col(UserAccount.status) == UserStatus.ACTIVE,
                    )
                    .order_by(
                        col(ExternalIdentity.last_authenticated_at).desc().nulls_last(),
                        col(ExternalIdentity.id).desc(),
                    )
                    .limit(1)
                )
            ).first()
            if row is None:
                raise AuthenticationError("MCP access token is invalid or expired")
            access_token, user, identity = row
            if (
                access_token.last_used_at is None
                or access_token.last_used_at <= now - _MCP_TOKEN_LAST_USED_INTERVAL
            ):
                await database.execute(
                    update(McpAccessToken)
                    .where(
                        col(McpAccessToken.id) == access_token.id,
                        col(McpAccessToken.revoked_at).is_(None),
                        col(McpAccessToken.expires_at) > now,
                    )
                    .values(last_used_at=now)
                )
                await database.commit()
            return _principal_for_identity(user, identity)

    @staticmethod
    async def cleanup_sessions(
        *,
        revoked_retention: timedelta = timedelta(days=30),
        user_id: UUID | None = None,
    ) -> int:
        """Delete expired sessions and old revoked sessions for scheduled maintenance."""

        now = datetime.now(UTC)
        revoked_before = now - revoked_retention
        cleanup_predicate = or_(
            col(AuthSession.expires_at) <= now,
            col(AuthSession.revoked_at) <= revoked_before,
        )
        if user_id is not None:
            cleanup_predicate = and_(
                col(AuthSession.user_id) == user_id,
                cleanup_predicate,
            )
        async with session_factory() as database:
            result = await database.execute(delete(AuthSession).where(cleanup_predicate))
            await database.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    @staticmethod
    async def authenticate_optional(
        authorization: str | None,
        session_token: str | None = None,
    ) -> AuthenticatedPrincipal | None:
        settings = get_settings()
        if authorization is None and session_token is not None and settings.auth_mode == "oidc":
            return await AuthenticationService.authenticate_session(session_token)
        if authorization is None and settings.auth_mode == "oidc":
            return None
        if session_token is None:
            return await AuthenticationService.authenticate(authorization)
        return await AuthenticationService.authenticate(
            authorization,
            session_token=session_token,
        )

    @staticmethod
    async def authenticate(
        authorization: str | None,
        *,
        session_token: str | None = None,
    ) -> AuthenticatedPrincipal:
        settings = get_settings()
        raw_token: str | None = None
        if authorization is not None:
            if not authorization.startswith("Bearer "):
                raise AuthenticationError("Bearer access token required")
            raw_token = authorization.removeprefix("Bearer ").strip()
            if not raw_token:
                raise AuthenticationError("Bearer access token required")
            if raw_token.startswith("mcp_"):
                return await AuthenticationService.authenticate_mcp_access_token(raw_token)
        if settings.auth_mode == "development":
            return AuthenticatedPrincipal(
                user_id=settings.development_user_id,
                display_name="Development User",
                primary_email="developer@localhost",
                is_service_account=False,
                issuer=DEVELOPMENT_IDENTITY_ISSUER,
                subject=DEVELOPMENT_IDENTITY_SUBJECT,
            )
        if authorization is None and session_token is not None:
            return await AuthenticationService.authenticate_session(session_token)
        if raw_token is None:
            raise AuthenticationError("Bearer access token required")
        claims = await asyncio.to_thread(_decode_oidc_token, raw_token, settings)
        return await _principal_from_oidc_bearer_claims(claims)


__all__ = [
    "AuthenticatedPrincipal",
    "AuthenticationError",
    "AuthenticationService",
    "McpAccessTokenInfo",
    "current_principal",
    "reset_current_principal",
    "reset_request_context_active",
    "set_current_principal",
    "set_request_context_active",
    "request_context_active",
]
