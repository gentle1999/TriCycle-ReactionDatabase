from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Self
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from tricycle_reaction_db.api.app import create_app
from tricycle_reaction_db.application.dtos import OrganizationAccessView, OrganizationCreate
from tricycle_reaction_db.application.services import authorization as authorization_module
from tricycle_reaction_db.application.services import email as email_module
from tricycle_reaction_db.application.services import (
    organization_management as organization_management_module,
)
from tricycle_reaction_db.application.services.authentication import AuthenticatedPrincipal
from tricycle_reaction_db.application.services.email import (
    EmailDeliveryService,
    EmailDeliveryStatus,
)
from tricycle_reaction_db.core.config import Settings
from tricycle_reaction_db.domain.enums import OrganizationRole, OrganizationStatus, ProjectRole
from tricycle_reaction_db.domain.identity import DEVELOPMENT_USER_ID


@pytest.mark.asyncio
async def test_organization_route_exposes_empty_organizations_for_project_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization = OrganizationAccessView(
        id=UUID("00000000-0000-7000-8000-000000000901"),
        slug="empty-organization",
        name="Empty Organization",
        status=OrganizationStatus.ACTIVE,
        role=OrganizationRole.ADMIN,
        can_create_projects=True,
    )

    async def list_organizations(_: UUID) -> list[OrganizationAccessView]:
        return [organization]

    monkeypatch.setattr(
        authorization_module.AuthorizationService,
        "organization_accesses",
        staticmethod(list_organizations),
    )
    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/organizations")

    assert response.status_code == 200
    assert response.json()[0]["can_create_projects"] is True
    assert response.json()[0]["slug"] == "empty-organization"


@pytest.mark.asyncio
async def test_organization_route_creates_an_owner_membership_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = UUID("00000000-0000-7000-8000-000000000902")

    async def create_organization(
        payload: OrganizationCreate,
        principal: AuthenticatedPrincipal,
    ) -> OrganizationAccessView:
        assert principal.user_id == DEVELOPMENT_USER_ID
        assert payload.slug == "new-team"
        return OrganizationAccessView(
            id=organization_id,
            slug=payload.slug,
            name=payload.name,
            status=OrganizationStatus.ACTIVE,
            role=OrganizationRole.OWNER,
            can_create_projects=True,
        )

    monkeypatch.setattr(
        organization_management_module.OrganizationManagementService,
        "create_organization",
        staticmethod(create_organization),
    )
    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/organizations",
            json={"slug": "new-team", "name": "New Team"},
        )

    assert response.status_code == 201
    assert response.json()["role"] == "owner"
    assert response.json()["can_create_projects"] is True


@pytest.mark.asyncio
async def test_link_delivery_is_explicit_in_development(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        email_module,
        "get_settings",
        lambda: Settings(_env_file=None, email_delivery_mode="link"),
    )
    result = await EmailDeliveryService.send_project_invitation(
        recipient="member@example.test",
        project_name="Demo",
        role=ProjectRole.VIEWER,
        accept_url="http://example.test/invitations/token",
        expires_at=datetime.now(UTC),
    )

    assert result.status is EmailDeliveryStatus.LINK_ONLY
    assert result.error is None


@pytest.mark.asyncio
async def test_smtp_failure_is_reported_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        email_module,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            email_delivery_mode="smtp",
            smtp_host="smtp.example.test",
            smtp_from_email="noreply@example.test",
        ),
    )

    def fail(**_: object) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(email_module, "_send_smtp", fail)
    result = await EmailDeliveryService.send_project_invitation(
        recipient="member@example.test",
        project_name="Demo",
        role=ProjectRole.VIEWER,
        accept_url="http://example.test/invitations/token",
        expires_at=datetime.now(UTC),
    )

    assert result.status is EmailDeliveryStatus.FAILED
    assert result.error == "connection refused"


def test_smtp_delivery_passes_a_verifying_tls_context(monkeypatch: pytest.MonkeyPatch) -> None:
    tls_context = object()
    observed: dict[str, object] = {}

    class FakeSMTP:
        def __init__(self, host: str, port: int, *, timeout: int) -> None:
            observed.update(host=host, port=port, timeout=timeout)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def starttls(self, *, context: object) -> None:
            observed["context"] = context

        def login(self, username: str, password: str) -> None:
            observed.update(username=username, password=password)

        def send_message(self, message: EmailMessage) -> None:
            observed["from"] = message["From"] or ""

    monkeypatch.setattr(
        email_module,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            email_delivery_mode="smtp",
            smtp_host="smtp.example.test",
            smtp_username="mailer",
            smtp_password="secret",
            smtp_from_email="noreply@example.test",
        ),
    )
    monkeypatch.setattr(email_module, "smtp_tls_context", lambda **_kwargs: tls_context)
    monkeypatch.setattr(email_module.smtplib, "SMTP", FakeSMTP)

    email_module._send_smtp(
        recipient="member@example.test",
        subject="Invitation",
        text="Open the invitation link.",
    )

    assert observed == {
        "host": "smtp.example.test",
        "port": 587,
        "timeout": 15,
        "context": tls_context,
        "username": "mailer",
        "password": "secret",
        "from": "noreply@example.test",
    }
