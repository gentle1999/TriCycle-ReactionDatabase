"""Provision deployment-owned identities, organization, and initial project."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import asdict, dataclass
from typing import Literal
from uuid import UUID

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from tricycle_reaction_db.core.config import Settings, get_settings
from tricycle_reaction_db.db.models import (
    AuditEvent,
    ExternalIdentity,
    Organization,
    OrganizationMembership,
    Project,
    ProjectMembership,
    UserAccount,
)
from tricycle_reaction_db.db.session import dispose_engine, session_factory
from tricycle_reaction_db.domain.enums import (
    OrganizationRole,
    OrganizationStatus,
    ProjectRole,
    ProjectStatus,
    UserStatus,
)
from tricycle_reaction_db.domain.identity import (
    DEVELOPMENT_IDENTITY_ID,
    DEVELOPMENT_IDENTITY_ISSUER,
    DEVELOPMENT_IDENTITY_SUBJECT,
    DEVELOPMENT_ORGANIZATION_MEMBERSHIP_ID,
    DEVELOPMENT_PROJECT_MEMBERSHIP_ID,
    DEVELOPMENT_USER_ID,
    SYSTEM_ORGANIZATION_ID,
    SYSTEM_PROJECT_ID,
    SYSTEM_USER_ID,
)

BootstrapMode = Literal["development", "production"]
_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SYSTEM_ORGANIZATION_MEMBERSHIP_ID = UUID("00000000-0000-7000-8000-000000000402")
_SYSTEM_PROJECT_MEMBERSHIP_ID = UUID("00000000-0000-7000-8000-000000000502")


@dataclass(frozen=True, slots=True)
class BootstrapSpec:
    mode: BootstrapMode
    issuer: str
    subject: str
    admin_display_name: str
    admin_email: str
    organization_slug: str
    organization_name: str
    project_slug: str
    project_name: str
    system_user_display_name: str

    @classmethod
    def from_settings(cls, settings: Settings, mode: BootstrapMode) -> BootstrapSpec:
        if mode == "development":
            if settings.environment == "production":
                raise ValueError("development bootstrap is forbidden in production")
            values = {
                "issuer": DEVELOPMENT_IDENTITY_ISSUER,
                "subject": DEVELOPMENT_IDENTITY_SUBJECT,
                "admin_display_name": settings.bootstrap_development_user_display_name,
                "admin_email": settings.bootstrap_development_user_email,
                "organization_slug": settings.bootstrap_organization_slug
                or "development-organization",
                "organization_name": settings.bootstrap_organization_name
                or "Development Organization",
                "project_slug": settings.bootstrap_project_slug or "reaction-database",
                "project_name": settings.bootstrap_project_name or "Reaction Database",
            }
        else:
            if settings.environment != "production":
                raise ValueError("production bootstrap requires TRICYCLE_ENVIRONMENT=production")
            required = {
                "issuer": ("TRICYCLE_BOOTSTRAP_OIDC_ISSUER", settings.bootstrap_oidc_issuer),
                "subject": ("TRICYCLE_BOOTSTRAP_OIDC_SUBJECT", settings.bootstrap_oidc_subject),
                "admin_display_name": (
                    "TRICYCLE_BOOTSTRAP_ADMIN_DISPLAY_NAME",
                    settings.bootstrap_admin_display_name,
                ),
                "admin_email": (
                    "TRICYCLE_BOOTSTRAP_ADMIN_EMAIL",
                    settings.bootstrap_admin_email,
                ),
                "organization_slug": (
                    "TRICYCLE_BOOTSTRAP_ORGANIZATION_SLUG",
                    settings.bootstrap_organization_slug,
                ),
                "organization_name": (
                    "TRICYCLE_BOOTSTRAP_ORGANIZATION_NAME",
                    settings.bootstrap_organization_name,
                ),
                "project_slug": (
                    "TRICYCLE_BOOTSTRAP_PROJECT_SLUG",
                    settings.bootstrap_project_slug,
                ),
                "project_name": (
                    "TRICYCLE_BOOTSTRAP_PROJECT_NAME",
                    settings.bootstrap_project_name,
                ),
            }
            missing = [env_name for env_name, value in required.values() if not value]
            if missing:
                raise ValueError("production bootstrap requires " + ", ".join(missing))
            values = {name: str(value).strip() for name, (_, value) in required.items()}
            if values["issuer"] != settings.oidc_issuer:
                raise ValueError("TRICYCLE_BOOTSTRAP_OIDC_ISSUER must match TRICYCLE_OIDC_ISSUER")

        for field in ("organization_slug", "project_slug"):
            slug = values[field]
            if len(slug) > 128 or _SLUG_PATTERN.fullmatch(slug) is None:
                raise ValueError(f"{field} must be a lowercase DNS-style slug")
        if "@" not in values["admin_email"]:
            raise ValueError("bootstrap administrator email must be valid")
        return cls(
            mode=mode,
            system_user_display_name=settings.bootstrap_system_user_display_name,
            **values,
        )


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    mode: BootstrapMode
    administrator_user_id: str
    organization_id: str
    organization_slug: str
    project_id: str
    project_slug: str


async def _system_user(session: AsyncSession, spec: BootstrapSpec) -> UserAccount:
    user = await session.get(UserAccount, SYSTEM_USER_ID)
    if user is None:
        user = UserAccount(
            id=SYSTEM_USER_ID,
            display_name=spec.system_user_display_name,
            status=UserStatus.ACTIVE,
            is_service_account=True,
        )
        session.add(user)
    else:
        user.display_name = spec.system_user_display_name
        user.status = UserStatus.ACTIVE
        user.is_service_account = True
    return user


async def _administrator(session: AsyncSession, spec: BootstrapSpec) -> UserAccount:
    identity = (
        await session.exec(
            select(ExternalIdentity).where(
                col(ExternalIdentity.issuer) == spec.issuer,
                col(ExternalIdentity.subject) == spec.subject,
            )
        )
    ).one_or_none()
    expected_user_id = DEVELOPMENT_USER_ID if spec.mode == "development" else None
    if identity is not None:
        user = await session.get(UserAccount, identity.user_id)
        if user is None:
            raise RuntimeError("bootstrap identity references a missing user")
        if expected_user_id is not None and user.id != expected_user_id:
            raise RuntimeError("development identity is already bound to another user")
        identity.email = spec.admin_email
        identity.claims = {"auth_mode": spec.mode, "bootstrap": True}
    else:
        user = await session.get(UserAccount, expected_user_id) if expected_user_id else None
        if user is None:
            user = UserAccount(
                id=expected_user_id,
                display_name=spec.admin_display_name,
                primary_email=spec.admin_email,
                status=UserStatus.ACTIVE,
                is_service_account=False,
            )
            session.add(user)
            await session.flush()
        if user.id is None:
            raise RuntimeError("database did not assign bootstrap administrator UUID")
        identity = ExternalIdentity(
            id=DEVELOPMENT_IDENTITY_ID if spec.mode == "development" else None,
            user_id=user.id,
            issuer=spec.issuer,
            subject=spec.subject,
            email=spec.admin_email,
            claims={"auth_mode": spec.mode, "bootstrap": True},
        )
        session.add(identity)
    user.display_name = spec.admin_display_name
    user.primary_email = spec.admin_email
    user.status = UserStatus.ACTIVE
    user.is_service_account = False
    return user


async def _organization(session: AsyncSession, spec: BootstrapSpec) -> Organization:
    expected_id = SYSTEM_ORGANIZATION_ID if spec.mode == "development" else None
    organization = await session.get(Organization, expected_id) if expected_id else None
    slug_match = (
        await session.exec(
            select(Organization).where(col(Organization.slug) == spec.organization_slug)
        )
    ).one_or_none()
    if organization is not None and slug_match is not None and organization.id != slug_match.id:
        raise RuntimeError("development organization UUID and slug refer to different rows")
    organization = organization or slug_match
    if organization is None:
        organization = Organization(
            id=expected_id,
            slug=spec.organization_slug,
            name=spec.organization_name,
            status=OrganizationStatus.ACTIVE,
        )
        session.add(organization)
        await session.flush()
    else:
        organization.slug = spec.organization_slug
        organization.name = spec.organization_name
        organization.status = OrganizationStatus.ACTIVE
    return organization


async def _project(
    session: AsyncSession,
    spec: BootstrapSpec,
    organization: Organization,
) -> Project:
    if organization.id is None:
        raise RuntimeError("database did not assign bootstrap organization UUID")
    expected_id = SYSTEM_PROJECT_ID if spec.mode == "development" else None
    project = await session.get(Project, expected_id) if expected_id else None
    slug_match = (
        await session.exec(
            select(Project).where(
                col(Project.organization_id) == organization.id,
                col(Project.slug) == spec.project_slug,
            )
        )
    ).one_or_none()
    if project is not None and slug_match is not None and project.id != slug_match.id:
        raise RuntimeError("development project UUID and slug refer to different rows")
    project = project or slug_match
    if project is None:
        project = Project(
            id=expected_id,
            organization_id=organization.id,
            slug=spec.project_slug,
            name=spec.project_name,
            status=ProjectStatus.ACTIVE,
        )
        session.add(project)
        await session.flush()
    else:
        if project.organization_id != organization.id:
            raise RuntimeError("bootstrap project belongs to another organization")
        project.slug = spec.project_slug
        project.name = spec.project_name
        project.status = ProjectStatus.ACTIVE
    return project


async def _membership(
    session: AsyncSession,
    *,
    organization: Organization,
    project: Project,
    user: UserAccount,
    organization_membership_id: UUID | None = None,
    project_membership_id: UUID | None = None,
) -> None:
    if organization.id is None or project.id is None or user.id is None:
        raise RuntimeError("bootstrap entities must have UUIDs before memberships are created")
    organization_membership = (
        await session.exec(
            select(OrganizationMembership).where(
                col(OrganizationMembership.organization_id) == organization.id,
                col(OrganizationMembership.user_id) == user.id,
            )
        )
    ).one_or_none()
    if organization_membership is None:
        organization_membership = OrganizationMembership(
            id=organization_membership_id,
            organization_id=organization.id,
            user_id=user.id,
            role=OrganizationRole.OWNER,
        )
        session.add(organization_membership)
    else:
        organization_membership.role = OrganizationRole.OWNER

    project_membership = (
        await session.exec(
            select(ProjectMembership).where(
                col(ProjectMembership.project_id) == project.id,
                col(ProjectMembership.user_id) == user.id,
            )
        )
    ).one_or_none()
    if project_membership is None:
        project_membership = ProjectMembership(
            id=project_membership_id,
            project_id=project.id,
            user_id=user.id,
            role=ProjectRole.MANAGER,
        )
        session.add(project_membership)
    else:
        project_membership.role = ProjectRole.MANAGER


async def bootstrap(spec: BootstrapSpec) -> BootstrapResult:
    async with session_factory() as session:
        system_user = await _system_user(session, spec)
        administrator = await _administrator(session, spec)
        organization = await _organization(session, spec)
        project = await _project(session, spec, organization)
        await _membership(
            session,
            organization=organization,
            project=project,
            user=administrator,
            organization_membership_id=(
                DEVELOPMENT_ORGANIZATION_MEMBERSHIP_ID if spec.mode == "development" else None
            ),
            project_membership_id=(
                DEVELOPMENT_PROJECT_MEMBERSHIP_ID if spec.mode == "development" else None
            ),
        )
        if spec.mode == "development":
            await _membership(
                session,
                organization=organization,
                project=project,
                user=system_user,
                organization_membership_id=_SYSTEM_ORGANIZATION_MEMBERSHIP_ID,
                project_membership_id=_SYSTEM_PROJECT_MEMBERSHIP_ID,
            )
            system_organization_membership = (
                await session.exec(
                    select(OrganizationMembership).where(
                        col(OrganizationMembership.organization_id) == organization.id,
                        col(OrganizationMembership.user_id) == system_user.id,
                    )
                )
            ).one()
            system_organization_membership.role = OrganizationRole.MEMBER
            system_project_membership = (
                await session.exec(
                    select(ProjectMembership).where(
                        col(ProjectMembership.project_id) == project.id,
                        col(ProjectMembership.user_id) == system_user.id,
                    )
                )
            ).one()
            system_project_membership.role = ProjectRole.CONTRIBUTOR
        await session.flush()
        if administrator.id is None or organization.id is None or project.id is None:
            raise RuntimeError("database did not assign all bootstrap UUIDs")
        session.add(
            AuditEvent(
                actor_user_id=administrator.id,
                project_id=project.id,
                action="deployment.bootstrap",
                entity_type="project",
                entity_id=project.id,
                metadata_json={
                    "mode": spec.mode,
                    "organization_slug": organization.slug,
                    "project_slug": project.slug,
                },
            )
        )
        await session.commit()
        return BootstrapResult(
            mode=spec.mode,
            administrator_user_id=str(administrator.id),
            organization_id=str(organization.id),
            organization_slug=organization.slug,
            project_id=str(project.id),
            project_slug=project.slug,
        )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("development", "production"), required=True)
    return parser.parse_args()


async def _run(mode: BootstrapMode) -> BootstrapResult:
    try:
        return await bootstrap(BootstrapSpec.from_settings(get_settings(), mode))
    finally:
        await dispose_engine()


def main() -> None:
    arguments = _arguments()
    result = asyncio.run(_run(arguments.mode))
    print(json.dumps(asdict(result), sort_keys=True))


if __name__ == "__main__":
    main()
