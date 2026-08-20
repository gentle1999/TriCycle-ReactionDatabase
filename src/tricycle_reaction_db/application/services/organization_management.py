"""Use cases for creating and onboarding organizations."""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from tricycle_reaction_db.application.dtos import OrganizationAccessView, OrganizationCreate
from tricycle_reaction_db.application.services.audit import AuditService
from tricycle_reaction_db.application.services.authentication import AuthenticatedPrincipal
from tricycle_reaction_db.db.models import Organization, OrganizationMembership
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import OrganizationRole, OrganizationStatus


class OrganizationManagementError(RuntimeError):
    """Base error for organization-management operations."""


class OrganizationManagementConflictError(OrganizationManagementError):
    pass


class OrganizationManagementService:
    @staticmethod
    async def create_organization(
        payload: OrganizationCreate,
        principal: AuthenticatedPrincipal,
    ) -> OrganizationAccessView:
        slug = payload.slug.strip().lower()
        name = payload.name.strip()
        async with session_factory() as session:
            organization = Organization(
                slug=slug,
                name=name,
                status=OrganizationStatus.ACTIVE,
            )
            session.add(organization)
            try:
                await session.flush()
            except IntegrityError as error:
                await session.rollback()
                raise OrganizationManagementConflictError(
                    "organization slug already exists"
                ) from error
            if organization.id is None:
                raise RuntimeError("database did not assign organization UUID")
            session.add(
                OrganizationMembership(
                    organization_id=organization.id,
                    user_id=principal.user_id,
                    role=OrganizationRole.OWNER,
                )
            )
            await session.commit()
            await session.refresh(organization)
            view = OrganizationAccessView(
                id=organization.id,
                slug=organization.slug,
                name=organization.name,
                status=OrganizationStatus(organization.status),
                role=OrganizationRole.OWNER,
                can_create_projects=True,
            )
        await AuditService.record(
            action="organization.created",
            entity_type="organization",
            entity_id=view.id,
            actor_user_id=principal.user_id,
            metadata={"slug": view.slug, "name": view.name},
        )
        return view


__all__ = [
    "OrganizationManagementConflictError",
    "OrganizationManagementError",
    "OrganizationManagementService",
]
