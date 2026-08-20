"""Use cases for local user profiles, projects, and project membership."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from tricycle_reaction_db.application.dtos import (
    ProjectCreate,
    ProjectMemberRoleUpdate,
    ProjectMemberUpsert,
    ProjectMemberView,
    ProjectUpdate,
    ProjectView,
)
from tricycle_reaction_db.application.services.audit import AuditService
from tricycle_reaction_db.application.services.authentication import AuthenticatedPrincipal
from tricycle_reaction_db.application.services.authorization import (
    _PROJECT_ROLE_PERMISSIONS,
    AuthorizationService,
    ProjectAccessDeniedError,
    ProjectPermission,
)
from tricycle_reaction_db.db.models import (
    Organization,
    OrganizationMembership,
    Project,
    ProjectMembership,
    UserAccount,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import (
    OrganizationRole,
    OrganizationStatus,
    ProjectRole,
    ProjectStatus,
    UserStatus,
)


class ProjectManagementError(RuntimeError):
    """Base error for project-management operations."""


class ProjectManagementNotFoundError(ProjectManagementError):
    pass


class ProjectManagementConflictError(ProjectManagementError):
    pass


class ProjectManagementService:
    @staticmethod
    def _project_view(
        project: Project,
        organization: Organization,
        *,
        role: ProjectRole | None,
        organization_role: OrganizationRole | None,
    ) -> ProjectView:
        if project.id is None or organization.id is None:
            raise RuntimeError("persisted project is missing its UUID")
        if organization_role in {OrganizationRole.OWNER, OrganizationRole.ADMIN}:
            permissions = frozenset(ProjectPermission)
        elif role is not None:
            permissions = _PROJECT_ROLE_PERMISSIONS[role]
        else:
            permissions = frozenset()
        return ProjectView(
            id=project.id,
            organization_id=organization.id,
            organization_slug=organization.slug,
            organization_name=organization.name,
            slug=project.slug,
            name=project.name,
            status=ProjectStatus(project.status),
            role=role,
            organization_role=organization_role,
            permissions=sorted(permission.value for permission in permissions),
            created_at=project.created_at,
        )

    @staticmethod
    async def _organization_admin(
        session: AsyncSession,
        *,
        user_id: UUID,
        organization_id: UUID,
    ) -> OrganizationRole | None:
        membership = (
            await session.exec(
                select(OrganizationMembership).where(
                    OrganizationMembership.organization_id == organization_id,
                    OrganizationMembership.user_id == user_id,
                    col(OrganizationMembership.role).in_(
                        [OrganizationRole.OWNER, OrganizationRole.ADMIN]
                    ),
                )
            )
        ).first()
        return OrganizationRole(membership.role) if membership is not None else None

    @classmethod
    async def _require_project_manager(
        cls,
        session: AsyncSession,
        *,
        user_id: UUID,
        project: Project,
    ) -> OrganizationRole | None:
        if project.id is None:
            raise ProjectManagementNotFoundError("project not found")
        user = await session.get(UserAccount, user_id)
        if user is None or UserStatus(user.status) is not UserStatus.ACTIVE:
            raise ProjectAccessDeniedError("user account is suspended")
        organization = await session.get(Organization, project.organization_id)
        if (
            organization is None
            or OrganizationStatus(organization.status) is not OrganizationStatus.ACTIVE
        ):
            raise ProjectManagementNotFoundError("project not found")
        organization_role = await cls._organization_admin(
            session,
            user_id=user_id,
            organization_id=project.organization_id,
        )
        project_membership = (
            await session.exec(
                select(ProjectMembership).where(
                    ProjectMembership.project_id == project.id,
                    ProjectMembership.user_id == user_id,
                )
            )
        ).first()
        if organization_role is None and (
            project_membership is None
            or ProjectRole(project_membership.role) is not ProjectRole.MANAGER
        ):
            raise ProjectAccessDeniedError("project management permission required")
        return organization_role

    @classmethod
    async def list_projects(
        cls,
        principal: AuthenticatedPrincipal,
        *,
        include_archived: bool = False,
    ) -> list[ProjectView]:
        accesses = await AuthorizationService.project_accesses(
            principal.user_id,
            include_archived=include_archived,
        )
        return [
            cls._project_view(
                access.project,
                access.organization,
                role=access.project_role,
                organization_role=access.organization_role,
            )
            for access in accesses
        ]

    @classmethod
    async def get_project(
        cls,
        project_id: UUID,
        principal: AuthenticatedPrincipal,
    ) -> ProjectView:
        access = next(
            (
                access
                for access in await AuthorizationService.project_accesses(
                    principal.user_id,
                    include_archived=True,
                )
                if access.project.id == project_id
            ),
            None,
        )
        if access is None:
            raise ProjectManagementNotFoundError("project not found")
        return cls._project_view(
            access.project,
            access.organization,
            role=access.project_role,
            organization_role=access.organization_role,
        )

    @classmethod
    async def create_project(
        cls,
        payload: ProjectCreate,
        principal: AuthenticatedPrincipal,
    ) -> ProjectView:
        async with session_factory() as session:
            organization = await session.get(Organization, payload.organization_id)
            if (
                organization is None
                or OrganizationStatus(organization.status) is not OrganizationStatus.ACTIVE
            ):
                raise ProjectManagementNotFoundError("organization not found")
            organization_role = await cls._organization_admin(
                session,
                user_id=principal.user_id,
                organization_id=payload.organization_id,
            )
            if organization_role is None:
                raise ProjectAccessDeniedError("organization admin permission required")
            project = Project(
                organization_id=payload.organization_id,
                slug=payload.slug,
                name=payload.name,
                status=ProjectStatus.ACTIVE,
            )
            session.add(project)
            try:
                await session.flush()
            except IntegrityError as error:
                await session.rollback()
                raise ProjectManagementConflictError("project slug already exists") from error
            if project.id is None:
                raise RuntimeError("database did not assign project UUID")
            session.add(
                ProjectMembership(
                    project_id=project.id,
                    user_id=principal.user_id,
                    role=ProjectRole.MANAGER,
                )
            )
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise ProjectManagementConflictError("project slug already exists") from error
            await session.refresh(project)
            view = cls._project_view(
                project,
                organization,
                role=ProjectRole.MANAGER,
                organization_role=organization_role,
            )
        await AuditService.record(
            action="project.created",
            entity_type="project",
            entity_id=view.id,
            actor_user_id=principal.user_id,
            project_id=view.id,
            metadata={"slug": view.slug, "name": view.name},
        )
        return view

    @classmethod
    async def update_project(
        cls,
        project_id: UUID,
        payload: ProjectUpdate,
        principal: AuthenticatedPrincipal,
    ) -> ProjectView:
        if payload.slug is None and payload.name is None and payload.status is None:
            raise ProjectManagementConflictError("at least one project field must be supplied")
        async with session_factory() as session:
            project = await session.get(Project, project_id, with_for_update=True)
            if project is None:
                raise ProjectManagementNotFoundError("project not found")
            organization = await session.get(Organization, project.organization_id)
            if organization is None:
                raise ProjectManagementNotFoundError("project not found")
            organization_role = await cls._require_project_manager(
                session,
                user_id=principal.user_id,
                project=project,
            )
            if payload.slug is not None:
                project.slug = payload.slug
            if payload.name is not None:
                project.name = payload.name
            if payload.status is not None:
                project.status = payload.status
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise ProjectManagementConflictError("project slug already exists") from error
            await session.refresh(project)
            membership = (
                await session.exec(
                    select(ProjectMembership).where(
                        ProjectMembership.project_id == project_id,
                        ProjectMembership.user_id == principal.user_id,
                    )
                )
            ).first()
            view = cls._project_view(
                project,
                organization,
                role=ProjectRole(membership.role) if membership is not None else None,
                organization_role=organization_role,
            )
        await AuditService.record(
            action="project.updated",
            entity_type="project",
            entity_id=view.id,
            actor_user_id=principal.user_id,
            project_id=view.id,
            metadata={"slug": view.slug, "name": view.name, "status": view.status.value},
        )
        return view

    @classmethod
    async def list_members(
        cls,
        project_id: UUID,
        principal: AuthenticatedPrincipal,
    ) -> list[ProjectMemberView]:
        async with session_factory() as session:
            project = await session.get(Project, project_id)
            if project is None:
                raise ProjectManagementNotFoundError("project not found")
            await cls._require_project_manager(
                session,
                user_id=principal.user_id,
                project=project,
            )
            rows = (
                await session.exec(
                    select(ProjectMembership, UserAccount)
                    .join(UserAccount, col(ProjectMembership.user_id) == col(UserAccount.id))
                    .where(ProjectMembership.project_id == project_id)
                    .order_by(col(UserAccount.display_name), col(UserAccount.id))
                )
            ).all()
        return [
            ProjectMemberView(
                user_id=membership.user_id,
                display_name=user.display_name,
                primary_email=user.primary_email,
                role=ProjectRole(membership.role),
                created_at=membership.created_at,
            )
            for membership, user in rows
        ]

    @classmethod
    async def upsert_member(
        cls,
        project_id: UUID,
        payload: ProjectMemberUpsert,
        principal: AuthenticatedPrincipal,
    ) -> ProjectMemberView:
        async with session_factory() as session:
            project = await session.get(Project, project_id, with_for_update=True)
            if project is None:
                raise ProjectManagementNotFoundError("project not found")
            await cls._require_project_manager(session, user_id=principal.user_id, project=project)
            user = await session.get(UserAccount, payload.user_id)
            if user is None or UserStatus(user.status) is not UserStatus.ACTIVE:
                raise ProjectManagementNotFoundError("user not found")
            membership = (
                await session.exec(
                    select(ProjectMembership).where(
                        ProjectMembership.project_id == project_id,
                        ProjectMembership.user_id == payload.user_id,
                    )
                )
            ).first()
            created = membership is None
            if membership is None:
                membership = ProjectMembership(
                    project_id=project_id,
                    user_id=payload.user_id,
                    role=payload.role,
                )
                session.add(membership)
            else:
                if (
                    ProjectRole(membership.role) is ProjectRole.MANAGER
                    and payload.role is not ProjectRole.MANAGER
                ):
                    manager_count = int(
                        (
                            await session.exec(
                                select(func.count())
                                .select_from(ProjectMembership)
                                .where(
                                    ProjectMembership.project_id == project_id,
                                    ProjectMembership.role == ProjectRole.MANAGER,
                                )
                            )
                        ).one()
                    )
                    if manager_count <= 1:
                        raise ProjectManagementConflictError(
                            "cannot demote the last project manager"
                        )
                membership.role = payload.role
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise ProjectManagementConflictError("project member already exists") from error
            await session.refresh(membership)
            view = ProjectMemberView(
                user_id=user.id if user.id is not None else payload.user_id,
                display_name=user.display_name,
                primary_email=user.primary_email,
                role=ProjectRole(membership.role),
                created_at=membership.created_at,
            )
        await AuditService.record(
            action="project.member.added" if created else "project.member.role_changed",
            entity_type="project_membership",
            entity_id=view.user_id,
            actor_user_id=principal.user_id,
            project_id=project_id,
            metadata={"role": view.role.value},
        )
        return view

    @classmethod
    async def update_member_role(
        cls,
        project_id: UUID,
        user_id: UUID,
        payload: ProjectMemberRoleUpdate,
        principal: AuthenticatedPrincipal,
    ) -> ProjectMemberView:
        return await cls.upsert_member(
            project_id,
            ProjectMemberUpsert(user_id=user_id, role=payload.role),
            principal,
        )

    @classmethod
    async def remove_member(
        cls,
        project_id: UUID,
        user_id: UUID,
        principal: AuthenticatedPrincipal,
    ) -> None:
        async with session_factory() as session:
            project = await session.get(Project, project_id, with_for_update=True)
            if project is None:
                raise ProjectManagementNotFoundError("project not found")
            await cls._require_project_manager(session, user_id=principal.user_id, project=project)
            membership = (
                await session.exec(
                    select(ProjectMembership).where(
                        ProjectMembership.project_id == project_id,
                        ProjectMembership.user_id == user_id,
                    )
                )
            ).first()
            if membership is None:
                raise ProjectManagementNotFoundError("project member not found")
            if ProjectRole(membership.role) is ProjectRole.MANAGER:
                manager_count = int(
                    (
                        await session.exec(
                            select(func.count())
                            .select_from(ProjectMembership)
                            .where(
                                ProjectMembership.project_id == project_id,
                                ProjectMembership.role == ProjectRole.MANAGER,
                            )
                        )
                    ).one()
                )
                if manager_count <= 1:
                    raise ProjectManagementConflictError("cannot remove the last project manager")
            await session.delete(membership)
            await session.commit()
        await AuditService.record(
            action="project.member.removed",
            entity_type="project_membership",
            entity_id=user_id,
            actor_user_id=principal.user_id,
            project_id=project_id,
        )


__all__ = [
    "ProjectManagementConflictError",
    "ProjectManagementError",
    "ProjectManagementNotFoundError",
    "ProjectManagementService",
]
