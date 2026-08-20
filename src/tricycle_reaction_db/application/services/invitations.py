"""Project invitation creation, listing, revocation, and acceptance."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlmodel import col, select

from tricycle_reaction_db.application.dtos import (
    ProjectInvitationCreate,
    ProjectInvitationCreateResult,
    ProjectInvitationView,
)
from tricycle_reaction_db.application.services.audit import AuditService
from tricycle_reaction_db.application.services.authentication import AuthenticatedPrincipal
from tricycle_reaction_db.application.services.authorization import (
    AuthorizationService,
    ProjectPermission,
)
from tricycle_reaction_db.application.services.email import EmailDeliveryService
from tricycle_reaction_db.db.models import (
    Project,
    ProjectInvitation,
    ProjectMembership,
    UserAccount,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import ProjectRole, UserStatus


class InvitationError(RuntimeError):
    pass


class InvitationNotFoundError(InvitationError):
    pass


class InvitationConflictError(InvitationError):
    pass


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _view(invitation: ProjectInvitation) -> ProjectInvitationView:
    if invitation.id is None:
        raise RuntimeError("persisted project invitation is missing its UUID")
    return ProjectInvitationView(
        id=invitation.id,
        project_id=invitation.project_id,
        email=invitation.email,
        role=ProjectRole(invitation.role),
        created_at=invitation.created_at,
        expires_at=invitation.expires_at,
        accepted_at=invitation.accepted_at,
        revoked_at=invitation.revoked_at,
        delivery_status=invitation.delivery_status,
        delivery_error=invitation.delivery_error,
    )


class InvitationService:
    @staticmethod
    async def create(
        project_id: UUID,
        payload: ProjectInvitationCreate,
        principal: AuthenticatedPrincipal,
        *,
        frontend_url: str,
    ) -> ProjectInvitationCreateResult:
        await AuthorizationService.require_project_permission(
            principal.user_id,
            project_id,
            ProjectPermission.PROJECT_MANAGE,
        )
        email = payload.email.strip().lower()
        raw_token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        async with session_factory() as session:
            project = await session.get(Project, project_id)
            if project is None:
                raise InvitationNotFoundError("project not found")
            previous = (
                await session.exec(
                    select(ProjectInvitation).where(
                        ProjectInvitation.project_id == project_id,
                        ProjectInvitation.email == email,
                        col(ProjectInvitation.accepted_at).is_(None),
                        col(ProjectInvitation.revoked_at).is_(None),
                    )
                )
            ).all()
            for item in previous:
                item.revoked_at = now
            invitation = ProjectInvitation(
                project_id=project_id,
                invited_by_user_id=principal.user_id,
                email=email,
                role=payload.role,
                token_hash=_hash_token(raw_token),
                expires_at=now + timedelta(days=payload.expires_in_days),
            )
            session.add(invitation)
            await session.commit()
            await session.refresh(invitation)
        if invitation.id is None:
            raise RuntimeError("database did not assign invitation UUID")
        await AuditService.record(
            action="project.invitation.created",
            entity_type="project_invitation",
            entity_id=invitation.id,
            actor_user_id=principal.user_id,
            project_id=project_id,
            metadata={"email": email, "role": payload.role.value},
        )
        delivery = await EmailDeliveryService.send_project_invitation(
            recipient=email,
            project_name=project.name,
            role=payload.role,
            accept_url=f"{frontend_url.rstrip('/')}/invitations/{raw_token}",
            expires_at=invitation.expires_at,
        )
        if delivery.status == "failed":
            await AuditService.record(
                action="project.invitation.delivery_failed",
                entity_type="project_invitation",
                entity_id=invitation.id,
                actor_user_id=principal.user_id,
                project_id=project_id,
                metadata={"error": delivery.error or "SMTP delivery failed"},
            )
        async with session_factory() as session:
            persisted = await session.get(ProjectInvitation, invitation.id, with_for_update=True)
            if persisted is not None:
                persisted.delivery_status = delivery.status
                persisted.delivery_error = delivery.error
                persisted.delivery_sent_at = (
                    datetime.now(UTC) if delivery.status == "sent" else None
                )
                await session.commit()
                invitation.delivery_status = persisted.delivery_status
                invitation.delivery_error = persisted.delivery_error
        return ProjectInvitationCreateResult(
            invitation=_view(invitation),
            accept_token=raw_token,
            accept_url=f"{frontend_url.rstrip('/')}/invitations/{raw_token}",
            delivery_status=delivery.status,
            delivery_error=delivery.error,
        )

    @staticmethod
    async def resend(
        project_id: UUID,
        invitation_id: UUID,
        principal: AuthenticatedPrincipal,
        *,
        frontend_url: str,
    ) -> ProjectInvitationCreateResult:
        await AuthorizationService.require_project_permission(
            principal.user_id, project_id, ProjectPermission.PROJECT_MANAGE
        )
        raw_token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        async with session_factory() as session:
            invitation = await session.get(ProjectInvitation, invitation_id, with_for_update=True)
            if invitation is None or invitation.project_id != project_id:
                raise InvitationNotFoundError("invitation not found")
            if invitation.accepted_at is not None:
                raise InvitationConflictError("accepted invitation cannot be resent")
            invitation.token_hash = _hash_token(raw_token)
            invitation.expires_at = now + timedelta(days=7)
            invitation.revoked_at = None
            invitation.delivery_status = "pending"
            invitation.delivery_error = None
            await session.commit()
            await session.refresh(invitation)
            project = await session.get(Project, project_id)
            if project is None:
                raise InvitationNotFoundError("project not found")
        accept_url = f"{frontend_url.rstrip('/')}/invitations/{raw_token}"
        delivery = await EmailDeliveryService.send_project_invitation(
            recipient=invitation.email,
            project_name=project.name,
            role=ProjectRole(invitation.role),
            accept_url=accept_url,
            expires_at=invitation.expires_at,
        )
        if delivery.status == "failed":
            await AuditService.record(
                action="project.invitation.delivery_failed",
                entity_type="project_invitation",
                entity_id=invitation_id,
                actor_user_id=principal.user_id,
                project_id=project_id,
                metadata={"error": delivery.error or "SMTP delivery failed"},
            )
        async with session_factory() as session:
            persisted = await session.get(ProjectInvitation, invitation_id, with_for_update=True)
            if persisted is not None:
                persisted.delivery_status = delivery.status
                persisted.delivery_error = delivery.error
                persisted.delivery_sent_at = (
                    datetime.now(UTC) if delivery.status == "sent" else None
                )
                await session.commit()
                invitation.delivery_status = persisted.delivery_status
                invitation.delivery_error = persisted.delivery_error
        return ProjectInvitationCreateResult(
            invitation=_view(invitation),
            accept_token=raw_token,
            accept_url=accept_url,
            delivery_status=delivery.status,
            delivery_error=delivery.error,
        )

    @staticmethod
    async def list(
        project_id: UUID,
        principal: AuthenticatedPrincipal,
    ) -> list[ProjectInvitationView]:
        await AuthorizationService.require_project_permission(
            principal.user_id,
            project_id,
            ProjectPermission.PROJECT_MANAGE,
        )
        async with session_factory() as session:
            invitations = (
                await session.exec(
                    select(ProjectInvitation)
                    .where(ProjectInvitation.project_id == project_id)
                    .order_by(col(ProjectInvitation.created_at).desc())
                )
            ).all()
        return [_view(item) for item in invitations]

    @staticmethod
    async def revoke(
        project_id: UUID,
        invitation_id: UUID,
        principal: AuthenticatedPrincipal,
    ) -> None:
        await AuthorizationService.require_project_permission(
            principal.user_id,
            project_id,
            ProjectPermission.PROJECT_MANAGE,
        )
        async with session_factory() as session:
            invitation = await session.get(ProjectInvitation, invitation_id, with_for_update=True)
            if invitation is None or invitation.project_id != project_id:
                raise InvitationNotFoundError("invitation not found")
            if invitation.accepted_at is not None:
                raise InvitationConflictError("accepted invitation cannot be revoked")
            invitation.revoked_at = datetime.now(UTC)
            await session.commit()
        await AuditService.record(
            action="project.invitation.revoked",
            entity_type="project_invitation",
            entity_id=invitation_id,
            actor_user_id=principal.user_id,
            project_id=project_id,
        )

    @staticmethod
    async def accept(token: str, principal: AuthenticatedPrincipal) -> ProjectInvitationView:
        now = datetime.now(UTC)
        async with session_factory() as session:
            invitation = (
                await session.exec(
                    select(ProjectInvitation)
                    .where(
                        ProjectInvitation.token_hash == _hash_token(token),
                        col(ProjectInvitation.accepted_at).is_(None),
                        col(ProjectInvitation.revoked_at).is_(None),
                        ProjectInvitation.expires_at > now,
                    )
                    .with_for_update()
                )
            ).first()
            if invitation is None:
                raise InvitationNotFoundError("invitation is invalid or expired")
            if (
                principal.primary_email is None
                or principal.primary_email.strip().lower() != invitation.email
            ):
                raise InvitationConflictError("authenticated email does not match invitation")
            user = await session.get(UserAccount, principal.user_id)
            if user is None or UserStatus(user.status) is not UserStatus.ACTIVE:
                raise InvitationNotFoundError("user account is unavailable")
            membership = (
                await session.exec(
                    select(ProjectMembership).where(
                        ProjectMembership.project_id == invitation.project_id,
                        ProjectMembership.user_id == principal.user_id,
                    )
                )
            ).first()
            if membership is None:
                session.add(
                    ProjectMembership(
                        project_id=invitation.project_id,
                        user_id=principal.user_id,
                        role=invitation.role,
                    )
                )
            invitation.accepted_at = now
            await session.commit()
            result = _view(invitation)
        await AuditService.record(
            action="project.invitation.accepted",
            entity_type="project_invitation",
            entity_id=invitation.id,
            actor_user_id=principal.user_id,
            project_id=invitation.project_id,
            metadata={"role": invitation.role.value},
        )
        return result


__all__ = [
    "InvitationConflictError",
    "InvitationError",
    "InvitationNotFoundError",
    "InvitationService",
]
