"""Authenticated organization-access routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from tricycle_reaction_db.api.authentication import get_authenticated_principal
from tricycle_reaction_db.application.dtos import OrganizationAccessView, OrganizationCreate
from tricycle_reaction_db.application.services.authentication import AuthenticatedPrincipal
from tricycle_reaction_db.application.services.authorization import AuthorizationService
from tricycle_reaction_db.application.services.organization_management import (
    OrganizationManagementConflictError,
    OrganizationManagementService,
)

router = APIRouter(prefix="/api/organizations", tags=["organizations"])
Principal = Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)]


@router.get("", response_model=list[OrganizationAccessView])
async def list_organizations(principal: Principal) -> list[OrganizationAccessView]:
    return await AuthorizationService.organization_accesses(principal.user_id)


@router.post("", response_model=OrganizationAccessView, status_code=status.HTTP_201_CREATED)
async def create_organization(
    payload: OrganizationCreate,
    principal: Principal,
) -> OrganizationAccessView:
    try:
        return await OrganizationManagementService.create_organization(payload, principal)
    except OrganizationManagementConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


__all__ = ["router"]
