import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, func
from sqlmodel import col, select

from tricycle_reaction_db.application.services import authentication as authentication_module
from tricycle_reaction_db.application.services.audit import AuditService
from tricycle_reaction_db.db.models import AuditEvent, AuthSession, ExternalIdentity, UserAccount
from tricycle_reaction_db.db.session import engine, session_factory
from tricycle_reaction_db.domain.enums import UserStatus

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


@pytest_asyncio.fixture
async def persisted_auth_identity() -> tuple[UUID, str]:
    user_id = uuid4()
    identity_id = uuid4()
    session_id = uuid4()
    raw_token = f"session-{uuid4().hex}"
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(
            UserAccount(
                id=user_id,
                display_name="Authentication hot path",
                primary_email=f"{user_id}@example.test",
                status=UserStatus.ACTIVE,
            )
        )
        await session.flush()
        session.add(
            ExternalIdentity(
                id=identity_id,
                user_id=user_id,
                issuer="urn:test:hot-path",
                subject=str(user_id),
                email=f"{user_id}@example.test",
                claims={"sub": str(user_id)},
                last_authenticated_at=now,
            )
        )
        session.add(
            AuthSession(
                id=session_id,
                user_id=user_id,
                token_hash=authentication_module.AuthenticationService._token_hash(raw_token),
                expires_at=now + timedelta(hours=1),
                last_seen_at=now,
            )
        )
        await session.commit()
    try:
        yield user_id, raw_token
    finally:
        async with session_factory() as session:
            await session.execute(delete(AuthSession).where(col(AuthSession.user_id) == user_id))
            await session.execute(
                delete(ExternalIdentity).where(col(ExternalIdentity.user_id) == user_id)
            )
            await session.execute(delete(UserAccount).where(col(UserAccount.id) == user_id))
            await session.commit()


def _statement_kinds(statements: list[str]) -> list[str]:
    return [statement.lstrip().split(maxsplit=1)[0].upper() for statement in statements]


