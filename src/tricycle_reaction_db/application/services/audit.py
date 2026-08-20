"""Append-only audit event recording and project-scoped audit reads."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlmodel import col, select

from tricycle_reaction_db.application.dtos import AuditEventView
from tricycle_reaction_db.application.services.authentication import AuthenticatedPrincipal
from tricycle_reaction_db.application.services.authorization import (
    AuthorizationService,
    ProjectPermission,
)
from tricycle_reaction_db.db.models import AuditEvent
from tricycle_reaction_db.db.session import session_factory


class AuditService:
    @staticmethod
    async def record(
        *,
        action: str,
        entity_type: str,
        entity_id: UUID | None = None,
        actor_user_id: UUID | None = None,
        project_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditEventView:
        async with session_factory() as session:
            event = AuditEvent(
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                actor_user_id=actor_user_id,
                project_id=project_id,
                metadata_json=metadata or {},
            )
            session.add(event)
            await session.commit()
            await session.refresh(event)
        return AuditService._view(event)

    @staticmethod
    def _view(event: AuditEvent) -> AuditEventView:
        if event.id is None:
            raise RuntimeError("persisted audit event is missing its UUID")
        return AuditEventView(
            id=event.id,
            created_at=event.created_at,
            actor_user_id=event.actor_user_id,
            project_id=event.project_id,
            action=event.action,
            entity_type=event.entity_type,
            entity_id=event.entity_id,
            metadata_json=event.metadata_json,
        )

    @classmethod
    async def list_events(
        cls,
        principal: AuthenticatedPrincipal,
        *,
        project_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AuditEventView]:
        if project_id is not None:
            await AuthorizationService.require_project_permission(
                principal.user_id,
                project_id,
                ProjectPermission.PROJECT_MANAGE,
            )
        async with session_factory() as session:
            statement = select(AuditEvent)
            if project_id is not None:
                statement = statement.where(col(AuditEvent.project_id) == project_id)
            else:
                statement = statement.where(col(AuditEvent.actor_user_id) == principal.user_id)
            events = (
                await session.exec(
                    statement.order_by(col(AuditEvent.created_at).desc(), col(AuditEvent.id).desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
        return [cls._view(event) for event in events]


__all__ = ["AuditService"]
