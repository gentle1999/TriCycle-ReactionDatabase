"""Project membership resolution and permission checks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import and_, exists, or_
from sqlalchemy.orm import aliased
from sqlmodel import col, select

from tricycle_reaction_db.application.dtos import (
    CurrentUserView,
    IdentityView,
    OrganizationAccessView,
    ProjectAccessView,
)
from tricycle_reaction_db.application.services.authentication import AuthenticatedPrincipal
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
from tricycle_reaction_db.domain.identity import SYSTEM_ORGANIZATION_ID


class ProjectPermission(StrEnum):
    ARTIFACT_READ = "artifact:read"
    ARTIFACT_DOWNLOAD = "artifact:download"
    ARTIFACT_UPLOAD = "artifact:upload"
    ARTIFACT_DELETE = "artifact:delete"
    ARTIFACT_MANAGE = "artifact:manage"
    PROJECT_MANAGE = "project:manage"


_PROJECT_ROLE_PERMISSIONS: dict[ProjectRole, frozenset[ProjectPermission]] = {
    ProjectRole.MANAGER: frozenset(ProjectPermission),
    ProjectRole.CONTRIBUTOR: frozenset(
        {
            ProjectPermission.ARTIFACT_READ,
            ProjectPermission.ARTIFACT_DOWNLOAD,
            ProjectPermission.ARTIFACT_UPLOAD,
        }
    ),
    ProjectRole.VIEWER: frozenset(
        {ProjectPermission.ARTIFACT_READ, ProjectPermission.ARTIFACT_DOWNLOAD}
    ),
}


class ProjectAccessDeniedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProjectAccess:
    project: Project
    organization: Organization
    organization_role: OrganizationRole | None
    project_role: ProjectRole | None
    permissions: frozenset[ProjectPermission]


class AuthorizationService:
    @staticmethod
    def project_permission_predicate(
        user_id: UUID,
        project_id: Any,
        permission: ProjectPermission,
    ) -> Any:
        """Return a correlated EXISTS predicate without loading project IDs."""

        organization_ref = aliased(Organization)
        project_ref = aliased(Project)
        organization_access = exists(
            select(1)
            .select_from(OrganizationMembership)
            .join(
                organization_ref,
                col(OrganizationMembership.organization_id) == col(organization_ref.id),
            )
            .join(project_ref, col(project_ref.organization_id) == col(organization_ref.id))
            .where(
                col(OrganizationMembership.user_id) == user_id,
                col(OrganizationMembership.role).in_(
                    (OrganizationRole.OWNER, OrganizationRole.ADMIN)
                ),
                col(organization_ref.status) == OrganizationStatus.ACTIVE,
                col(project_ref.id) == project_id,
            )
        )
        project_roles = {
            ProjectPermission.ARTIFACT_READ: (
                ProjectRole.MANAGER,
                ProjectRole.CONTRIBUTOR,
                ProjectRole.VIEWER,
            ),
            ProjectPermission.ARTIFACT_DOWNLOAD: (
                ProjectRole.MANAGER,
                ProjectRole.CONTRIBUTOR,
                ProjectRole.VIEWER,
            ),
            ProjectPermission.ARTIFACT_UPLOAD: (ProjectRole.MANAGER, ProjectRole.CONTRIBUTOR),
            ProjectPermission.ARTIFACT_DELETE: (ProjectRole.MANAGER,),
            ProjectPermission.ARTIFACT_MANAGE: (ProjectRole.MANAGER,),
            ProjectPermission.PROJECT_MANAGE: (ProjectRole.MANAGER,),
        }[permission]
        direct_access = exists(
            select(1)
            .select_from(ProjectMembership)
            .where(
                col(ProjectMembership.project_id) == project_id,
                col(ProjectMembership.user_id) == user_id,
                col(ProjectMembership.role).in_(project_roles),
            )
        )
        return or_(organization_access, direct_access)

    @staticmethod
    async def organization_accesses(user_id: UUID) -> list[OrganizationAccessView]:
        async with session_factory() as session:
            memberships = (
                await session.exec(
                    select(OrganizationMembership, Organization)
                    .join(
                        Organization,
                        col(OrganizationMembership.organization_id) == col(Organization.id),
                    )
                    .where(
                        OrganizationMembership.user_id == user_id,
                        Organization.status == OrganizationStatus.ACTIVE,
                    )
                )
            ).all()
            project_organizations = (
                await session.exec(
                    select(Organization)
                    .join(Project, col(Project.organization_id) == col(Organization.id))
                    .join(
                        ProjectMembership,
                        col(ProjectMembership.project_id) == col(Project.id),
                    )
                    .where(
                        ProjectMembership.user_id == user_id,
                        Organization.status == OrganizationStatus.ACTIVE,
                    )
                )
            ).all()
        roles = {
            organization.id: OrganizationRole(membership.role)
            for membership, organization in memberships
        }
        organizations = {organization.id: organization for _, organization in memberships}
        organizations.update(
            {organization.id: organization for organization in project_organizations}
        )
        return [
            OrganizationAccessView(
                id=organization.id,
                slug=organization.slug,
                name=organization.name,
                status=OrganizationStatus(organization.status),
                role=roles.get(organization.id),
                can_create_projects=roles.get(organization.id)
                in {OrganizationRole.OWNER, OrganizationRole.ADMIN},
            )
            for organization in sorted(organizations.values(), key=lambda item: item.slug)
            if organization.id is not None
        ]

    @staticmethod
    async def project_accesses(
        user_id: UUID,
        *,
        include_archived: bool = False,
    ) -> list[ProjectAccess]:
        async with session_factory() as session:
            statement = (
                select(
                    Project,
                    Organization,
                    OrganizationMembership.role,
                    ProjectMembership.role,
                )
                .join(Organization, col(Project.organization_id) == col(Organization.id))
                .outerjoin(
                    OrganizationMembership,
                    and_(
                        col(OrganizationMembership.organization_id) == col(Organization.id),
                        col(OrganizationMembership.user_id) == user_id,
                    ),
                )
                .outerjoin(
                    ProjectMembership,
                    and_(
                        col(ProjectMembership.project_id) == col(Project.id),
                        col(ProjectMembership.user_id) == user_id,
                    ),
                )
                .where(
                    Organization.status == OrganizationStatus.ACTIVE,
                    or_(
                        col(OrganizationMembership.role).in_(
                            (OrganizationRole.OWNER, OrganizationRole.ADMIN)
                        ),
                        col(ProjectMembership.user_id).is_not(None),
                    ),
                )
                .order_by(col(Organization.slug), col(Project.slug))
            )
            if not include_archived:
                statement = statement.where(Project.status == ProjectStatus.ACTIVE)
            rows = (await session.exec(statement)).all()

        accesses: list[ProjectAccess] = []
        for project, organization, organization_role_raw, project_role_raw in rows:
            if project.id is None or organization.id is None:
                raise RuntimeError("persisted project access row is missing its UUID")
            organization_role = (
                OrganizationRole(organization_role_raw)
                if organization_role_raw is not None
                else None
            )
            project_role = ProjectRole(project_role_raw) if project_role_raw is not None else None
            if organization_role in {OrganizationRole.OWNER, OrganizationRole.ADMIN}:
                permissions = frozenset(ProjectPermission)
            elif project_role is not None:
                permissions = _PROJECT_ROLE_PERMISSIONS[project_role]
            else:
                continue
            accesses.append(
                ProjectAccess(
                    project=project,
                    organization=organization,
                    organization_role=organization_role,
                    project_role=project_role,
                    permissions=permissions,
                )
            )
        return accesses

    @classmethod
    async def accessible_project_ids(
        cls,
        user_id: UUID,
        permission: ProjectPermission,
    ) -> set[UUID]:
        async with session_factory() as session:
            statement = (
                select(Project.id)
                .join(Organization, col(Project.organization_id) == col(Organization.id))
                .where(
                    Organization.status == OrganizationStatus.ACTIVE,
                    Project.status == ProjectStatus.ACTIVE,
                    cls.project_permission_predicate(user_id, col(Project.id), permission),
                )
            )
            return {
                project_id
                for project_id in (await session.exec(statement)).all()
                if project_id is not None
            }

    @classmethod
    async def require_project_permission(
        cls,
        user_id: UUID,
        project_id: UUID,
        permission: ProjectPermission,
    ) -> None:
        async with session_factory() as session:
            accessible_project = (
                select(1)
                .select_from(Project)
                .join(Organization, col(Project.organization_id) == col(Organization.id))
                .where(
                    Project.id == project_id,
                    Project.status == ProjectStatus.ACTIVE,
                    Organization.status == OrganizationStatus.ACTIVE,
                    cls.project_permission_predicate(user_id, col(Project.id), permission),
                )
            )
            allowed = bool((await session.exec(select(exists(accessible_project)))).one())
        if not allowed:
            raise ProjectAccessDeniedError(
                f"user does not have {permission.value} on project {project_id}"
            )

    @staticmethod
    async def require_system_curator(user_id: UUID) -> None:
        """Restrict global reaction curation to system organization administrators."""

        async with session_factory() as session:
            statement = select(1).where(
                exists(
                    select(1)
                    .select_from(OrganizationMembership)
                    .join(
                        Organization,
                        col(OrganizationMembership.organization_id) == col(Organization.id),
                    )
                    .where(
                        col(OrganizationMembership.organization_id) == SYSTEM_ORGANIZATION_ID,
                        col(OrganizationMembership.user_id) == user_id,
                        col(OrganizationMembership.role).in_(
                            (OrganizationRole.OWNER, OrganizationRole.ADMIN)
                        ),
                        col(Organization.status) == OrganizationStatus.ACTIVE,
                    )
                )
            )
            allowed = bool((await session.exec(statement)).first())
        if not allowed:
            raise ProjectAccessDeniedError("system curator permission is required")

    @classmethod
    async def current_user_view(cls, principal: AuthenticatedPrincipal) -> CurrentUserView:
        async with session_factory() as session:
            user = await session.get(UserAccount, principal.user_id)
        if user is None or user.id is None:
            raise RuntimeError("authenticated user is not provisioned in the database")
        if user.status is not UserStatus.ACTIVE:
            raise ProjectAccessDeniedError("user account is suspended")
        accesses = await cls.project_accesses(principal.user_id)
        return CurrentUserView(
            id=user.id,
            display_name=user.display_name,
            primary_email=user.primary_email,
            is_service_account=user.is_service_account,
            identity=IdentityView(issuer=principal.issuer, subject=principal.subject),
            projects=[
                ProjectAccessView(
                    project_id=access.project.id,
                    project_slug=access.project.slug,
                    project_name=access.project.name,
                    organization_id=access.organization.id,
                    organization_slug=access.organization.slug,
                    organization_name=access.organization.name,
                    organization_role=access.organization_role,
                    project_role=access.project_role,
                    permissions=sorted(permission.value for permission in access.permissions),
                )
                for access in accesses
                if access.project.id is not None and access.organization.id is not None
            ],
        )


__all__ = [
    "AuthorizationService",
    "ProjectAccess",
    "ProjectAccessDeniedError",
    "ProjectPermission",
]