@pytest.mark.asyncio
async def test_hot_session_authentication_is_one_select_without_writes(
    persisted_auth_identity: tuple[UUID, str],
) -> None:
    _, raw_token = persisted_auth_identity
    statements: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        principal = await authentication_module.AuthenticationService.authenticate_session(
            raw_token
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    assert principal.primary_email is not None
    assert _statement_kinds(statements) == ["SELECT"]


@pytest.mark.asyncio
async def test_stale_session_authentication_conditionally_updates_last_seen(
    persisted_auth_identity: tuple[UUID, str],
) -> None:
    user_id, raw_token = persisted_auth_identity
    stale_at = datetime.now(UTC) - timedelta(minutes=6)
    async with session_factory() as session:
        await session.execute(
            # Keep the update outside the measured authentication request.
            AuthSession.__table__.update()
            .where(col(AuthSession.user_id) == user_id)
            .values(last_seen_at=stale_at)
        )
        await session.commit()

    statements: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        await authentication_module.AuthenticationService.authenticate_session(raw_token)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    assert _statement_kinds(statements) == ["SELECT", "UPDATE"]


@pytest.mark.asyncio
async def test_one_hundred_hot_session_requests_remain_read_only_without_login_audits(
    persisted_auth_identity: tuple[UUID, str],
) -> None:
    user_id, raw_token = persisted_auth_identity
    async with session_factory() as session:
        audit_count_before = int(
            (
                await session.exec(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(
                        col(AuditEvent.actor_user_id) == user_id,
                        col(AuditEvent.action) == "auth.login",
                    )
                )
            ).one()
        )

    statements: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        for _ in range(100):
            await authentication_module.AuthenticationService.authenticate_session(raw_token)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    assert _statement_kinds(statements) == ["SELECT"] * 100
    async with session_factory() as session:
        audit_count_after = int(
            (
                await session.exec(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(
                        col(AuditEvent.actor_user_id) == user_id,
                        col(AuditEvent.action) == "auth.login",
                    )
                )
            ).one()
        )
    assert audit_count_after == audit_count_before


@pytest.mark.asyncio
async def test_session_listing_filters_inactive_rows_and_cleanup_removes_only_due_rows(
    persisted_auth_identity: tuple[UUID, str],
) -> None:
    user_id, raw_token = persisted_auth_identity
    now = datetime.now(UTC)
    expired_id = uuid4()
    recent_revoked_id = uuid4()
    old_revoked_id = uuid4()
    async with session_factory() as session:
        session.add_all(
            [
                AuthSession(
                    id=expired_id,
                    user_id=user_id,
                    token_hash=authentication_module.AuthenticationService._token_hash(
                        f"expired-{expired_id}"
                    ),
                    expires_at=now - timedelta(minutes=1),
                    last_seen_at=now - timedelta(hours=1),
                ),
                AuthSession(
                    id=recent_revoked_id,
                    user_id=user_id,
                    token_hash=authentication_module.AuthenticationService._token_hash(
                        f"recent-revoked-{recent_revoked_id}"
                    ),
                    expires_at=now + timedelta(hours=1),
                    last_seen_at=now - timedelta(minutes=1),
                    revoked_at=now - timedelta(days=1),
                ),
                AuthSession(
                    id=old_revoked_id,
                    user_id=user_id,
                    token_hash=authentication_module.AuthenticationService._token_hash(
                        f"old-revoked-{old_revoked_id}"
                    ),
                    expires_at=now + timedelta(hours=1),
                    last_seen_at=now - timedelta(days=40),
                    revoked_at=now - timedelta(days=31),
                ),
            ]
        )
        await session.commit()

    active = await authentication_module.AuthenticationService.list_sessions(
        user_id,
        current_token=raw_token,
        limit=10,
    )
    assert len(active) == 1
    assert active[0].current is True

    deleted_count = await authentication_module.AuthenticationService.cleanup_sessions(
        revoked_retention=timedelta(days=30),
        user_id=user_id,
    )
    assert deleted_count == 2
    async with session_factory() as session:
        remaining_ids = set(
            (
                await session.exec(
                    select(AuthSession.id).where(col(AuthSession.user_id) == user_id)
                )
            ).all()
        )
    assert expired_id not in remaining_ids
    assert old_revoked_id not in remaining_ids
    assert recent_revoked_id in remaining_ids


@pytest.mark.asyncio
async def test_existing_bearer_identity_is_read_only(
    persisted_auth_identity: tuple[UUID, str],
) -> None:
    user_id, _ = persisted_auth_identity
    claims = {
        "iss": "urn:test:hot-path",
        "sub": str(user_id),
        "email": "changed@example.test",
        "name": "Changed display name",
    }
    statements: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        principal = await authentication_module._principal_from_oidc_bearer_claims(claims)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    assert principal.user_id == user_id
    assert _statement_kinds(statements) == ["SELECT"]


@pytest.mark.asyncio
async def test_concurrent_first_bearer_provisioning_reloads_unique_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = f"concurrent-{uuid4().hex}"
    claims = {
        "iss": "urn:test:concurrent-provisioning",
        "sub": subject,
        "email": f"{subject}@example.test",
        "name": "Concurrent provisioning",
    }

    async def no_audit(**_: object) -> None:
        return None

    monkeypatch.setattr(AuditService, "record", staticmethod(no_audit))
    results = await asyncio.gather(
        authentication_module._principal_from_oidc_bearer_claims(claims),
        authentication_module._principal_from_oidc_bearer_claims(claims),
    )

    assert len({principal.user_id for principal in results}) == 1
    async with session_factory() as session:
        identity_rows = (
            await session.exec(
                select(ExternalIdentity).where(
                    col(ExternalIdentity.issuer) == claims["iss"],
                    col(ExternalIdentity.subject) == subject,
                )
            )
        ).all()
        assert len(identity_rows) == 1
        user_id = identity_rows[0].user_id
        await session.execute(
            delete(ExternalIdentity).where(col(ExternalIdentity.user_id) == user_id)
        )
        await session.execute(delete(UserAccount).where(col(UserAccount.id) == user_id))
        await session.commit()
