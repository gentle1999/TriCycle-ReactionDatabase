"""Project and project-membership management routes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from tricycle_reaction_db.api.authentication import get_authenticated_principal
from tricycle_reaction_db.application.dtos import (
    AuditEventView,
    ProjectCreate,
    ProjectInvitationCreate,
    ProjectInvitationCreateResult,
    ProjectInvitationView,
    ProjectMemberRoleUpdate,
    ProjectMemberUpsert,
    ProjectMemberView,
    ProjectUpdate,
    ProjectView,
)
from tricycle_reaction_db.application.services.audit import AuditService
from tricycle_reaction_db.application.services.authentication import AuthenticatedPrincipal
from tricycle_reaction_db.application.services.authorization import ProjectAccessDeniedError
from tricycle_reaction_db.application.services.invitations import (
    InvitationConflictError,
    InvitationError,
    InvitationNotFoundError,
    InvitationService,
)
from tricycle_reaction_db.application.services.project_management import (
    ProjectManagementConflictError,
    ProjectManagementError,
    ProjectManagementNotFoundError,
    ProjectManagementService,
)
from tricycle_reaction_db.core.config import get_settings

router = APIRouter(prefix="/api/projects", tags=["project management"])
Principal = Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)]


def _management_error(error: Exception) -> HTTPException:
    if isinstance(error, InvitationNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    if isinstance(error, InvitationConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    if isinstance(error, ProjectManagementNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    if isinstance(error, ProjectManagementConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    if isinstance(error, ProjectAccessDeniedError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error))
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(error))


@router.get("", response_model=list[ProjectView])
async def list_projects(
    principal: Principal,
    include_archived: bool = False,
) -> list[ProjectView]:
    return await ProjectManagementService.list_projects(
        principal,
        include_archived=include_archived,
    )


@router.post("", response_model=ProjectView, status_code=status.HTTP_201_CREATED)
async def create_project(payload: ProjectCreate, principal: Principal) -> ProjectView:
    try:
        return await ProjectManagementService.create_project(payload, principal)
    except (ProjectManagementError, ProjectAccessDeniedError) as error:
        raise _management_error(error) from error


@router.get("/{project_id}", response_model=ProjectView)
async def get_project(project_id: UUID, principal: Principal) -> ProjectView:
    try:
        return await ProjectManagementService.get_project(project_id, principal)
    except (ProjectManagementError, ProjectAccessDeniedError) as error:
        raise _management_error(error) from error


@router.patch("/{project_id}", response_model=ProjectView)
async def update_project(
    project_id: UUID,
    payload: ProjectUpdate,
    principal: Principal,
) -> ProjectView:
    try:
        return await ProjectManagementService.update_project(project_id, payload, principal)
    except (ProjectManagementError, ProjectAccessDeniedError) as error:
        raise _management_error(error) from error


@router.get("/{project_id}/members", response_model=list[ProjectMemberView])
async def list_project_members(project_id: UUID, principal: Principal) -> list[ProjectMemberView]:
    try:
        return await ProjectManagementService.list_members(project_id, principal)
    except (ProjectManagementError, ProjectAccessDeniedError) as error:
        raise _management_error(error) from error


@router.post("/{project_id}/members", response_model=ProjectMemberView)
async def add_project_member(
    project_id: UUID,
    payload: ProjectMemberUpsert,
    principal: Principal,
) -> ProjectMemberView:
    try:
        return await ProjectManagementService.upsert_member(project_id, payload, principal)
    except (ProjectManagementError, ProjectAccessDeniedError) as error:
        raise _management_error(error) from error


@router.patch("/{project_id}/members/{user_id}", response_model=ProjectMemberView)
async def update_project_member(
    project_id: UUID,
    user_id: UUID,
    payload: ProjectMemberRoleUpdate,
    principal: Principal,
) -> ProjectMemberView:
    try:
        return await ProjectManagementService.update_member_role(
            project_id,
            user_id,
            payload,
            principal,
        )
    except (ProjectManagementError, ProjectAccessDeniedError) as error:
        raise _management_error(error) from error


@router.delete("/{project_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_project_member(project_id: UUID, user_id: UUID, principal: Principal) -> None:
    try:
        await ProjectManagementService.remove_member(project_id, user_id, principal)
    except (ProjectManagementError, ProjectAccessDeniedError) as error:
        raise _management_error(error) from error


@router.get("/{project_id}/invitations", response_model=list[ProjectInvitationView])
async def list_project_invitations(
    project_id: UUID,
    principal: Principal,
) -> list[ProjectInvitationView]:
    try:
        return await InvitationService.list(project_id, principal)
    except (InvitationError, ProjectAccessDeniedError) as error:
        raise _management_error(error) from error


@router.post(
    "/{project_id}/invitations",
    response_model=ProjectInvitationCreateResult,
    status_code=status.HTTP_201_CREATED,
)
async def create_project_invitation(
    project_id: UUID,
    payload: ProjectInvitationCreate,
    principal: Principal,
) -> ProjectInvitationCreateResult:
    try:
        return await InvitationService.create(
            project_id,
            payload,
            principal,
            frontend_url=get_settings().oidc_frontend_url,
        )
    except (InvitationError, ProjectAccessDeniedError) as error:
        raise _management_error(error) from error


@router.delete(
    "/{project_id}/invitations/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_project_invitation(
    project_id: UUID,
    invitation_id: UUID,
    principal: Principal,
) -> None:
    try:
        await InvitationService.revoke(project_id, invitation_id, principal)
    except (InvitationError, ProjectAccessDeniedError) as error:
        raise _management_error(error) from error


@router.post(
    "/{project_id}/invitations/{invitation_id}/resend",
    response_model=ProjectInvitationCreateResult,
)
async def resend_project_invitation(
    project_id: UUID,
    invitation_id: UUID,
    principal: Principal,
) -> ProjectInvitationCreateResult:
    try:
        return await InvitationService.resend(
            project_id,
            invitation_id,
            principal,
            frontend_url=get_settings().oidc_frontend_url,
        )
    except (InvitationError, ProjectAccessDeniedError) as error:
        raise _management_error(error) from error


@router.get("/{project_id}/audit", response_model=list[AuditEventView])
async def list_project_audit(
    project_id: UUID,
    principal: Principal,
    limit: int = 50,
    offset: int = 0,
) -> list[AuditEventView]:
    try:
        return await AuditService.list_events(
            principal,
            project_id=project_id,
            limit=limit,
            offset=offset,
        )
    except ProjectAccessDeniedError as error:
        raise _management_error(error) from error


__all__ = ["router"]
