"""Use cases for creating organizations and managing their memberships."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from tricycle_reaction_db.application.dtos import (
    OrganizationAccessView,
    OrganizationCreate,
    OrganizationMemberUpsert,
    OrganizationMemberView,
)
from tricycle_reaction_db.application.services.audit import AuditService
from tricycle_reaction_db.application.services.authentication import AuthenticatedPrincipal
from tricycle_reaction_db.db.models import (
    Organization,
    OrganizationMembership,
    UserAccount,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import OrganizationRole, OrganizationStatus, UserStatus


class OrganizationManagementError(RuntimeError):
    """Base error for organization-management operations."""


class OrganizationManagementConflictError(OrganizationManagementError):
    pass


class OrganizationManagementNotFoundError(OrganizationManagementError):
    pass


class OrganizationManagementAccessDeniedError(OrganizationManagementError):
    pass


class OrganizationManagementService:
    @staticmethod
    async def _require_organization(
        session: AsyncSession,
        *,
        organization_id: UUID,
        with_for_update: bool = False,
    ) -> Organization:
        organization = await session.get(
            Organization,
            organization_id,
            with_for_update=with_for_update,
        )
        if (
            organization is None
            or OrganizationStatus(organization.status) is not OrganizationStatus.ACTIVE
        ):
            raise OrganizationManagementNotFoundError("organization not found")
        return organization

    @staticmethod
    async def _require_member(
        session: AsyncSession,
        *,
        organization_id: UUID,
        user_id: UUID,
        administrators_only: bool,
    ) -> OrganizationRole:
        user = await session.get(UserAccount, user_id)
        if user is None or UserStatus(user.status) is not UserStatus.ACTIVE:
            raise OrganizationManagementAccessDeniedError("user account is suspended")
        roles = [OrganizationRole.OWNER, OrganizationRole.ADMIN]
        if not administrators_only:
            roles.append(OrganizationRole.MEMBER)
        membership = (
            await session.exec(
                select(OrganizationMembership).where(
                    OrganizationMembership.organization_id == organization_id,
                    OrganizationMembership.user_id == user_id,
                    col(OrganizationMembership.role).in_(roles),
                )
            )
        ).first()
        if membership is None:
            if administrators_only:
                raise OrganizationManagementAccessDeniedError(
                    "organization admin permission required"
                )
            raise OrganizationManagementAccessDeniedError("organization membership required")
        return OrganizationRole(membership.role)

    @staticmethod
    def _member_view(
        membership: OrganizationMembership,
        user: UserAccount,
    ) -> OrganizationMemberView:
        return OrganizationMemberView(
            user_id=membership.user_id,
            display_name=user.display_name,
            primary_email=user.primary_email,
            role=OrganizationRole(membership.role),
            created_at=membership.created_at,
        )

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

    @classmethod
    async def list_members(
        cls,
        organization_id: UUID,
        principal: AuthenticatedPrincipal,
    ) -> list[OrganizationMemberView]:
        async with session_factory() as session:
            await cls._require_organization(session, organization_id=organization_id)
            await cls._require_member(
                session,
                organization_id=organization_id,
                user_id=principal.user_id,
                administrators_only=False,
            )
            rows = (
                await session.exec(
                    select(OrganizationMembership, UserAccount)
                    .join(UserAccount, col(OrganizationMembership.user_id) == col(UserAccount.id))
                    .where(OrganizationMembership.organization_id == organization_id)
                    .order_by(col(UserAccount.display_name), col(UserAccount.id))
                )
            ).all()
        return [cls._member_view(membership, user) for membership, user in rows]

    @classmethod
    async def upsert_member(
        cls,
        organization_id: UUID,
        payload: OrganizationMemberUpsert,
        principal: AuthenticatedPrincipal,
    ) -> OrganizationMemberView:
        async with session_factory() as session:
            await cls._require_organization(
                session,
                organization_id=organization_id,
                with_for_update=True,
            )
            actor_role = await cls._require_member(
                session,
                organization_id=organization_id,
                user_id=principal.user_id,
                administrators_only=True,
            )
            if payload.role is OrganizationRole.OWNER and actor_role is not OrganizationRole.OWNER:
                raise OrganizationManagementAccessDeniedError(
                    "only an organization owner can assign the owner role"
                )
            user = await session.get(UserAccount, payload.user_id)
            if user is None or UserStatus(user.status) is not UserStatus.ACTIVE:
                raise OrganizationManagementNotFoundError("user not found")
            membership = (
                await session.exec(
                    select(OrganizationMembership).where(
                        OrganizationMembership.organization_id == organization_id,
                        OrganizationMembership.user_id == payload.user_id,
                    )
                )
            ).first()
            created = membership is None
            if membership is None:
                membership = OrganizationMembership(
                    organization_id=organization_id,
                    user_id=payload.user_id,
                    role=payload.role,
                )
                session.add(membership)
            else:
                target_role = OrganizationRole(membership.role)
                if (
                    target_role is OrganizationRole.OWNER
                    and actor_role is not OrganizationRole.OWNER
                ):
                    raise OrganizationManagementAccessDeniedError(
                        "only an organization owner can modify another owner"
                    )
                if (
                    target_role is OrganizationRole.OWNER
                    and payload.role is not OrganizationRole.OWNER
                ):
                    owner_count = int(
                        (
                            await session.exec(
                                select(func.count())
                                .select_from(OrganizationMembership)
                                .where(
                                    OrganizationMembership.organization_id == organization_id,
                                    OrganizationMembership.role == OrganizationRole.OWNER,
                                )
                            )
                        ).one()
                    )
                    if owner_count <= 1:
                        raise OrganizationManagementConflictError(
                            "cannot demote the last organization owner"
                        )
                membership.role = payload.role
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise OrganizationManagementConflictError(
                    "organization member already exists"
                ) from error
            await session.refresh(membership)
            if membership.id is None:
                raise RuntimeError("database did not assign organization membership UUID")
            view = cls._member_view(membership, user)
            membership_id = membership.id
        await AuditService.record(
            action="organization.member.added" if created else "organization.member.role_changed",
            entity_type="organization_membership",
            entity_id=membership_id,
            actor_user_id=principal.user_id,
            metadata={"organization_id": str(organization_id), "role": view.role.value},
        )
        return view

    @classmethod
    async def remove_member(
        cls,
        organization_id: UUID,
        user_id: UUID,
        principal: AuthenticatedPrincipal,
    ) -> None:
        async with session_factory() as session:
            await cls._require_organization(
                session,
                organization_id=organization_id,
                with_for_update=True,
            )
            actor_role = await cls._require_member(
                session,
                organization_id=organization_id,
                user_id=principal.user_id,
                administrators_only=True,
            )
            membership = (
                await session.exec(
                    select(OrganizationMembership).where(
                        OrganizationMembership.organization_id == organization_id,
                        OrganizationMembership.user_id == user_id,
                    )
                )
            ).first()
            if membership is None:
                raise OrganizationManagementNotFoundError("organization member not found")
            target_role = OrganizationRole(membership.role)
            if target_role is OrganizationRole.OWNER and actor_role is not OrganizationRole.OWNER:
                raise OrganizationManagementAccessDeniedError(
                    "only an organization owner can remove another owner"
                )
            if target_role is OrganizationRole.OWNER:
                owner_count = int(
                    (
                        await session.exec(
                            select(func.count())
                            .select_from(OrganizationMembership)
                            .where(
                                OrganizationMembership.organization_id == organization_id,
                                OrganizationMembership.role == OrganizationRole.OWNER,
                            )
                        )
                    ).one()
                )
                if owner_count <= 1:
                    raise OrganizationManagementConflictError(
                        "cannot remove the last organization owner"
                    )
            membership_id = membership.id
            if membership_id is None:
                raise RuntimeError("persisted organization membership is missing its UUID")
            await session.delete(membership)
            await session.commit()
        await AuditService.record(
            action="organization.member.removed",
            entity_type="organization_membership",
            entity_id=membership_id,
            actor_user_id=principal.user_id,
            metadata={"organization_id": str(organization_id), "user_id": str(user_id)},
        )


__all__ = [
    "OrganizationManagementAccessDeniedError",
    "OrganizationManagementConflictError",
    "OrganizationManagementError",
    "OrganizationManagementNotFoundError",
    "OrganizationManagementService",
]
