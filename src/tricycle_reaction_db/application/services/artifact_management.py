"""Administrative lifecycle operations for immutable artifact catalogue entries."""

from __future__ import annotations

import asyncio
import re
from typing import cast
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from tricycle_reaction_db.application.dtos import ArtifactMetadataUpdate, ArtifactSummary
from tricycle_reaction_db.application.services._persistence import _acquire_identity_locks
from tricycle_reaction_db.application.services.artifact_content import artifact_preview_available
from tricycle_reaction_db.application.services.authorization import (
    AuthorizationService,
    ProjectPermission,
)
from tricycle_reaction_db.db.models import ArtifactFile
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import StorageStatus
from tricycle_reaction_db.storage.rustfs import RustFSObjectStore, RustFSSettings


class ArtifactRemovalError(RuntimeError):
    """Base error for retiring an ArtifactFile."""


class ArtifactRemovalNotFoundError(ArtifactRemovalError):
    pass


class ArtifactRemovalIntegrityError(ArtifactRemovalError):
    pass


class ArtifactRemovalUnavailableError(ArtifactRemovalError):
    pass


class ArtifactMetadataConflictError(ArtifactRemovalError):
    pass


def _artifact_summary(artifact: ArtifactFile) -> ArtifactSummary:
    if artifact.id is None:
        raise RuntimeError("persisted artifact is missing its UUID")
    ingestion = artifact.ingestion
    return ArtifactSummary(
        id=artifact.id,
        project_id=artifact.project_id,
        created_by_user_id=artifact.created_by_user_id,
        visibility=artifact.visibility.value,
        original_filename=artifact.original_filename,
        content_sha256=artifact.content_sha256,
        size_bytes=artifact.size_bytes,
        media_type=artifact.media_type,
        artifact_kind=artifact.artifact_kind.value,
        storage_status=artifact.storage_status.value,
        storage_verified_at=artifact.storage_verified_at,
        preview_available=artifact_preview_available(artifact.media_type),
        ingestion_status=ingestion.status.value if ingestion is not None else None,
        source_frame_count=ingestion.source_frame_count if ingestion is not None else None,
        transition_state_frame_count=(
            ingestion.transition_state_frame_count if ingestion is not None else None
        ),
        ingestion_error_code=ingestion.error_code if ingestion is not None else None,
        ingestion_error_message=ingestion.error_message if ingestion is not None else None,
    )


def _delete_artifact_object(
    *,
    bucket: str,
    object_key: str,
    content_sha256: str,
    size_bytes: int,
    version_id: str | None,
) -> None:
    settings = RustFSSettings().model_copy(update={"bucket": bucket})
    with RustFSObjectStore(settings) as store:
        if not store.exists(object_key, version_id=version_id):
            return
        metadata = store.head(object_key, version_id=version_id)
        if metadata.size != size_bytes or metadata.sha256 != content_sha256:
            raise ArtifactRemovalIntegrityError(
                "stored object metadata no longer matches the artifact identity"
            )
        store.delete(object_key, version_id=version_id)


class ArtifactManagementService:
    @staticmethod
    async def update_metadata(
        artifact_id: UUID,
        payload: ArtifactMetadataUpdate,
        *,
        user_id: UUID,
    ) -> ArtifactSummary:
        if payload.original_filename is None and payload.visibility is None:
            raise ArtifactMetadataConflictError("at least one artifact field must be supplied")
        filename = (
            payload.original_filename.strip() if payload.original_filename is not None else None
        )
        if filename is not None and (not filename or re.search(r"[/\\]", filename)):
            raise ArtifactMetadataConflictError("artifact filename must be a plain filename")

        async with session_factory() as session:
            artifact = (
                await session.exec(
                    select(ArtifactFile)
                    .options(selectinload(ArtifactFile.ingestion))
                    .where(ArtifactFile.id == artifact_id)
                    .with_for_update()
                )
            ).one_or_none()
            if artifact is None or artifact.storage_status is StorageStatus.RETIRED:
                raise ArtifactRemovalNotFoundError("artifact not found")
            await AuthorizationService.require_project_permission(
                user_id,
                artifact.project_id,
                ProjectPermission.ARTIFACT_MANAGE,
            )
            if filename is not None:
                artifact.original_filename = filename
            if payload.visibility is not None:
                artifact.visibility = payload.visibility
            session.add(artifact)
            await session.commit()
            await session.refresh(artifact)
            return _artifact_summary(artifact)

    @staticmethod
    async def retire(artifact_id: UUID, *, user_id: UUID) -> None:
        """Retire catalogue visibility, then remove the corresponding RustFS object.

        The tombstone remains in PostgreSQL so scientific provenance keeps a stable
        artifact identity. A retry is idempotent and completes object cleanup when a
        previous RustFS request failed after the tombstone was committed.
        """

        async with session_factory() as session:
            artifact = await session.get(ArtifactFile, artifact_id)
            if artifact is None:
                raise ArtifactRemovalNotFoundError("artifact not found")
            await AuthorizationService.require_project_permission(
                user_id,
                artifact.project_id,
                ProjectPermission.ARTIFACT_DELETE,
            )
            await session.run_sync(
                lambda sync_session: _acquire_identity_locks(
                    cast(Session, sync_session),
                    ("artifact-content", artifact.content_sha256),
                )
            )
            await session.refresh(artifact)
            artifact.storage_status = StorageStatus.RETIRED
            session.add(artifact)
            await session.commit()
            bucket = artifact.bucket
            object_key = artifact.object_key
            content_sha256 = artifact.content_sha256
            size_bytes = artifact.size_bytes
            version_id = artifact.version_id

        try:
            async with session_factory() as session:
                await session.run_sync(
                    lambda sync_session: _acquire_identity_locks(
                        cast(Session, sync_session),
                        ("artifact-content", content_sha256),
                    )
                )
                current = await session.get(ArtifactFile, artifact_id)
                if current is not None and (
                    current.object_key == object_key
                    and current.storage_status is not StorageStatus.RETIRED
                ):
                    await session.commit()
                    return
                shared_reference = (
                    await session.exec(
                        select(ArtifactFile.id).where(
                            ArtifactFile.id != artifact_id,
                            ArtifactFile.bucket == bucket,
                            ArtifactFile.object_key == object_key,
                            ArtifactFile.storage_status != StorageStatus.RETIRED,
                        )
                    )
                ).first()
                if shared_reference is not None:
                    await session.commit()
                    return
                await asyncio.to_thread(
                    _delete_artifact_object,
                    bucket=bucket,
                    object_key=object_key,
                    content_sha256=content_sha256,
                    size_bytes=size_bytes,
                    version_id=version_id,
                )
                await session.commit()
        except ArtifactRemovalIntegrityError:
            raise
        except (BotoCoreError, ClientError) as error:
            raise ArtifactRemovalUnavailableError(
                "artifact was retired, but RustFS cleanup is still pending"
            ) from error


__all__ = [
    "ArtifactManagementService",
    "ArtifactMetadataConflictError",
    "ArtifactRemovalError",
    "ArtifactRemovalIntegrityError",
    "ArtifactRemovalNotFoundError",
    "ArtifactRemovalUnavailableError",
]
