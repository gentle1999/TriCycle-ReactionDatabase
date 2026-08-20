"""Small database-backed user directory and account-status controls."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_
from sqlmodel import col, select

from tricycle_reaction_db.application.dtos import UserPage, UserStatusUpdate, UserSummaryView
from tricycle_reaction_db.application.services.authentication import AuthenticatedPrincipal
from tricycle_reaction_db.application.services.authorization import (
    AuthorizationService,
    ProjectPermission,
)
from tricycle_reaction_db.db.models import (
    Organization,
    OrganizationMembership,
    ProjectMembership,
    UserAccount,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import (
    OrganizationRole,
    OrganizationStatus,
    ProjectRole,
    UserStatus,
)
from tricycle_reaction_db.domain.identity import SYSTEM_ORGANIZATION_ID, SYSTEM_USER_ID


class UserManagementError(RuntimeError):
    """Base error for user-directory operations."""


class UserManagementNotFoundError(UserManagementError):
    pass


class UserManagementConflictError(UserManagementError):
    pass


class UserManagementService:
    @staticmethod
    def _view(
        user: UserAccount,
        *,
        project_role: ProjectRole | None = None,
    ) -> UserSummaryView:
        if user.id is None:
            raise RuntimeError("persisted user is missing its UUID")
        return UserSummaryView(
            id=user.id,
            display_name=user.display_name,
            primary_email=user.primary_email,
            status=UserStatus(user.status),
            is_service_account=user.is_service_account,
            last_authenticated_at=user.last_authenticated_at,
            created_at=user.created_at,
            project_role=project_role,
        )

    @staticmethod
    async def _require_system_admin(user_id: UUID) -> None:
        async with session_factory() as session:
            actor = await session.get(UserAccount, user_id)
            if actor is None or UserStatus(actor.status) is not UserStatus.ACTIVE:
                raise UserManagementNotFoundError("user not found")
            membership = (
                await session.exec(
                    select(OrganizationMembership)
                    .join(
                        Organization,
                        col(OrganizationMembership.organization_id) == col(Organization.id),
                    )
                    .where(
                        OrganizationMembership.organization_id == SYSTEM_ORGANIZATION_ID,
                        OrganizationMembership.user_id == user_id,
                        col(OrganizationMembership.role).in_(
                            [OrganizationRole.OWNER, OrganizationRole.ADMIN]
                        ),
                        Organization.status == OrganizationStatus.ACTIVE,
                    )
                )
            ).first()
        if membership is None:
            raise UserManagementNotFoundError("user not found")

    @classmethod
    async def list_users(
        cls,
        principal: AuthenticatedPrincipal,
        *,
        query: str | None = None,
        status: UserStatus | None = None,
        project_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> UserPage:
        if project_id is None:
            await cls._require_system_admin(principal.user_id)
        else:
            await AuthorizationService.require_project_permission(
                principal.user_id,
                project_id,
                ProjectPermission.PROJECT_MANAGE,
            )

        criteria: list[Any] = []
        if project_id is not None:
            criteria.extend(
                (
                    col(UserAccount.status) == UserStatus.ACTIVE,
                    col(UserAccount.is_service_account).is_(False),
                )
            )
        if status is not None:
            criteria.append(col(UserAccount.status) == status)
        normalized_query = query.strip() if query is not None else ""
        if normalized_query:
            criteria.append(
                or_(
                    col(UserAccount.display_name).ilike(f"%{normalized_query}%"),
                    col(UserAccount.primary_email).ilike(f"%{normalized_query}%"),
                )
            )

        async with session_factory() as session:
            count_statement = select(func.count()).select_from(UserAccount).where(*criteria)
            total = int((await session.exec(count_statement)).one())
            if project_id is None:
                user_statement = (
                    select(UserAccount)
                    .where(*criteria)
                    .order_by(col(UserAccount.display_name), col(UserAccount.id))
                    .offset(offset)
                    .limit(limit)
                )
                users = (await session.exec(user_statement)).all()
                items = [cls._view(user) for user in users]
            else:
                join_criterion = and_(
                    col(ProjectMembership.user_id) == col(UserAccount.id),
                    col(ProjectMembership.project_id) == project_id,
                )
                project_statement = (
                    select(UserAccount, ProjectMembership)
                    .outerjoin(ProjectMembership, join_criterion)
                    .where(*criteria)
                    .order_by(col(UserAccount.display_name), col(UserAccount.id))
                    .offset(offset)
                    .limit(limit)
                )
                rows = (await session.exec(project_statement)).all()
                items = [
                    cls._view(
                        user,
                        project_role=(
                            ProjectRole(membership.role) if membership is not None else None
                        ),
                    )
                    for user, membership in rows
                ]
        return UserPage(items=items, total=total, limit=limit, offset=offset)

    @classmethod
    async def get_user(
        cls,
        user_id: UUID,
        principal: AuthenticatedPrincipal,
    ) -> UserSummaryView:
        await cls._require_system_admin(principal.user_id)
        async with session_factory() as session:
            user = await session.get(UserAccount, user_id)
        if user is None:
            raise UserManagementNotFoundError("user not found")
        return cls._view(user)

    @classmethod
    async def update_status(
        cls,
        user_id: UUID,
        payload: UserStatusUpdate,
        principal: AuthenticatedPrincipal,
    ) -> UserSummaryView:
        await cls._require_system_admin(principal.user_id)
        if user_id == SYSTEM_USER_ID:
            raise UserManagementConflictError("the system service account cannot be suspended")
        if user_id == principal.user_id and payload.status is UserStatus.SUSPENDED:
            raise UserManagementConflictError("an administrator cannot suspend their own account")
        async with session_factory() as session:
            user = await session.get(UserAccount, user_id, with_for_update=True)
            if user is None:
                raise UserManagementNotFoundError("user not found")
            user.status = payload.status
            session.add(user)
            await session.commit()
            await session.refresh(user)
            return cls._view(user)


__all__ = [
    "UserManagementConflictError",
    "UserManagementError",
    "UserManagementNotFoundError",
    "UserManagementService",
]
