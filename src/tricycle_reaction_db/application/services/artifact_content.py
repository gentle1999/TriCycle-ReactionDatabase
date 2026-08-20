"""Controlled preview and download access to immutable RustFS artifacts."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError

from tricycle_reaction_db.application.dtos import ArtifactPreview
from tricycle_reaction_db.application.services.authorization import (
    AuthorizationService,
    ProjectAccessDeniedError,
    ProjectPermission,
)
from tricycle_reaction_db.core.observability import STORAGE_FAILURES
from tricycle_reaction_db.db.models import ArtifactFile
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import ArtifactVisibility, StorageStatus
from tricycle_reaction_db.ingestion.media_type import (
    detect_artifact_media_type,
    is_text_media_type,
)
from tricycle_reaction_db.storage.rustfs import RustFSObjectStore, RustFSSettings


class ArtifactContentError(RuntimeError):
    """Base error for controlled artifact content access."""


class ArtifactNotFoundError(ArtifactContentError):
    pass


class ArtifactUnavailableError(ArtifactContentError):
    pass


class ArtifactPreviewUnsupportedError(ArtifactContentError):
    pass


class ArtifactObjectIntegrityError(ArtifactContentError):
    pass


class ArtifactForbiddenError(ArtifactContentError):
    """Legacy subtype retained for callers; authorization now uses not-found semantics."""

    pass


@dataclass(frozen=True, slots=True)
class ArtifactDownload:
    id: UUID
    original_filename: str
    media_type: str
    size_bytes: int
    content_sha256: str
    bucket: str
    object_key: str
    version_id: str | None


@dataclass(frozen=True, slots=True)
class _ArtifactReference:
    id: UUID
    project_id: UUID
    visibility: ArtifactVisibility
    original_filename: str
    media_type: str
    size_bytes: int
    content_sha256: str
    bucket: str
    object_key: str
    version_id: str | None
    storage_status: StorageStatus


def artifact_preview_available(media_type: str) -> bool:
    return is_text_media_type(media_type)


def _verify_object(reference: _ArtifactReference, store: RustFSObjectStore) -> None:
    metadata = store.head(reference.object_key, version_id=reference.version_id)
    if metadata.size != reference.size_bytes or metadata.sha256 != reference.content_sha256:
        STORAGE_FAILURES.labels(reason="corrupt").inc()
        raise ArtifactObjectIntegrityError(
            f"RustFS metadata does not match ArtifactFile {reference.id}"
        )


def _read_preview(reference: _ArtifactReference, max_bytes: int) -> ArtifactPreview:
    settings = RustFSSettings().model_copy(update={"bucket": reference.bucket})
    with RustFSObjectStore(settings) as store:
        _verify_object(reference, store)
        payload = store.get_range(
            reference.object_key,
            max_bytes=max_bytes,
            version_id=reference.version_id,
        )
    resolved_media_type = detect_artifact_media_type(
        reference.original_filename,
        reference.media_type,
        payload,
    )
    if not is_text_media_type(resolved_media_type):
        raise ArtifactPreviewUnsupportedError(
            f"preview is not available for {reference.media_type}"
        )
    return ArtifactPreview(
        id=reference.id,
        original_filename=reference.original_filename,
        media_type=resolved_media_type,
        size_bytes=reference.size_bytes,
        content_sha256=reference.content_sha256,
        preview_text=payload.decode("utf-8-sig", errors="replace"),
        preview_bytes=len(payload),
        truncated=reference.size_bytes > len(payload),
    )


def iter_artifact_download(download: ArtifactDownload) -> Iterator[bytes]:
    settings = RustFSSettings().model_copy(update={"bucket": download.bucket})
    with RustFSObjectStore(settings) as store:
        yield from store.iter_bytes(download.object_key, version_id=download.version_id)


class ArtifactContentService:
    @staticmethod
    async def _reference(artifact_id: UUID) -> _ArtifactReference:
        async with session_factory() as session:
            artifact = await session.get(ArtifactFile, artifact_id)
        if (
            artifact is None
            or artifact.id is None
            or artifact.storage_status is StorageStatus.RETIRED
        ):
            raise ArtifactNotFoundError("artifact not found")
        status = StorageStatus(artifact.storage_status)
        return _ArtifactReference(
            id=artifact.id,
            project_id=artifact.project_id,
            visibility=ArtifactVisibility(artifact.visibility),
            original_filename=artifact.original_filename,
            media_type=artifact.media_type,
            size_bytes=artifact.size_bytes,
            content_sha256=artifact.content_sha256,
            bucket=artifact.bucket,
            object_key=artifact.object_key,
            version_id=artifact.version_id,
            storage_status=status,
        )

    @staticmethod
    async def _authorize(
        reference: _ArtifactReference,
        user_id: UUID | None,
        permission: ProjectPermission,
    ) -> None:
        if reference.visibility is ArtifactVisibility.PUBLIC:
            return
        if user_id is None:
            raise ArtifactNotFoundError("artifact not found")
        try:
            await AuthorizationService.require_project_permission(
                user_id,
                reference.project_id,
                permission,
            )
        except ProjectAccessDeniedError as error:
            raise ArtifactNotFoundError("artifact not found") from error

    @staticmethod
    def _ensure_available(reference: _ArtifactReference) -> None:
        if reference.storage_status is not StorageStatus.AVAILABLE:
            raise ArtifactUnavailableError(
                f"artifact storage status is {reference.storage_status.value}"
            )

    @classmethod
    async def preview(
        cls,
        artifact_id: UUID,
        *,
        max_bytes: int,
        user_id: UUID | None = None,
    ) -> ArtifactPreview:
        reference = await cls._reference(artifact_id)
        await cls._authorize(reference, user_id, ProjectPermission.ARTIFACT_READ)
        cls._ensure_available(reference)
        try:
            return await asyncio.to_thread(_read_preview, reference, max_bytes)
        except (BotoCoreError, ClientError) as error:
            raise ArtifactUnavailableError("RustFS object is unavailable") from error

    @classmethod
    async def download(
        cls,
        artifact_id: UUID,
        *,
        user_id: UUID | None = None,
    ) -> ArtifactDownload:
        reference = await cls._reference(artifact_id)
        await cls._authorize(reference, user_id, ProjectPermission.ARTIFACT_DOWNLOAD)
        cls._ensure_available(reference)

        def verify() -> None:
            settings = RustFSSettings().model_copy(update={"bucket": reference.bucket})
            with RustFSObjectStore(settings) as store:
                _verify_object(reference, store)

        try:
            await asyncio.to_thread(verify)
        except (BotoCoreError, ClientError) as error:
            raise ArtifactUnavailableError("RustFS object is unavailable") from error
        return ArtifactDownload(
            id=reference.id,
            original_filename=reference.original_filename,
            media_type=reference.media_type,
            size_bytes=reference.size_bytes,
            content_sha256=reference.content_sha256,
            bucket=reference.bucket,
            object_key=reference.object_key,
            version_id=reference.version_id,
        )


__all__ = [
    "ArtifactContentError",
    "ArtifactContentService",
    "ArtifactDownload",
    "ArtifactForbiddenError",
    "ArtifactNotFoundError",
    "ArtifactObjectIntegrityError",
    "ArtifactPreviewUnsupportedError",
    "ArtifactUnavailableError",
    "artifact_preview_available",
    "detect_artifact_media_type",
    "iter_artifact_download",
]
