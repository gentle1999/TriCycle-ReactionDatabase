import os
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlmodel import col, select

from tricycle_reaction_db.application.dtos import (
    OrganizationCreate,
    ProjectCreate,
    ProjectInvitationCreate,
    ProjectMemberRoleUpdate,
    ProjectMemberUpsert,
    ProjectUpdate,
)
from tricycle_reaction_db.application.services import (
    AuthenticatedPrincipal,
    InvitationConflictError,
    InvitationNotFoundError,
    InvitationService,
    OrganizationManagementConflictError,
    OrganizationManagementService,
    ProjectManagementConflictError,
    ProjectManagementService,
)
from tricycle_reaction_db.db.models import (
    Organization,
    OrganizationMembership,
    Project,
    ProjectInvitation,
    ProjectMembership,
    UserAccount,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import ProjectRole, ProjectStatus
from tricycle_reaction_db.domain.identity import (
    DEVELOPMENT_IDENTITY_ISSUER,
    DEVELOPMENT_IDENTITY_SUBJECT,
    DEVELOPMENT_USER_ID,
    SYSTEM_ORGANIZATION_ID,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


def _principal(user_id=DEVELOPMENT_USER_ID) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=user_id,
        display_name="Project Manager",
        primary_email=None,
        is_service_account=False,
        issuer=DEVELOPMENT_IDENTITY_ISSUER,
        subject=DEVELOPMENT_IDENTITY_SUBJECT,
    )


@pytest.mark.asyncio
async def test_authenticated_user_can_create_an_organization_and_first_project() -> None:
    suffix = uuid4().hex
    created_organization_id = None
    created_project_id = None
    owner = _principal()

    try:
        organization = await OrganizationManagementService.create_organization(
            OrganizationCreate(slug=f"onboarding-{suffix}", name="Onboarding Organization"),
            owner,
        )
        created_organization_id = organization.id
        assert organization.role.value == "owner"
        assert organization.can_create_projects is True

        project = await ProjectManagementService.create_project(
            ProjectCreate(
                organization_id=organization.id,
                slug="first-project",
                name="First Project",
            ),
            owner,
        )
        created_project_id = project.id
        assert project.organization_id == organization.id
        assert project.role is ProjectRole.MANAGER

        with pytest.raises(
            OrganizationManagementConflictError,
            match="organization slug already exists",
        ):
            await OrganizationManagementService.create_organization(
                OrganizationCreate(
                    slug=f"onboarding-{suffix}",
                    name="Duplicate Organization",
                ),
                owner,
            )
    finally:
        async with session_factory() as session:
            if created_project_id is not None:
                await session.exec(
                    delete(ProjectMembership).where(
                        col(ProjectMembership.project_id) == created_project_id
                    )
                )
                await session.exec(delete(Project).where(col(Project.id) == created_project_id))
            if created_organization_id is not None:
                await session.exec(
                    delete(OrganizationMembership).where(
                        col(OrganizationMembership.organization_id) == created_organization_id
                    )
                )
                await session.exec(
                    delete(Organization).where(col(Organization.id) == created_organization_id)
                )
            await session.commit()


@pytest.mark.asyncio
async def test_project_and_member_management_preserves_a_manager() -> None:
    suffix = uuid4().hex
    created_project_id = None
    member_user_id = uuid4()
    owner = _principal()

    try:
        created = await ProjectManagementService.create_project(
            ProjectCreate(
                organization_id=SYSTEM_ORGANIZATION_ID,
                slug=f"management-{suffix}",
                name="Management Integration Project",
            ),
            owner,
        )
        created_project_id = created.id
        assert created.role is ProjectRole.MANAGER
        assert created.status is ProjectStatus.ACTIVE
        assert "project:manage" in created.permissions
        assert "artifact:delete" in created.permissions
        assert "artifact:manage" in created.permissions

        async with session_factory() as session:
            session.add(
                UserAccount(
                    id=member_user_id,
                    display_name="Second Manager",
                    primary_email=f"management-{suffix}@example.test",
                )
            )
            await session.commit()

        added = await ProjectManagementService.upsert_member(
            created.id,
            ProjectMemberUpsert(user_id=member_user_id, role=ProjectRole.VIEWER),
            owner,
        )
        assert added.role is ProjectRole.VIEWER

        with pytest.raises(ProjectManagementConflictError, match="last project manager"):
            await ProjectManagementService.remove_member(created.id, owner.user_id, owner)

        promoted = await ProjectManagementService.update_member_role(
            created.id,
            member_user_id,
            ProjectMemberRoleUpdate(role=ProjectRole.MANAGER),
            owner,
        )
        assert promoted.role is ProjectRole.MANAGER
        await ProjectManagementService.remove_member(created.id, owner.user_id, owner)

        member_principal = _principal(member_user_id)
        with pytest.raises(ProjectManagementConflictError, match="last project manager"):
            await ProjectManagementService.update_member_role(
                created.id,
                member_user_id,
                ProjectMemberRoleUpdate(role=ProjectRole.CONTRIBUTOR),
                member_principal,
            )

        archived = await ProjectManagementService.update_project(
            created.id,
            ProjectUpdate(name="Archived Project", status=ProjectStatus.ARCHIVED),
            member_principal,
        )
        assert archived.name == "Archived Project"
        assert archived.status is ProjectStatus.ARCHIVED
        assert created.id not in {
            project.id for project in await ProjectManagementService.list_projects(member_principal)
        }
        archived_projects = await ProjectManagementService.list_projects(
            member_principal,
            include_archived=True,
        )
        assert created.id in {project.id for project in archived_projects}
        archived_detail = await ProjectManagementService.get_project(created.id, member_principal)
        assert archived_detail.status is ProjectStatus.ARCHIVED

        restored = await ProjectManagementService.update_project(
            created.id,
            ProjectUpdate(status=ProjectStatus.ACTIVE),
            member_principal,
        )
        assert restored.status is ProjectStatus.ACTIVE
    finally:
        async with session_factory() as session:
            if created_project_id is not None:
                await session.exec(
                    delete(ProjectMembership).where(
                        col(ProjectMembership.project_id) == created_project_id
                    )
                )
                await session.exec(delete(Project).where(col(Project.id) == created_project_id))
            await session.exec(delete(UserAccount).where(col(UserAccount.id) == member_user_id))
            await session.commit()


@pytest.mark.asyncio
async def test_project_invitation_can_be_accepted_by_matching_oidc_email() -> None:
    suffix = uuid4().hex
    created_project_id = None
    invitee_id = uuid4()
    owner = _principal()
    invitee = _principal(invitee_id)
    invitee = AuthenticatedPrincipal(
        user_id=invitee.user_id,
        display_name="Invited Researcher",
        primary_email=f"invitee-{suffix}@example.test",
        is_service_account=False,
        issuer=DEVELOPMENT_IDENTITY_ISSUER,
        subject=f"invitee-{suffix}",
    )

    try:
        created = await ProjectManagementService.create_project(
            ProjectCreate(
                organization_id=SYSTEM_ORGANIZATION_ID,
                slug=f"invitation-{suffix}",
                name="Invitation Integration Project",
            ),
            owner,
        )
        created_project_id = created.id
        async with session_factory() as session:
            session.add(
                UserAccount(
                    id=invitee_id,
                    display_name=invitee.display_name,
                    primary_email=invitee.primary_email,
                )
            )
            await session.commit()

        result = await InvitationService.create(
            created.id,
            ProjectInvitationCreate(
                email=invitee.primary_email,
                role=ProjectRole.VIEWER,
                expires_in_days=7,
            ),
            owner,
            frontend_url="http://127.0.0.1:5176",
        )
        assert result.accept_token
        assert result.invitation.accepted_at is None
        assert result.delivery_status in {"link_only", "sent"}

        accepted = await InvitationService.accept(result.accept_token, invitee)
        assert accepted.accepted_at is not None

        async with session_factory() as session:
            membership = (
                await session.exec(
                    select(ProjectMembership).where(
                        ProjectMembership.project_id == created.id,
                        ProjectMembership.user_id == invitee_id,
                    )
                )
            ).first()
            assert membership is not None
            assert ProjectRole(membership.role) is ProjectRole.VIEWER

        with pytest.raises(InvitationNotFoundError):
            await InvitationService.accept(result.accept_token, invitee)

        mismatch_invitation = await InvitationService.create(
            created.id,
            ProjectInvitationCreate(
                email=f"another-{suffix}@example.test",
                role=ProjectRole.VIEWER,
                expires_in_days=7,
            ),
            owner,
            frontend_url="http://127.0.0.1:5176",
        )
        mismatch = AuthenticatedPrincipal(
            user_id=invitee_id,
            display_name="Wrong User",
            primary_email="wrong@example.test",
            is_service_account=False,
            issuer=DEVELOPMENT_IDENTITY_ISSUER,
            subject="wrong-user",
        )
        with pytest.raises(InvitationConflictError):
            await InvitationService.accept(mismatch_invitation.accept_token, mismatch)
    finally:
        async with session_factory() as session:
            if created_project_id is not None:
                await session.exec(
                    delete(ProjectMembership).where(
                        col(ProjectMembership.project_id) == created_project_id
                    )
                )
                await session.exec(
                    delete(ProjectInvitation).where(
                        col(ProjectInvitation.project_id) == created_project_id
                    )
                )
                await session.exec(delete(Project).where(Project.id == created_project_id))
            await session.exec(delete(UserAccount).where(UserAccount.id == invitee_id))
            await session.commit()
