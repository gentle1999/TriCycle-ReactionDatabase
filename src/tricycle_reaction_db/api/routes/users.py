"""Basic local user directory and account-status administration."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from tricycle_reaction_db.api.authentication import get_authenticated_principal
from tricycle_reaction_db.application.dtos import UserPage, UserStatusUpdate, UserSummaryView
from tricycle_reaction_db.application.services.authentication import AuthenticatedPrincipal
from tricycle_reaction_db.application.services.authorization import ProjectAccessDeniedError
from tricycle_reaction_db.application.services.user_management import (
    UserManagementConflictError,
    UserManagementError,
    UserManagementNotFoundError,
    UserManagementService,
)
from tricycle_reaction_db.domain.enums import UserStatus

router = APIRouter(prefix="/api/users", tags=["user management"])
Principal = Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)]
DirectoryLimit = Annotated[int, Query(ge=1, le=200)]
DirectoryOffset = Annotated[int, Query(ge=0)]


def _management_error(error: Exception) -> HTTPException:
    if isinstance(error, UserManagementNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    if isinstance(error, UserManagementConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    if isinstance(error, ProjectAccessDeniedError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error))
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(error))


@router.get("", response_model=UserPage)
async def list_users(
    principal: Principal,
    q: str | None = None,
    user_status: UserStatus | None = None,
    project_id: UUID | None = None,
    limit: DirectoryLimit = 50,
    offset: DirectoryOffset = 0,
) -> UserPage:
    try:
        return await UserManagementService.list_users(
            principal,
            query=q,
            status=user_status,
            project_id=project_id,
            limit=limit,
            offset=offset,
        )
    except (UserManagementError, ProjectAccessDeniedError) as error:
        raise _management_error(error) from error


@router.get("/{user_id}", response_model=UserSummaryView)
async def get_user(user_id: UUID, principal: Principal) -> UserSummaryView:
    try:
        return await UserManagementService.get_user(user_id, principal)
    except UserManagementError as error:
        raise _management_error(error) from error


@router.patch("/{user_id}/status", response_model=UserSummaryView)
async def update_user_status(
    user_id: UUID,
    payload: UserStatusUpdate,
    principal: Principal,
) -> UserSummaryView:
    try:
        return await UserManagementService.update_status(user_id, payload, principal)
    except UserManagementError as error:
        raise _management_error(error) from error


__all__ = ["router"]
