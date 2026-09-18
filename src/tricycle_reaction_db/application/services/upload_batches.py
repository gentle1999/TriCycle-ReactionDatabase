"""Durable queue coordination for independently uploaded artifact files."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import and_, case, cast, func, or_, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from tricycle_reaction_db.application.dtos import (
    UploadBatchCreate,
    UploadBatchFileCreate,
    UploadBatchItemPage,
    UploadBatchItemView,
    UploadBatchPage,
    UploadBatchStatusUpdate,
    UploadBatchView,
)
from tricycle_reaction_db.application.services.artifact_uploads import (
    ArtifactUploadPayload,
    ArtifactUploadService,
)
from tricycle_reaction_db.application.services.authorization import (
    AuthorizationService,
    ProjectPermission,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    ArtifactIngestion,
    UploadBatch,
    UploadBatchItem,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactKind,
    ImportMaterializationStatus,
    ImportParseStatus,
    ImportSelectionStatus,
    StorageStatus,
    UploadBatchItemStatus,
    UploadBatchStatus,
)
from tricycle_reaction_db.ingestion.media_type import detect_artifact_media_type


class UploadBatchError(RuntimeError):
    pass


class UploadBatchNotFoundError(UploadBatchError):
    pass


class UploadBatchConflictError(UploadBatchError):
    pass


class UploadBatchLimitError(UploadBatchError):
    pass


UPLOAD_PROGRESS_METADATA_KEY = "__tricycle_upload_progress"
_stage_slots: tuple[asyncio.AbstractEventLoop, int, asyncio.Semaphore] | None = None


def _shared_stage_slots() -> asyncio.Semaphore:
    """Bound RustFS staging across all upload sessions in this event loop."""

    global _stage_slots
    loop = asyncio.get_running_loop()
    workers = max(1, get_settings().upload_max_concurrency)
    if _stage_slots is None or _stage_slots[0] is not loop or _stage_slots[1] != workers:
        _stage_slots = (loop, workers, asyncio.Semaphore(workers))
    return _stage_slots[2]


def _with_upload_progress(
    metadata: dict[str, object],
    *,
    phase: str,
    completed: int | None = None,
    total: int | None = None,
) -> dict[str, object]:
    current = metadata.get(UPLOAD_PROGRESS_METADATA_KEY)
    current_progress = current if isinstance(current, dict) else {}
    resolved_total = total if total is not None else current_progress.get("total", 0)
    resolved_completed = (
        completed
        if completed is not None
        else resolved_total
        if phase in {"completed", "failed"}
        else current_progress.get("completed", 0)
    )
    return {
        **metadata,
        UPLOAD_PROGRESS_METADATA_KEY: {
            "phase": phase,
            "completed": int(resolved_completed),
            "total": int(resolved_total),
        },
    }


@dataclass(frozen=True, slots=True)
class UploadProcessingJob:
    """A leased, server-owned parse task detached from the browser request."""

    project_id: UUID
    batch_id: UUID
    item_id: UUID
    client_file_id: UUID
    artifact_file_id: UUID
    user_id: UUID
    lease_id: UUID
    lease_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PendingIngestionJob:
    """A lease for a calculation ingestion left outside the web queue."""

    project_id: UUID
    ingestion_id: UUID
    artifact_file_id: UUID
    user_id: UUID
    lease_id: UUID
    lease_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StagedUploadSubmission:
    """The durable queue records created by a transport upload request."""

    batch: UploadBatchView
    items: tuple[UploadBatchItemView, ...]


def _required_uuid(value: UUID | None, label: str) -> UUID:
    if value is None:
        raise RuntimeError(f"persisted {label} is missing its UUID")
    return value


def _required_datetime(value: datetime | None, label: str) -> datetime:
    if value is None:
        raise RuntimeError(f"persisted {label} is missing its timestamp")
    return value


def _upload_media_type(upload: ArtifactUploadPayload, filename: str, declared: str) -> str:
    """Resolve the queue MIME from the uploaded bytes, not browser metadata."""

    if upload.payload is not None:
        sample = upload.payload[: 64 * 1024]
    elif upload.spool_path is not None:
        with upload.spool_path.open("rb") as stream:
            sample = stream.read(64 * 1024)
    else:
        raise UploadBatchConflictError("uploaded artifact has no payload")
    return detect_artifact_media_type(filename, declared, sample)


def _batch_view(batch: UploadBatch) -> UploadBatchView:
    return UploadBatchView(
        id=_required_uuid(batch.id, "UploadBatch"),
        created_at=_required_datetime(batch.created_at, "UploadBatch.created_at"),
        updated_at=_required_datetime(batch.updated_at, "UploadBatch.updated_at"),
        project_id=batch.project_id,
        created_by_user_id=batch.created_by_user_id,
        artifact_kind=batch.artifact_kind,
        status=batch.status,
        shared_metadata=batch.shared_metadata,
        archive_sha256=batch.archive_sha256,
        manifest_sha256=batch.manifest_sha256,
        manifest_schema_version=batch.manifest_schema_version,
        total_count=batch.total_count,
        total_bytes=batch.total_bytes,
        succeeded_count=batch.succeeded_count,
        failed_count=batch.failed_count,
        cancelled_count=batch.cancelled_count,
        uploading_count=batch.uploading_count,
        staged_count=batch.staged_count,
        processing_count=batch.processing_count,
    )


def _item_view(
    item: UploadBatchItem,
    ingestion: ArtifactIngestion | None = None,
    *,
    ingestion_status: ArtifactIngestionStatus | None = None,
    ingestion_error_message: str | None = None,
) -> UploadBatchItemView:
    return UploadBatchItemView(
        id=_required_uuid(item.id, "UploadBatchItem"),
        batch_id=item.batch_id,
        created_at=_required_datetime(item.created_at, "UploadBatchItem.created_at"),
        updated_at=_required_datetime(item.updated_at, "UploadBatchItem.updated_at"),
        client_file_id=item.client_file_id,
        position=item.position,
        original_filename=item.original_filename,
        relative_path=item.relative_path,
        size_bytes=item.size_bytes,
        media_type=item.media_type,
        status=item.status,
        attempt_count=item.attempt_count,
        processing_attempt_count=item.processing_attempt_count,
        content_sha256=item.content_sha256,
        expected_file_sha256=item.expected_file_sha256,
        is_gaussian_log=item.is_gaussian_log,
        selection_status=ImportSelectionStatus(item.selection_status),
        parse_status=ImportParseStatus(item.parse_status),
        materialization_status=ImportMaterializationStatus(item.materialization_status),
        parse_revision_id=item.parse_revision_id,
        artifact_file_id=item.artifact_file_id,
        ingestion_id=ingestion.id if ingestion is not None else None,
        ingestion_status=ingestion.status if ingestion is not None else ingestion_status,
        ingestion_error_message=(
            ingestion.error_message if ingestion is not None else ingestion_error_message
        ),
        error_code=item.error_code,
        error_message=item.error_message,
        metadata=item.metadata_json,
    )


def _mark_ingestion_processing(
    ingestion: ArtifactIngestion,
    *,
    started_at: datetime,
    lease_id: UUID,
    lease_expires_at: datetime,
) -> None:
    """Publish that the worker has taken the RustFS object into MolOP."""

    ingestion.status = ArtifactIngestionStatus.PROCESSING
    ingestion.started_at = started_at
    ingestion.completed_at = None
    ingestion.processing_attempt_count += 1
    ingestion.worker_lease_id = lease_id
    ingestion.worker_lease_expires_at = lease_expires_at
    ingestion.error_code = None
    ingestion.error_message = None


def _reset_ingestion_to_pending(ingestion: ArtifactIngestion) -> None:
    """Return an expired parser lease to the durable waiting queue."""

    ingestion.status = ArtifactIngestionStatus.PENDING
    ingestion.started_at = None
    ingestion.completed_at = None
    ingestion.worker_lease_id = None
    ingestion.worker_lease_expires_at = None


def _queue_ingestion_for_reparse(
    ingestion: ArtifactIngestion,
    *,
    queued_at: datetime,
) -> None:
    """Publish a clean pending state as soon as a reparse is accepted."""

    ingestion.status = ArtifactIngestionStatus.PENDING
    ingestion.source_frame_count = None
    ingestion.transition_state_frame_count = None
    ingestion.started_at = None
    ingestion.completed_at = None
    ingestion.worker_lease_id = None
    ingestion.worker_lease_expires_at = None
    ingestion.error_code = None
    ingestion.error_message = None
    ingestion.parser_metadata = {
        "reparse_queued": True,
        "queued_at": queued_at.isoformat(),
    }


async def _owned_batch(
    session: AsyncSession,
    batch_id: UUID,
    user_id: UUID,
    *,
    project_id: UUID | None = None,
    lock: bool = False,
) -> UploadBatch:
    statement = select(UploadBatch).where(
        col(UploadBatch.id) == batch_id,
        col(UploadBatch.created_by_user_id) == user_id,
    )
    if project_id is not None:
        statement = statement.where(col(UploadBatch.project_id) == project_id)
    if lock:
        statement = statement.with_for_update()
    batch = (await session.exec(statement)).one_or_none()
    if batch is None:
        raise UploadBatchNotFoundError("upload batch not found")
    return batch


async def _batch_item(
    session: AsyncSession,
    batch_id: UUID,
    client_file_id: UUID,
    *,
    lock: bool = False,
) -> UploadBatchItem:
    statement = select(UploadBatchItem).where(
        col(UploadBatchItem.batch_id) == batch_id,
        col(UploadBatchItem.client_file_id) == client_file_id,
    )
    if lock:
        statement = statement.with_for_update()
    item = (await session.exec(statement)).one_or_none()
    if item is None:
        raise UploadBatchNotFoundError("upload batch file not found")
    return item


def _finish_batch_if_terminal(batch: UploadBatch) -> None:
    terminal = batch.succeeded_count + batch.failed_count + batch.cancelled_count
    if (
        batch.status is not UploadBatchStatus.CANCELLED
        and batch.uploading_count == 0
        and batch.staged_count == 0
        and batch.processing_count == 0
        and terminal == batch.total_count
    ):
        batch.status = UploadBatchStatus.COMPLETED


async def _refresh_manifest_paths(
    session: AsyncSession,
    batch: UploadBatch,
    files: list[UploadBatchFileCreate],
    *,
    user_id: UUID,
    now: datetime,
) -> None:
    """Refresh pending manifest paths when an operator relocates staging.

    Only the manifest owner may update paths, and terminal raw/parse results
    keep the path that was actually used for their completed attempt.
    """

    if batch.created_by_user_id != user_id:
        return
    existing_items = (
        await session.exec(select(UploadBatchItem).where(col(UploadBatchItem.batch_id) == batch.id))
    ).all()
    files_by_client_id = {file.client_file_id: file for file in files}
    updated = False
    for item in existing_items:
        incoming = files_by_client_id.get(item.client_file_id)
        if (
            incoming is not None
            and item.status in {UploadBatchItemStatus.QUEUED, UploadBatchItemStatus.FAILED}
            and incoming.staged_file_path is not None
            and item.staged_file_path != incoming.staged_file_path
        ):
            item.staged_file_path = incoming.staged_file_path
            item.updated_at = now
            session.add(item)
            updated = True
    if updated:
        batch.updated_at = now
        session.add(batch)
        await session.commit()
        await session.refresh(batch)


class UploadBatchService:
    """Manage a server-visible queue without combining file bodies into one request."""

    @staticmethod
    async def create(
        payload: UploadBatchCreate,
        *,
        user_id: UUID,
        manifest_registration: bool = False,
        allow_transport_rejections: bool = False,
    ) -> UploadBatchView:
        settings = get_settings()
        if not manifest_registration and (
            payload.archive_sha256 is not None
            or payload.manifest_sha256 is not None
            or payload.manifest_schema_version is not None
            or any(
                file.expected_file_sha256 is not None
                or file.staged_file_path is not None
                or file.is_gaussian_log
                or file.selection_status is not ImportSelectionStatus.SELECTED
                for file in payload.files
            )
        ):
            raise UploadBatchConflictError(
                "manifest metadata must be registered through the import-job control plane"
            )
        if len(payload.files) > settings.max_upload_queue_files:
            raise UploadBatchLimitError(
                f"upload queue exceeds the {settings.max_upload_queue_files}-file limit"
            )
        oversized = next(
            (item for item in payload.files if item.size_bytes > settings.max_upload_bytes),
            None,
        )
        if oversized is not None and not allow_transport_rejections:
            raise UploadBatchLimitError(
                f"{oversized.original_filename} exceeds the {settings.max_upload_bytes}-byte limit"
            )
        total_bytes = sum(item.size_bytes for item in payload.files)
        if total_bytes > settings.max_upload_queue_bytes:
            raise UploadBatchLimitError(
                f"upload queue exceeds the {settings.max_upload_queue_bytes}-byte limit"
            )
        try:
            metadata_bytes = len(
                json.dumps(
                    payload.shared_metadata,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except (TypeError, ValueError) as error:
            raise UploadBatchConflictError("shared metadata must be JSON serializable") from error
        if metadata_bytes > settings.max_upload_metadata_bytes:
            raise UploadBatchLimitError(
                f"shared metadata exceeds the {settings.max_upload_metadata_bytes}-byte limit"
            )

        await AuthorizationService.require_project_permission(
            user_id,
            payload.project_id,
            ProjectPermission.ARTIFACT_UPLOAD,
        )
        now = datetime.now(UTC)
        async with session_factory() as session:
            if payload.manifest_sha256 is not None:
                existing = (
                    await session.exec(
                        select(UploadBatch).where(
                            col(UploadBatch.project_id) == payload.project_id,
                            col(UploadBatch.manifest_sha256) == payload.manifest_sha256,
                        )
                    )
                ).first()
                if existing is not None:
                    if existing.artifact_kind is not payload.artifact_kind:
                        raise UploadBatchConflictError(
                            "manifest is already registered with a different artifact kind"
                        )
                    await _refresh_manifest_paths(
                        session,
                        existing,
                        payload.files,
                        user_id=user_id,
                        now=now,
                    )
                    return _batch_view(existing)
            selected_count = sum(
                file.selection_status is ImportSelectionStatus.SELECTED for file in payload.files
            )
            batch = UploadBatch(
                project_id=payload.project_id,
                created_by_user_id=user_id,
                artifact_kind=payload.artifact_kind,
                status=(
                    UploadBatchStatus.ACTIVE if selected_count else UploadBatchStatus.COMPLETED
                ),
                shared_metadata=payload.shared_metadata,
                archive_sha256=payload.archive_sha256,
                manifest_sha256=payload.manifest_sha256,
                manifest_schema_version=payload.manifest_schema_version,
                total_count=len(payload.files),
                total_bytes=total_bytes,
                cancelled_count=len(payload.files) - selected_count,
                updated_at=now,
            )
            session.add(batch)
            await session.flush()
            batch_id = _required_uuid(batch.id, "UploadBatch")
            session.add_all(
                [
                    UploadBatchItem(
                        batch_id=batch_id,
                        client_file_id=file.client_file_id,
                        position=position,
                        original_filename=file.original_filename,
                        relative_path=file.relative_path,
                        size_bytes=file.size_bytes,
                        media_type=file.media_type,
                        expected_file_sha256=file.expected_file_sha256,
                        staged_file_path=file.staged_file_path,
                        is_gaussian_log=file.is_gaussian_log,
                        selection_status=file.selection_status.value,
                        parse_status=(
                            ImportParseStatus.FILTERED.value
                            if file.selection_status is not ImportSelectionStatus.SELECTED
                            else ImportParseStatus.NOT_STARTED.value
                        ),
                        materialization_status=ImportMaterializationStatus.NOT_STARTED.value,
                        status=(
                            UploadBatchItemStatus.QUEUED
                            if file.selection_status is ImportSelectionStatus.SELECTED
                            else UploadBatchItemStatus.CANCELLED
                        ),
                        error_code=(
                            "manifest_not_selected"
                            if file.selection_status is not ImportSelectionStatus.SELECTED
                            else None
                        ),
                        error_message=(
                            "manifest entry is explicitly excluded from import"
                            if file.selection_status is not ImportSelectionStatus.SELECTED
                            else None
                        ),
                        metadata_json=payload.shared_metadata,
                        updated_at=now,
                    )
                    for position, file in enumerate(payload.files)
                ]
            )
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                if payload.manifest_sha256 is not None:
                    existing = (
                        await session.exec(
                            select(UploadBatch).where(
                                col(UploadBatch.project_id) == payload.project_id,
                                col(UploadBatch.manifest_sha256) == payload.manifest_sha256,
                            )
                        )
                    ).first()
                    if existing is not None:
                        if existing.artifact_kind is not payload.artifact_kind:
                            raise UploadBatchConflictError(
                                "manifest is already registered with a different artifact kind"
                            ) from error
                        await _refresh_manifest_paths(
                            session,
                            existing,
                            payload.files,
                            user_id=user_id,
                            now=now,
                        )
                        return _batch_view(existing)
                raise UploadBatchConflictError("upload batch identity already exists") from error
            await session.refresh(batch)
            return _batch_view(batch)

    @classmethod
    async def create_and_stage(
        cls,
        *,
        files: list[ArtifactUploadPayload],
        artifact_kind: ArtifactKind,
        project_id: UUID,
        user_id: UUID,
        shared_metadata: dict[str, object] | None = None,
    ) -> StagedUploadSubmission:
        """Create an implicit durable batch for a transport upload.

        The legacy artifact upload endpoints do not expose client-side batch
        manifests, but they must still use exactly the same staging and worker
        hand-off as the explicit upload queue.  This adapter gives those
        endpoints a stable batch/item identity without duplicating the queue
        state machine.
        """

        if not files:
            raise UploadBatchConflictError("upload request contains no files")

        client_file_ids = [uuid4() for _ in files]
        descriptors: list[UploadBatchFileCreate] = []
        payloads: list[tuple[UUID, ArtifactUploadPayload]] = []
        seen_paths: set[str] = set()
        for index, upload in enumerate(files):
            size_bytes = (
                upload.declared_size_bytes
                if upload.declared_size_bytes is not None
                else cls._payload_size(upload)
            )
            if size_bytes < 0:
                raise UploadBatchConflictError("uploaded artifact has no payload")
            filename = Path(upload.filename).name
            if not filename:
                raise UploadBatchConflictError("uploaded artifact requires a filename")
            relative_path = upload.relative_path or filename
            if relative_path in seen_paths:
                relative_path = f"{relative_path}.__direct_{index}"
            seen_paths.add(relative_path)
            descriptors.append(
                UploadBatchFileCreate(
                    client_file_id=client_file_ids[index],
                    original_filename=filename,
                    relative_path=relative_path,
                    size_bytes=size_bytes,
                    media_type=upload.media_type,
                )
            )
            payloads.append(
                (
                    client_file_ids[index],
                    ArtifactUploadPayload(
                        filename=filename,
                        media_type=upload.media_type,
                        payload=upload.payload,
                        spool_path=upload.spool_path,
                        error_code=upload.error_code,
                        error_message=upload.error_message,
                        declared_size_bytes=upload.declared_size_bytes,
                        relative_path=relative_path,
                        expected_sha256=upload.expected_sha256,
                        expected_size_bytes=upload.expected_size_bytes,
                    ),
                )
            )

        batch = await cls.create(
            UploadBatchCreate(
                project_id=project_id,
                artifact_kind=artifact_kind,
                shared_metadata=shared_metadata or {},
                files=descriptors,
            ),
            user_id=user_id,
            allow_transport_rejections=True,
        )
        items = await cls.upload_items(batch.id, files=payloads, user_id=user_id)
        refreshed = await cls.get(batch.id, user_id=user_id, project_id=project_id)
        return StagedUploadSubmission(batch=refreshed, items=tuple(items))

    @classmethod
    async def enqueue_reparse(
        cls,
        artifact_id: UUID,
        *,
        user_id: UUID,
    ) -> StagedUploadSubmission:
        """Put an existing calculation artifact on the shared parse queue."""

        async with session_factory() as session:
            artifact = await session.get(ArtifactFile, artifact_id)
            if artifact is None:
                raise UploadBatchNotFoundError("artifact not found")
            await AuthorizationService.require_project_permission(
                user_id,
                artifact.project_id,
                ProjectPermission.ARTIFACT_UPLOAD,
            )
            if artifact.artifact_kind is not ArtifactKind.CALCULATION_OUTPUT:
                raise UploadBatchConflictError("only calculation output artifacts can be reparsed")
            if artifact.storage_status is not StorageStatus.AVAILABLE:
                raise UploadBatchConflictError("artifact bytes are not available for reparse")

            now = datetime.now(UTC)
            existing = (
                await session.exec(
                    select(UploadBatchItem, UploadBatch)
                    .join(UploadBatch, col(UploadBatch.id) == col(UploadBatchItem.batch_id))
                    .where(
                        col(UploadBatch.created_by_user_id) == user_id,
                        col(UploadBatch.status) == UploadBatchStatus.ACTIVE,
                        col(UploadBatchItem.artifact_file_id) == artifact_id,
                        col(UploadBatchItem.status).in_(
                            (
                                UploadBatchItemStatus.STAGED,
                                UploadBatchItemStatus.PROCESSING,
                            )
                        ),
                    )
                    .order_by(col(UploadBatchItem.created_at), col(UploadBatchItem.id))
                )
            ).first()
            if existing is not None:
                item, batch = existing
                ingestion = (
                    await session.exec(
                        select(ArtifactIngestion).where(
                            col(ArtifactIngestion.artifact_file_id) == artifact_id
                        )
                    )
                ).first()
                if (
                    item.status is UploadBatchItemStatus.STAGED
                    and ingestion is not None
                    and ingestion.status is not ArtifactIngestionStatus.PROCESSING
                ):
                    _queue_ingestion_for_reparse(ingestion, queued_at=now)
                    session.add(ingestion)
                    await session.commit()
                return StagedUploadSubmission(
                    batch=_batch_view(batch),
                    items=(
                        _item_view(
                            item,
                            ingestion,
                            ingestion_status=(
                                ArtifactIngestionStatus.PROCESSING
                                if item.status is UploadBatchItemStatus.PROCESSING
                                else ArtifactIngestionStatus.PENDING
                            ),
                        ),
                    ),
                )

            ingestion = (
                await session.exec(
                    select(ArtifactIngestion)
                    .where(col(ArtifactIngestion.artifact_file_id) == artifact_id)
                    .with_for_update()
                )
            ).first()
            if ingestion is not None and ingestion.status is not ArtifactIngestionStatus.PROCESSING:
                _queue_ingestion_for_reparse(ingestion, queued_at=now)
                session.add(ingestion)
            batch = UploadBatch(
                project_id=artifact.project_id,
                created_by_user_id=user_id,
                artifact_kind=artifact.artifact_kind,
                status=UploadBatchStatus.ACTIVE,
                total_count=1,
                total_bytes=artifact.size_bytes,
                staged_count=1,
                updated_at=now,
            )
            session.add(batch)
            await session.flush()
            batch_id = _required_uuid(batch.id, "UploadBatch")
            item = UploadBatchItem(
                batch_id=batch_id,
                client_file_id=uuid4(),
                position=0,
                original_filename=artifact.original_filename,
                relative_path=artifact.source_relative_path or artifact.original_filename,
                size_bytes=artifact.size_bytes,
                media_type=artifact.media_type,
                content_sha256=artifact.content_sha256,
                status=UploadBatchItemStatus.STAGED,
                parse_status=ImportParseStatus.PENDING.value,
                materialization_status=ImportMaterializationStatus.SUCCEEDED.value,
                artifact_file_id=artifact_id,
                metadata_json=_with_upload_progress(
                    {"reparse": True},
                    phase="staged",
                    completed=0,
                    total=1,
                ),
                updated_at=now,
            )
            session.add(item)
            await session.commit()
            await session.refresh(batch)
            await session.refresh(item)
            return StagedUploadSubmission(
                batch=_batch_view(batch),
                items=(
                    _item_view(
                        item,
                        ingestion,
                        ingestion_status=(
                            ingestion.status
                            if ingestion is not None
                            else ArtifactIngestionStatus.PENDING
                        ),
                    ),
                ),
            )

    @staticmethod
    async def list_batches(
        *,
        user_id: UUID,
        project_id: UUID,
        limit: int = 25,
        offset: int = 0,
    ) -> UploadBatchPage:
        criteria = [col(UploadBatch.created_by_user_id) == user_id]
        criteria.append(col(UploadBatch.project_id) == project_id)
        count_statement = select(func.count()).select_from(UploadBatch).where(*criteria)
        statement = (
            select(UploadBatch)
            .where(*criteria)
            .order_by(col(UploadBatch.created_at).desc(), col(UploadBatch.id).desc())
            .offset(offset)
            .limit(limit)
        )
        async with session_factory() as session:
            total = int((await session.exec(count_statement)).one())
            batches = (await session.exec(statement)).all()
        return UploadBatchPage(
            items=[_batch_view(batch) for batch in batches],
            total=total,
            limit=limit,
            offset=offset,
        )

    @staticmethod
    async def get(batch_id: UUID, *, user_id: UUID, project_id: UUID) -> UploadBatchView:
        async with session_factory() as session:
            return _batch_view(
                await _owned_batch(session, batch_id, user_id, project_id=project_id)
            )

    @staticmethod
    async def list_items(
        batch_id: UUID,
        *,
        user_id: UUID,
        project_id: UUID,
        status: UploadBatchItemStatus | None = None,
        updated_after: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> UploadBatchItemPage:
        async with session_factory() as session:
            await _owned_batch(session, batch_id, user_id, project_id=project_id)
            criteria = [col(UploadBatchItem.batch_id) == batch_id]
            if status is not None:
                criteria.append(col(UploadBatchItem.status) == status)
            if updated_after is not None:
                criteria.append(col(UploadBatchItem.updated_at) >= updated_after)
            total = int(
                (
                    await session.exec(
                        select(func.count()).select_from(UploadBatchItem).where(*criteria)
                    )
                ).one()
            )
            items = list(
                (
                    await session.exec(
                        select(UploadBatchItem)
                        .where(*criteria)
                        .order_by(col(UploadBatchItem.position), col(UploadBatchItem.id))
                        .offset(offset)
                        .limit(limit)
                    )
                ).all()
            )
            artifact_ids = {
                artifact_id for item in items if (artifact_id := item.artifact_file_id) is not None
            }
            ingestions_by_artifact_id: dict[UUID, ArtifactIngestion] = {}
            if artifact_ids:
                ingestions = (
                    await session.exec(
                        select(ArtifactIngestion)
                        .where(col(ArtifactIngestion.artifact_file_id).in_(artifact_ids))
                        .with_for_update()
                    )
                ).all()
                ingestions_by_artifact_id = {
                    ingestion.artifact_file_id: ingestion for ingestion in ingestions
                }
        return UploadBatchItemPage(
            items=[
                _item_view(
                    item,
                    ingestions_by_artifact_id.get(item.artifact_file_id)
                    if item.artifact_file_id is not None
                    else None,
                )
                for item in items
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    @staticmethod
    async def set_status(
        batch_id: UUID,
        payload: UploadBatchStatusUpdate,
        *,
        user_id: UUID,
    ) -> UploadBatchView:
        async with session_factory() as session:
            batch = await _owned_batch(session, batch_id, user_id, lock=True)
            if batch.status in {UploadBatchStatus.CANCELLED, UploadBatchStatus.COMPLETED}:
                raise UploadBatchConflictError(
                    "terminal upload batches cannot be paused or resumed"
                )
            batch.status = UploadBatchStatus(payload.status)
            batch.updated_at = datetime.now(UTC)
            session.add(batch)
            await session.commit()
            await session.refresh(batch)
            return _batch_view(batch)

    # The methods below are the server-owned queue implementation. HTTP only
    # stages bytes; a worker owns parsing.

    @staticmethod
    def _payload_size(upload: ArtifactUploadPayload) -> int:
        if upload.declared_size_bytes is not None:
            return upload.declared_size_bytes
        if upload.payload is not None:
            return len(upload.payload)
        if upload.spool_path is not None:
            return upload.spool_path.stat().st_size
        return -1

    @staticmethod
    def _payload_sha256(upload: ArtifactUploadPayload) -> str:
        if upload.payload is not None:
            return sha256(upload.payload).hexdigest()
        if upload.spool_path is None:
            raise UploadBatchConflictError("uploaded artifact has no payload")
        digest = sha256()
        with upload.spool_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    async def _available_artifact_for_item(
        session: AsyncSession,
        batch: UploadBatch,
        item: UploadBatchItem,
    ) -> ArtifactFile | None:
        """Resolve the object that may have been stored before the request died."""

        if item.artifact_file_id is not None:
            artifact = await session.get(ArtifactFile, item.artifact_file_id)
            if (
                artifact is not None
                and artifact.project_id == batch.project_id
                and artifact.artifact_kind is batch.artifact_kind
                and artifact.size_bytes == item.size_bytes
                and (
                    (item.content_sha256 is None or artifact.content_sha256 == item.content_sha256)
                    and (
                        item.expected_file_sha256 is None
                        or artifact.content_sha256 == item.expected_file_sha256
                    )
                )
                and artifact.storage_status is StorageStatus.AVAILABLE
            ):
                return artifact
        # A manifest expectation is authoritative.  ``content_sha256`` may be
        # a stale digest from an earlier failed attempt and must not prevent
        # recovery from a valid object with the registered identity.
        expected_sha256 = item.expected_file_sha256 or item.content_sha256
        if expected_sha256 is None:
            return None
        return (
            await session.exec(
                select(ArtifactFile)
                .where(
                    col(ArtifactFile.project_id) == batch.project_id,
                    col(ArtifactFile.content_sha256) == expected_sha256,
                    col(ArtifactFile.size_bytes) == item.size_bytes,
                    col(ArtifactFile.artifact_kind) == batch.artifact_kind,
                    col(ArtifactFile.storage_status) == StorageStatus.AVAILABLE,
                )
                .order_by(col(ArtifactFile.created_at), col(ArtifactFile.id))
            )
        ).first()

    @classmethod
    async def _link_recovered_artifact(
        cls,
        session: AsyncSession,
        batch: UploadBatch,
        item: UploadBatchItem,
    ) -> bool:
        artifact = await cls._available_artifact_for_item(session, batch, item)
        if artifact is None:
            return False
        item.artifact_file_id = _required_uuid(artifact.id, "ArtifactFile")
        return True

    @classmethod
    async def _finish_staged_item(
        cls,
        batch_id: UUID,
        client_file_id: UUID,
        *,
        user_id: UUID,
        result: object | None = None,
        error: Exception | None = None,
    ) -> UploadBatchItemView:
        """Close the short HTTP staging lease in an independent transaction."""

        from tricycle_reaction_db.application.dtos import ArtifactUploadResult

        upload_result = result if isinstance(result, ArtifactUploadResult) else None
        async with session_factory() as session:
            batch = await _owned_batch(session, batch_id, user_id, lock=True)
            item = await _batch_item(session, batch_id, client_file_id, lock=True)
            ingestion: ArtifactIngestion | None = None
            if item.artifact_file_id is not None:
                ingestion = (
                    await session.exec(
                        select(ArtifactIngestion).where(
                            col(ArtifactIngestion.artifact_file_id) == item.artifact_file_id
                        )
                    )
                ).first()

            if item.status is not UploadBatchItemStatus.UPLOADING:
                return _item_view(item, ingestion)

            now = datetime.now(UTC)
            batch.uploading_count = max(0, batch.uploading_count - 1)
            item.worker_lease_id = None
            item.worker_lease_expires_at = None

            if batch.status is UploadBatchStatus.CANCELLED:
                if upload_result is not None:
                    item.artifact_file_id = upload_result.artifact_id
                    item.parse_revision_id = upload_result.parse_revision_id
                item.status = UploadBatchItemStatus.CANCELLED
                item.materialization_status = (
                    ImportMaterializationStatus.SUCCEEDED.value
                    if upload_result is not None
                    else ImportMaterializationStatus.FAILED.value
                )
                batch.cancelled_count += 1
                item.error_code = None
                item.error_message = None
                progress_phase = "cancelled"
            elif error is not None or upload_result is None:
                item.artifact_file_id = None
                ingestion = None
                item.status = UploadBatchItemStatus.FAILED
                item.parse_status = ImportParseStatus.FAILED.value
                item.materialization_status = ImportMaterializationStatus.FAILED.value
                batch.failed_count += 1
                item.error_code = (
                    getattr(error, "error_code", None) or "artifact_stage_failed"
                    if error is not None
                    else "artifact_stage_failed"
                )
                item.error_message = (
                    str(error) or type(error).__name__
                    if error is not None
                    else "artifact staging failed"
                )
                progress_phase = "failed"
            else:
                item.artifact_file_id = upload_result.artifact_id
                item.parse_revision_id = upload_result.parse_revision_id
                item.materialization_status = ImportMaterializationStatus.SUCCEEDED.value
                if upload_result.ingestion_id is not None:
                    ingestion = await session.get(ArtifactIngestion, upload_result.ingestion_id)
                ingestion_status = upload_result.ingestion_status
                if ingestion_status is ArtifactIngestionStatus.PENDING:
                    item.status = UploadBatchItemStatus.STAGED
                    item.parse_status = ImportParseStatus.PENDING.value
                    batch.staged_count += 1
                    item.error_code = None
                    item.error_message = None
                    progress_phase = "staged"
                elif ingestion_status in {
                    ArtifactIngestionStatus.FAILED,
                    ArtifactIngestionStatus.FILTERED,
                }:
                    item.status = UploadBatchItemStatus.FAILED
                    item.parse_status = (
                        ImportParseStatus.FILTERED.value
                        if ingestion_status is ArtifactIngestionStatus.FILTERED
                        else ImportParseStatus.FAILED.value
                    )
                    batch.failed_count += 1
                    item.error_code = (
                        ingestion.error_code if ingestion is not None else "ingestion_failed"
                    )
                    item.error_message = (
                        ingestion.error_message
                        if ingestion is not None
                        else "artifact ingestion failed"
                    )
                    progress_phase = "failed"
                else:
                    item.status = UploadBatchItemStatus.SUCCEEDED
                    item.parse_status = ImportParseStatus.SUCCEEDED.value
                    batch.succeeded_count += 1
                    item.error_code = None
                    item.error_message = None
                    progress_phase = "completed"

            item.metadata_json = _with_upload_progress(
                item.metadata_json,
                phase=progress_phase,
                completed=1 if progress_phase in {"completed", "failed", "cancelled"} else 0,
                total=1,
            )
            item.updated_at = now
            batch.updated_at = now
            _finish_batch_if_terminal(batch)
            session.add(item)
            session.add(batch)
            await session.commit()
            return _item_view(item, ingestion)

    @classmethod
    async def upload_item(
        cls,
        batch_id: UUID,
        client_file_id: UUID,
        *,
        payload: bytes,
        filename: str,
        media_type: str,
        user_id: UUID,
    ) -> UploadBatchItemView:
        items = await cls.upload_items(
            batch_id,
            files=[
                (
                    client_file_id,
                    ArtifactUploadPayload(
                        filename=filename,
                        media_type=media_type,
                        payload=payload,
                    ),
                )
            ],
            user_id=user_id,
        )
        return items[0]

    @classmethod
    async def upload_items(
        cls,
        batch_id: UUID,
        *,
        files: list[tuple[UUID, ArtifactUploadPayload]],
        user_id: UUID,
    ) -> list[UploadBatchItemView]:
        """Store each file durably, then leave parsing to the worker.

        This method deliberately has one short database transaction for the
        upload lease and one independent transaction per file completion.  A
        slow MolOP parse is never part of the HTTP request or its database
        transaction.
        """

        if not files:
            raise UploadBatchConflictError("upload batch request contains no files")
        settings = get_settings()
        if len(files) > settings.max_batch_files:
            raise UploadBatchLimitError(
                f"upload batch exceeds the {settings.max_batch_files}-file limit"
            )
        client_file_ids = [client_file_id for client_file_id, _ in files]
        if len(set(client_file_ids)) != len(client_file_ids):
            raise UploadBatchConflictError("client_file_id must be unique within an upload request")
        total_bytes = sum(
            max(0, cls._payload_size(upload))
            for _, upload in files
            if upload.error_code is None
        )
        if len(files) > 1 and total_bytes > settings.max_batch_bytes:
            raise UploadBatchLimitError(
                f"upload batch exceeds the {settings.max_batch_bytes}-byte limit"
            )

        now = datetime.now(UTC)

        async def calculate_digest(
            client_file_id: UUID,
            upload: ArtifactUploadPayload,
        ) -> tuple[UUID, str]:
            # Local imports may point at multi-gigabyte files. Hashing them in
            # the event-loop thread would pause every upload session, even
            # though staging itself is independently bounded below.
            return client_file_id, await asyncio.to_thread(cls._payload_sha256, upload)

        content_sha256_by_client_id = dict(
            await asyncio.gather(
                *(
                    calculate_digest(client_file_id, upload)
                    for client_file_id, upload in files
                    if upload.error_code is None
                )
            )
        )
        pending: list[tuple[UUID, ArtifactUploadPayload, str, str, str | None, int]] = []
        views_by_client_id: dict[UUID, UploadBatchItemView] = {}
        async with session_factory() as session:
            batch = await _owned_batch(session, batch_id, user_id, lock=True)
            items = (
                await session.exec(
                    select(UploadBatchItem)
                    .where(
                        col(UploadBatchItem.batch_id) == batch_id,
                        col(UploadBatchItem.client_file_id).in_(client_file_ids),
                    )
                    .with_for_update()
                )
            ).all()
            items_by_client_id = {item.client_file_id: item for item in items}
            if len(items_by_client_id) != len(client_file_ids):
                raise UploadBatchNotFoundError("upload batch file not found")
            await AuthorizationService.require_project_permission(
                user_id,
                batch.project_id,
                ProjectPermission.ARTIFACT_UPLOAD,
            )

            artifact_ids = {
                item.artifact_file_id for item in items if item.artifact_file_id is not None
            }
            ingestions_by_artifact_id: dict[UUID, ArtifactIngestion] = {}
            if artifact_ids:
                ingestions_by_artifact_id = {
                    ingestion.artifact_file_id: ingestion
                    for ingestion in (
                        await session.exec(
                            select(ArtifactIngestion).where(
                                col(ArtifactIngestion.artifact_file_id).in_(artifact_ids)
                            )
                        )
                    ).all()
                }

            pending_count = 0
            for client_file_id, upload in files:
                item = items_by_client_id[client_file_id]
                payload_size = cls._payload_size(upload)
                if item.original_filename != upload.filename or item.size_bytes != payload_size:
                    raise UploadBatchConflictError(
                        "uploaded file does not match the filename and size reserved by "
                        "client_file_id"
                    )
                if item.status in {
                    UploadBatchItemStatus.SUCCEEDED,
                    UploadBatchItemStatus.STAGED,
                    UploadBatchItemStatus.PROCESSING,
                }:
                    views_by_client_id[client_file_id] = _item_view(
                        item,
                        ingestions_by_artifact_id.get(item.artifact_file_id)
                        if item.artifact_file_id is not None
                        else None,
                    )
                    continue
                if item.status is UploadBatchItemStatus.UPLOADING:
                    raise UploadBatchConflictError("upload batch file is already being uploaded")
                if item.status is UploadBatchItemStatus.CANCELLED:
                    raise UploadBatchConflictError("cancelled upload batch files cannot be retried")
                if upload.error_code is not None:
                    if item.status is UploadBatchItemStatus.FAILED:
                        batch.failed_count = max(0, batch.failed_count - 1)
                    item.status = UploadBatchItemStatus.FAILED
                    item.parse_status = ImportParseStatus.FAILED.value
                    item.materialization_status = ImportMaterializationStatus.FAILED.value
                    item.error_code = upload.error_code
                    item.error_message = upload.error_message or upload.error_code
                    item.worker_lease_id = None
                    item.worker_lease_expires_at = None
                    item.metadata_json = _with_upload_progress(
                        item.metadata_json,
                        phase="failed",
                        completed=1,
                        total=1,
                    )
                    item.updated_at = now
                    batch.failed_count += 1
                    batch.updated_at = now
                    session.add(item)
                    views_by_client_id[client_file_id] = _item_view(item)
                    continue
                expected_sha256 = item.expected_file_sha256
                actual_sha256 = content_sha256_by_client_id[client_file_id]
                if expected_sha256 is not None and actual_sha256 != expected_sha256:
                    if item.status is UploadBatchItemStatus.FAILED:
                        batch.failed_count = max(0, batch.failed_count - 1)
                    item.status = UploadBatchItemStatus.FAILED
                    item.parse_status = ImportParseStatus.FAILED.value
                    item.materialization_status = ImportMaterializationStatus.FAILED.value
                    item.error_code = "manifest_file_hash_mismatch"
                    item.error_message = (
                        "uploaded file SHA-256 does not match the registered manifest"
                    )
                    item.content_sha256 = actual_sha256
                    item.updated_at = now
                    batch.failed_count += 1
                    batch.updated_at = now
                    session.add(item)
                    views_by_client_id[client_file_id] = _item_view(item)
                    continue
                if batch.status is UploadBatchStatus.CANCELLED:
                    raise UploadBatchConflictError("cancelled upload batches cannot receive files")
                if batch.status is UploadBatchStatus.PAUSED:
                    raise UploadBatchConflictError("paused upload batches cannot receive files")
                if item.status is UploadBatchItemStatus.FAILED:
                    batch.failed_count = max(0, batch.failed_count - 1)
                resolved_media_type = _upload_media_type(
                    upload,
                    item.original_filename,
                    item.media_type,
                )
                item.media_type = resolved_media_type
                item.status = UploadBatchItemStatus.UPLOADING
                item.attempt_count += 1
                item.content_sha256 = actual_sha256
                item.materialization_status = ImportMaterializationStatus.PENDING.value
                item.parse_status = ImportParseStatus.NOT_STARTED.value
                item.error_code = None
                item.error_message = None
                item.worker_lease_id = None
                item.worker_lease_expires_at = None
                item.metadata_json = _with_upload_progress(
                    item.metadata_json,
                    phase="uploading",
                    completed=0,
                    total=1,
                )
                item.updated_at = now
                batch.uploading_count += 1
                pending_count += 1
                pending.append(
                    (
                        client_file_id,
                        upload,
                        resolved_media_type,
                        item.relative_path,
                        item.expected_file_sha256,
                        item.size_bytes,
                    )
                )
                session.add(item)

            if pending_count and batch.status is UploadBatchStatus.COMPLETED:
                batch.status = UploadBatchStatus.ACTIVE
            batch.updated_at = now
            _finish_batch_if_terminal(batch)
            session.add(batch)
            artifact_kind = batch.artifact_kind
            project_id = batch.project_id
            await session.commit()

        # Each stage operation is independent.  A failed object store request
        # becomes one failed queue item, while all other files remain usable.
        # The shared admission semaphore keeps concurrent upload sessions from
        # creating an unbounded number of RustFS client threads.
        stage_slots = _shared_stage_slots()

        async def stage_one(
            staged_file: tuple[UUID, ArtifactUploadPayload, str, str, str | None, int],
        ) -> tuple[UUID, UploadBatchItemView]:
            (
                client_file_id,
                upload,
                resolved_media_type,
                relative_path,
                expected_sha256,
                expected_size_bytes,
            ) = staged_file
            try:
                async with stage_slots:
                    source = upload.payload if upload.payload is not None else upload.spool_path
                    if source is None:
                        raise UploadBatchConflictError("uploaded artifact has no payload")
                    staged = await ArtifactUploadService.stage_source(
                        source=source,
                        filename=upload.filename,
                        media_type=resolved_media_type,
                        artifact_kind=artifact_kind,
                        project_id=project_id,
                        user_id=user_id,
                        relative_path=relative_path,
                        expected_sha256=expected_sha256,
                        expected_size_bytes=expected_size_bytes,
                    )
            except asyncio.CancelledError:
                await cls._finish_staged_item(
                    batch_id,
                    client_file_id,
                    user_id=user_id,
                    error=UploadBatchError("upload request was cancelled after staging started"),
                )
                raise
            except Exception as error:
                finished = await cls._finish_staged_item(
                    batch_id,
                    client_file_id,
                    user_id=user_id,
                    error=error,
                )
                return client_file_id, finished
            else:
                return client_file_id, await cls._finish_staged_item(
                    batch_id,
                    client_file_id,
                    user_id=user_id,
                    result=staged,
                )

        staged_results = await asyncio.gather(*(stage_one(file) for file in pending))
        views_by_client_id.update(dict(staged_results))
        return [views_by_client_id[client_file_id] for client_file_id in client_file_ids]

    @classmethod
    async def retry_failed(cls, batch_id: UUID, *, user_id: UUID) -> UploadBatchView:
        async with session_factory() as session:
            batch = await _owned_batch(session, batch_id, user_id, lock=True)
            if batch.status is UploadBatchStatus.CANCELLED:
                raise UploadBatchConflictError("cancelled upload batches cannot be retried")
            failed_items = (
                await session.exec(
                    select(UploadBatchItem)
                    .where(
                        col(UploadBatchItem.batch_id) == batch_id,
                        col(UploadBatchItem.status) == UploadBatchItemStatus.FAILED,
                    )
                    .with_for_update()
                )
            ).all()
            now = datetime.now(UTC)
            for item in failed_items:
                batch.failed_count = max(0, batch.failed_count - 1)
                if await cls._link_recovered_artifact(session, batch, item):
                    item.status = UploadBatchItemStatus.STAGED
                    item.parse_status = ImportParseStatus.PENDING.value
                    item.materialization_status = ImportMaterializationStatus.SUCCEEDED.value
                    batch.staged_count += 1
                    phase = "staged"
                else:
                    item.status = UploadBatchItemStatus.QUEUED
                    item.parse_status = ImportParseStatus.NOT_STARTED.value
                    item.materialization_status = ImportMaterializationStatus.NOT_STARTED.value
                    phase = "queued"
                item.error_code = None
                item.error_message = None
                item.worker_lease_id = None
                item.worker_lease_expires_at = None
                item.metadata_json = _with_upload_progress(
                    item.metadata_json,
                    phase=phase,
                    completed=0,
                    total=1,
                )
                item.updated_at = now
                session.add(item)
            batch.status = UploadBatchStatus.ACTIVE
            batch.updated_at = now
            session.add(batch)
            await session.commit()
            await session.refresh(batch)
            return _batch_view(batch)

    @classmethod
    async def retry_item(
        cls,
        batch_id: UUID,
        client_file_id: UUID,
        *,
        user_id: UUID,
    ) -> UploadBatchItemView:
        async with session_factory() as session:
            batch = await _owned_batch(session, batch_id, user_id, lock=True)
            item = await _batch_item(session, batch_id, client_file_id, lock=True)
            if batch.status is UploadBatchStatus.CANCELLED:
                raise UploadBatchConflictError("cancelled upload batches cannot be retried")
            if item.status is not UploadBatchItemStatus.FAILED:
                raise UploadBatchConflictError("only failed upload batch files can be retried")
            artifact_available = await cls._link_recovered_artifact(session, batch, item)
            now = datetime.now(UTC)
            batch.failed_count = max(0, batch.failed_count - 1)
            if artifact_available:
                item.status = UploadBatchItemStatus.STAGED
                item.parse_status = ImportParseStatus.PENDING.value
                item.materialization_status = ImportMaterializationStatus.SUCCEEDED.value
                batch.staged_count += 1
                phase = "staged"
            else:
                item.status = UploadBatchItemStatus.QUEUED
                item.parse_status = ImportParseStatus.NOT_STARTED.value
                item.materialization_status = ImportMaterializationStatus.NOT_STARTED.value
                phase = "queued"
            item.error_code = None
            item.error_message = None
            item.worker_lease_id = None
            item.worker_lease_expires_at = None
            item.metadata_json = _with_upload_progress(
                item.metadata_json,
                phase=phase,
                completed=0,
                total=1,
            )
            item.updated_at = now
            batch.status = UploadBatchStatus.ACTIVE
            batch.updated_at = now
            session.add(item)
            session.add(batch)
            ingestion = None
            if item.artifact_file_id is not None:
                ingestion = (
                    await session.exec(
                        select(ArtifactIngestion).where(
                            col(ArtifactIngestion.artifact_file_id) == item.artifact_file_id
                        )
                    )
                ).first()
            await session.commit()
            return _item_view(item, ingestion)

    @classmethod
    async def cancel(cls, batch_id: UUID, *, user_id: UUID) -> UploadBatchView:
        async with session_factory() as session:
            batch = await _owned_batch(session, batch_id, user_id, lock=True)
            if batch.status is UploadBatchStatus.CANCELLED:
                return _batch_view(batch)
            if batch.status is UploadBatchStatus.COMPLETED:
                raise UploadBatchConflictError("completed upload batches cannot be cancelled")
            now = datetime.now(UTC)
            cancellable_statuses = (
                UploadBatchItemStatus.QUEUED,
                UploadBatchItemStatus.STAGED,
            )
            cancellable_items = (
                await session.exec(
                    select(UploadBatchItem)
                    .where(
                        col(UploadBatchItem.batch_id) == batch_id,
                        col(UploadBatchItem.status).in_(cancellable_statuses),
                    )
                    .with_for_update()
                )
            ).all()
            for item in cancellable_items:
                if item.status is UploadBatchItemStatus.STAGED:
                    batch.staged_count = max(0, batch.staged_count - 1)
                item.status = UploadBatchItemStatus.CANCELLED
                item.updated_at = now
                item.metadata_json = _with_upload_progress(
                    item.metadata_json,
                    phase="cancelled",
                    completed=1,
                    total=1,
                )
                session.add(item)
            batch.cancelled_count += len(cancellable_items)
            batch.status = UploadBatchStatus.CANCELLED
            batch.updated_at = now
            session.add(batch)
            await session.commit()
            await session.refresh(batch)
            return _batch_view(batch)

    @classmethod
    async def recover_interrupted(cls, batch_id: UUID, *, user_id: UUID) -> UploadBatchView:
        """Recover stale HTTP and worker leases for one user-owned batch."""

        settings = get_settings()
        now = datetime.now(UTC)
        upload_cutoff = now - timedelta(seconds=settings.upload_client_lease_seconds)
        processing_cutoff = now
        async with session_factory() as session:
            batch = await _owned_batch(session, batch_id, user_id, lock=True)
            if batch.status is UploadBatchStatus.CANCELLED:
                raise UploadBatchConflictError("cancelled upload batches cannot be recovered")
            if batch.status is UploadBatchStatus.COMPLETED:
                raise UploadBatchConflictError("completed upload batches cannot be recovered")
            items = (
                await session.exec(
                    select(UploadBatchItem)
                    .where(
                        col(UploadBatchItem.batch_id) == batch_id,
                        or_(
                            and_(
                                col(UploadBatchItem.status) == UploadBatchItemStatus.UPLOADING,
                                col(UploadBatchItem.updated_at) <= upload_cutoff,
                            ),
                            and_(
                                col(UploadBatchItem.status) == UploadBatchItemStatus.PROCESSING,
                                or_(
                                    col(UploadBatchItem.worker_lease_expires_at).is_(None),
                                    col(UploadBatchItem.worker_lease_expires_at)
                                    <= processing_cutoff,
                                ),
                            ),
                        ),
                    )
                    .with_for_update()
                )
            ).all()
            await cls._recover_items_in_session(session, batch, list(items), now=now)
            batch.status = UploadBatchStatus.ACTIVE
            batch.updated_at = now
            session.add(batch)
            await session.commit()
            await session.refresh(batch)
            return _batch_view(batch)

    @classmethod
    async def recover_stale(
        cls,
        *,
        limit: int = 100,
        recover_unexpired_processing: bool = False,
    ) -> int:
        """Recover leases left by a crashed API or worker process.

        Normal polling only reclaims expired leases. A worker restart is a
        stronger boundary: the process that owned every unexpired processing
        lease is gone, so startup recovery must return those rows to ``staged``
        before the new worker starts claiming work. The stronger mode is
        intentionally explicit and is used only once by the single deployed
        upload worker.
        """

        settings = get_settings()
        now = datetime.now(UTC)
        upload_cutoff = now - timedelta(seconds=settings.upload_client_lease_seconds)
        processing_recovery = (
            col(UploadBatchItem.status) == UploadBatchItemStatus.PROCESSING
            if recover_unexpired_processing
            else and_(
                col(UploadBatchItem.status) == UploadBatchItemStatus.PROCESSING,
                or_(
                    col(UploadBatchItem.worker_lease_expires_at).is_(None),
                    col(UploadBatchItem.worker_lease_expires_at) <= now,
                ),
            )
        )
        statement = (
            select(UploadBatchItem, UploadBatch)
            .join(UploadBatch, col(UploadBatch.id) == col(UploadBatchItem.batch_id))
            .where(
                col(UploadBatch.status).in_(
                    (
                        UploadBatchStatus.ACTIVE,
                        UploadBatchStatus.PAUSED,
                        UploadBatchStatus.CANCELLED,
                    )
                ),
                or_(
                    and_(
                        col(UploadBatchItem.status) == UploadBatchItemStatus.UPLOADING,
                        col(UploadBatchItem.updated_at) <= upload_cutoff,
                    ),
                    processing_recovery,
                ),
            )
            .order_by(col(UploadBatchItem.updated_at), col(UploadBatchItem.id))
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        async with session_factory() as session:
            rows = (await session.exec(statement)).all()
            batches: dict[UUID, UploadBatch] = {}
            items_by_batch: dict[UUID, list[UploadBatchItem]] = {}
            for item, batch in rows:
                batches[_required_uuid(batch.id, "UploadBatch")] = batch
                items_by_batch.setdefault(_required_uuid(batch.id, "UploadBatch"), []).append(item)
            recovered = 0
            for batch_id, items in items_by_batch.items():
                recovered += await cls._recover_items_in_session(
                    session,
                    batches[batch_id],
                    items,
                    now=now,
                )
                batches[batch_id].updated_at = now
                _finish_batch_if_terminal(batches[batch_id])
                session.add(batches[batch_id])

            # Compatibility ingestions do not have an *active*
            # UploadBatchItem to drive recovery. A terminal item may remain
            # after an older worker crashed between publishing the item and
            # finalizing ArtifactIngestion; it must not strand the ingestion
            # forever. Active batch-owned rows are reconciled together with
            # their item above.
            has_active_upload_item = (
                select(1)
                .where(
                    col(UploadBatchItem.artifact_file_id)
                    == col(ArtifactIngestion.artifact_file_id),
                    col(UploadBatchItem.status).in_(
                        (
                            UploadBatchItemStatus.UPLOADING,
                            UploadBatchItemStatus.STAGED,
                            UploadBatchItemStatus.PROCESSING,
                        )
                    ),
                )
                .exists()
            )
            compatibility_processing_recovery = (
                col(ArtifactIngestion.status) == ArtifactIngestionStatus.PROCESSING
                if recover_unexpired_processing
                else and_(
                    col(ArtifactIngestion.status) == ArtifactIngestionStatus.PROCESSING,
                    or_(
                        col(ArtifactIngestion.worker_lease_id).is_(None),
                        col(ArtifactIngestion.worker_lease_expires_at).is_(None),
                        col(ArtifactIngestion.worker_lease_expires_at) <= now,
                    ),
                )
            )
            stale_ingestions = (
                await session.exec(
                    select(ArtifactIngestion)
                    .where(
                        compatibility_processing_recovery,
                        ~has_active_upload_item,
                    )
                    .order_by(col(ArtifactIngestion.started_at).nulls_first())
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for ingestion in stale_ingestions:
                _reset_ingestion_to_pending(ingestion)
                session.add(ingestion)
                recovered += 1
            if recovered:
                await session.commit()
            return recovered

    @classmethod
    async def claim_pending_ingestions(
        cls,
        *,
        limit: int | None = None,
    ) -> list[PendingIngestionJob]:
        """Lease old pending ingestions that have no active upload-batch item.

        The legacy single/batch artifact endpoints and the local importer can
        commit an ``ArtifactFile`` reservation before their process exits.
        Those rows predate the durable ``UploadBatch`` manifest, so they need a
        small compatibility queue.  Active web-queue items are excluded here;
        their own item lease remains the source of truth.
        """

        settings = get_settings()
        resolved_limit = limit or settings.upload_worker_concurrency
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=settings.upload_pending_recovery_seconds)
        active_upload_item = (
            select(1)
            .where(
                col(UploadBatchItem.artifact_file_id) == col(ArtifactIngestion.artifact_file_id),
                col(UploadBatchItem.status).in_(
                    (
                        UploadBatchItemStatus.UPLOADING,
                        UploadBatchItemStatus.STAGED,
                        UploadBatchItemStatus.PROCESSING,
                    )
                ),
            )
            .exists()
        )
        statement = (
            select(ArtifactIngestion, ArtifactFile)
            .join(
                ArtifactFile,
                col(ArtifactFile.id) == col(ArtifactIngestion.artifact_file_id),
            )
            .where(
                col(ArtifactIngestion.status) == ArtifactIngestionStatus.PENDING,
                col(ArtifactFile.artifact_kind) == ArtifactKind.CALCULATION_OUTPUT,
                col(ArtifactFile.storage_status).in_(
                    (StorageStatus.PENDING, StorageStatus.AVAILABLE)
                ),
                or_(
                    col(ArtifactIngestion.worker_lease_id).is_(None),
                    col(ArtifactIngestion.worker_lease_expires_at).is_(None),
                    col(ArtifactIngestion.worker_lease_expires_at) <= now,
                ),
                or_(
                    col(ArtifactIngestion.started_at).is_(None),
                    col(ArtifactIngestion.started_at) <= cutoff,
                ),
                ~active_upload_item,
            )
            .order_by(
                col(ArtifactIngestion.started_at).nulls_first(),
                col(ArtifactIngestion.id),
            )
            .limit(resolved_limit)
            .with_for_update(skip_locked=True)
        )
        jobs: list[PendingIngestionJob] = []
        async with session_factory() as session:
            rows = (await session.exec(statement)).all()
            lease_by_ingestion_id: dict[UUID, UUID] = {}
            lease_expires_at = now + timedelta(seconds=settings.upload_worker_lease_seconds)
            for ingestion, artifact in rows:
                ingestion_id = _required_uuid(ingestion.id, "ArtifactIngestion")
                artifact_id = _required_uuid(artifact.id, "ArtifactFile")
                lease_id = uuid4()
                lease_by_ingestion_id[ingestion_id] = lease_id
                jobs.append(
                    PendingIngestionJob(
                        project_id=artifact.project_id,
                        ingestion_id=ingestion_id,
                        artifact_file_id=artifact_id,
                        user_id=artifact.created_by_user_id,
                        lease_id=lease_id,
                        lease_expires_at=lease_expires_at,
                    )
                )
            if jobs:
                await session.exec(
                    update(ArtifactIngestion)
                    .where(col(ArtifactIngestion.id).in_(lease_by_ingestion_id))
                    .values(
                        status=ArtifactIngestionStatus.PROCESSING,
                        started_at=now,
                        completed_at=None,
                        processing_attempt_count=(
                            col(ArtifactIngestion.processing_attempt_count) + 1
                        ),
                        worker_lease_id=case(
                            lease_by_ingestion_id,
                            value=col(ArtifactIngestion.id),
                            else_=col(ArtifactIngestion.worker_lease_id),
                        ),
                        worker_lease_expires_at=lease_expires_at,
                        error_code=None,
                        error_message=None,
                    )
                )
                await session.commit()
        return jobs

    @staticmethod
    async def renew_pending_ingestion_lease(job: PendingIngestionJob) -> bool:
        """Keep a compatibility-queue ingestion owned while MolOP runs."""

        settings = get_settings()
        async with session_factory() as session:
            ingestion = (
                await session.exec(
                    select(ArtifactIngestion)
                    .where(
                        col(ArtifactIngestion.id) == job.ingestion_id,
                        col(ArtifactIngestion.artifact_file_id) == job.artifact_file_id,
                        col(ArtifactIngestion.status) == ArtifactIngestionStatus.PROCESSING,
                        col(ArtifactIngestion.worker_lease_id) == job.lease_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if ingestion is None:
                return False
            ingestion.worker_lease_expires_at = datetime.now(UTC) + timedelta(
                seconds=settings.upload_worker_lease_seconds
            )
            session.add(ingestion)
            await session.commit()
            return True

    @staticmethod
    async def renew_pending_ingestion_leases(jobs: list[PendingIngestionJob]) -> int:
        """Renew a compatibility claim with one set-based database update.

        The worker processes a project/user group as one parser/persistence
        microbatch.  Renewing every row through its own ``SELECT ... FOR
        UPDATE`` creates one database session per file and makes those
        heartbeat transactions wait behind the persistence transaction.  A
        single conditional update keeps the lease semantics while limiting
        the heartbeat to one session for the whole group.
        """

        if not jobs:
            return 0
        settings = get_settings()
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=settings.upload_worker_lease_seconds)
        predicate = or_(
            *(
                and_(
                    col(ArtifactIngestion.id) == job.ingestion_id,
                    col(ArtifactIngestion.artifact_file_id) == job.artifact_file_id,
                    col(ArtifactIngestion.status) == ArtifactIngestionStatus.PROCESSING,
                    col(ArtifactIngestion.worker_lease_id) == job.lease_id,
                )
                for job in jobs
            )
        )
        async with session_factory() as session:
            result = await session.exec(
                update(ArtifactIngestion)
                .where(predicate)
                .values(worker_lease_expires_at=expires_at)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    @staticmethod
    async def _recover_items_in_session(
        session: AsyncSession,
        batch: UploadBatch,
        items: list[UploadBatchItem],
        *,
        now: datetime,
    ) -> int:
        recovered = 0
        for item in items:
            was_processing = item.status is UploadBatchItemStatus.PROCESSING
            if item.status is UploadBatchItemStatus.UPLOADING:
                batch.uploading_count = max(0, batch.uploading_count - 1)
            elif was_processing:
                batch.processing_count = max(0, batch.processing_count - 1)
            else:
                continue

            if batch.status is UploadBatchStatus.CANCELLED:
                item.status = UploadBatchItemStatus.CANCELLED
                item.parse_status = ImportParseStatus.FAILED.value
                batch.cancelled_count += 1
                phase = "cancelled"
                error_code = "upload_cancelled"
                error_message = "批次已取消，服务端已停止处理此文件"
            elif await UploadBatchService._link_recovered_artifact(session, batch, item):
                item.status = UploadBatchItemStatus.STAGED
                item.parse_status = ImportParseStatus.PENDING.value
                item.materialization_status = ImportMaterializationStatus.SUCCEEDED.value
                batch.staged_count += 1
                phase = "staged"
                error_code = "worker_interrupted"
                error_message = "处理进程中断，文件仍保留在服务端等待重试"
            elif was_processing:
                item.status = UploadBatchItemStatus.FAILED
                item.parse_status = ImportParseStatus.FAILED.value
                item.materialization_status = ImportMaterializationStatus.FAILED.value
                batch.failed_count += 1
                phase = "failed"
                error_code = "processing_artifact_unavailable"
                error_message = "处理租约失效且服务端文件不可用"
            else:
                item.status = UploadBatchItemStatus.QUEUED
                item.parse_status = ImportParseStatus.NOT_STARTED.value
                item.materialization_status = ImportMaterializationStatus.NOT_STARTED.value
                phase = "queued"
                error_code = "upload_interrupted"
                error_message = "上传请求中断，文件已返回等待队列"
            if was_processing and item.artifact_file_id is not None:
                ingestion = (
                    await session.exec(
                        select(ArtifactIngestion)
                        .where(
                            col(ArtifactIngestion.artifact_file_id) == item.artifact_file_id,
                            col(ArtifactIngestion.status) == ArtifactIngestionStatus.PROCESSING,
                            or_(
                                col(ArtifactIngestion.worker_lease_id) == item.worker_lease_id,
                                col(ArtifactIngestion.worker_lease_id).is_(None),
                                col(ArtifactIngestion.worker_lease_expires_at).is_(None),
                                col(ArtifactIngestion.worker_lease_expires_at) <= now,
                            ),
                        )
                        .with_for_update()
                    )
                ).first()
                if ingestion is not None:
                    _reset_ingestion_to_pending(ingestion)
                    session.add(ingestion)
            item.worker_lease_id = None
            item.worker_lease_expires_at = None
            item.error_code = error_code
            item.error_message = error_message
            item.metadata_json = _with_upload_progress(
                item.metadata_json,
                phase=phase,
                completed=1 if phase == "failed" else 0,
                total=1,
            )
            item.updated_at = now
            session.add(item)
            recovered += 1
        return recovered

    @classmethod
    async def claim_processing(cls, *, limit: int | None = None) -> list[UploadProcessingJob]:
        """Atomically lease staged artifacts to one parser worker."""

        settings = get_settings()
        resolved_limit = limit or settings.upload_worker_concurrency
        now = datetime.now(UTC)
        jobs: list[UploadProcessingJob] = []
        statement = (
            select(UploadBatchItem, UploadBatch)
            .join(UploadBatch, col(UploadBatch.id) == col(UploadBatchItem.batch_id))
            .where(
                col(UploadBatch.status) == UploadBatchStatus.ACTIVE,
                col(UploadBatchItem.status) == UploadBatchItemStatus.STAGED,
            )
            .order_by(col(UploadBatchItem.created_at), col(UploadBatchItem.id))
            .limit(resolved_limit)
            .with_for_update(skip_locked=True)
        )
        async with session_factory() as session:
            rows = (await session.exec(statement)).all()
            artifact_ids = {
                item.artifact_file_id for item, _batch in rows if item.artifact_file_id is not None
            }
            await session.exec(
                select(ArtifactIngestion)
                .where(col(ArtifactIngestion.artifact_file_id).in_(artifact_ids))
                .with_for_update()
            )
            item_ids: list[UUID] = []
            item_statuses: dict[UUID, UploadBatchItemStatus] = {}
            item_parse_statuses: dict[UUID, str] = {}
            item_materialization_statuses: dict[UUID, str] = {}
            item_error_codes: dict[UUID, str | None] = {}
            item_error_messages: dict[UUID, str | None] = {}
            item_metadata: dict[UUID, dict[str, object]] = {}
            item_processing_attempts: set[UUID] = set()
            lease_by_item_id: dict[UUID, UUID] = {}
            lease_by_artifact_id: dict[UUID, UUID] = {}
            batch_deltas: dict[UUID, list[int]] = {}
            lease_expires_at = now + timedelta(seconds=settings.upload_worker_lease_seconds)
            for item, batch in rows:
                batch_id = _required_uuid(batch.id, "UploadBatch")
                item_id = _required_uuid(item.id, "UploadBatchItem")
                item_ids.append(item_id)
                delta = batch_deltas.setdefault(batch_id, [0, 0, 0])
                delta[0] -= 1  # staged_count
                if item.artifact_file_id is None:
                    item_statuses[item_id] = UploadBatchItemStatus.FAILED
                    item_parse_statuses[item_id] = ImportParseStatus.FAILED.value
                    item_materialization_statuses[item_id] = (
                        ImportMaterializationStatus.FAILED.value
                    )
                    item_error_codes[item_id] = "staged_artifact_missing"
                    item_error_messages[item_id] = "staged queue item has no artifact reference"
                    item_metadata[item_id] = _with_upload_progress(
                        item.metadata_json,
                        phase="failed",
                        completed=1,
                        total=1,
                    )
                    delta[2] += 1  # failed_count
                    continue
                lease_id = uuid4()
                item_statuses[item_id] = UploadBatchItemStatus.PROCESSING
                item_parse_statuses[item_id] = ImportParseStatus.PENDING.value
                item_materialization_statuses[item_id] = ImportMaterializationStatus.PENDING.value
                item_processing_attempts.add(item_id)
                lease_by_item_id[item_id] = lease_id
                lease_by_artifact_id[item.artifact_file_id] = lease_id
                item_error_codes[item_id] = None
                item_error_messages[item_id] = None
                item_metadata[item_id] = _with_upload_progress(
                    item.metadata_json,
                    phase="processing",
                    completed=0,
                    total=1,
                )
                delta[1] += 1  # processing_count
                jobs.append(
                    UploadProcessingJob(
                        project_id=batch.project_id,
                        batch_id=batch_id,
                        item_id=item_id,
                        client_file_id=item.client_file_id,
                        artifact_file_id=item.artifact_file_id,
                        user_id=batch.created_by_user_id,
                        lease_id=lease_id,
                        lease_expires_at=lease_expires_at,
                    )
                )

            def keyed_case(
                column: object,
                values: Mapping[UUID, object],
                *,
                key_column: object,
            ) -> object:
                if not values:
                    return column
                return case(values, value=key_column, else_=column)

            def jsonb_case(
                column: object,
                values: Mapping[UUID, object],
                *,
                key_column: object,
            ) -> object:
                if not values:
                    return column
                return case(
                    {key: cast(value, JSONB) for key, value in values.items()},
                    value=key_column,
                    else_=column,
                )

            if item_ids:
                await session.exec(
                    update(UploadBatchItem)
                    .where(col(UploadBatchItem.id).in_(item_ids))
                    .values(
                        status=keyed_case(
                            col(UploadBatchItem.status),
                            item_statuses,
                            key_column=col(UploadBatchItem.id),
                        ),
                        parse_status=keyed_case(
                            col(UploadBatchItem.parse_status),
                            item_parse_statuses,
                            key_column=col(UploadBatchItem.id),
                        ),
                        materialization_status=keyed_case(
                            col(UploadBatchItem.materialization_status),
                            item_materialization_statuses,
                            key_column=col(UploadBatchItem.id),
                        ),
                        processing_attempt_count=case(
                            (
                                col(UploadBatchItem.id).in_(item_processing_attempts),
                                col(UploadBatchItem.processing_attempt_count) + 1,
                            ),
                            else_=col(UploadBatchItem.processing_attempt_count),
                        ),
                        worker_lease_id=keyed_case(
                            col(UploadBatchItem.worker_lease_id),
                            lease_by_item_id,
                            key_column=col(UploadBatchItem.id),
                        ),
                        worker_lease_expires_at=case(
                            (col(UploadBatchItem.id).in_(lease_by_item_id), lease_expires_at),
                            else_=col(UploadBatchItem.worker_lease_expires_at),
                        ),
                        error_code=keyed_case(
                            col(UploadBatchItem.error_code),
                            item_error_codes,
                            key_column=col(UploadBatchItem.id),
                        ),
                        error_message=keyed_case(
                            col(UploadBatchItem.error_message),
                            item_error_messages,
                            key_column=col(UploadBatchItem.id),
                        ),
                        metadata_json=jsonb_case(
                            col(UploadBatchItem.metadata_json),
                            item_metadata,
                            key_column=col(UploadBatchItem.id),
                        ),
                        updated_at=now,
                    )
                )

            ingestion_artifact_ids = list(lease_by_artifact_id)
            if ingestion_artifact_ids:
                await session.exec(
                    update(ArtifactIngestion)
                    .where(col(ArtifactIngestion.artifact_file_id).in_(ingestion_artifact_ids))
                    .values(
                        status=ArtifactIngestionStatus.PROCESSING,
                        started_at=now,
                        completed_at=None,
                        processing_attempt_count=(
                            col(ArtifactIngestion.processing_attempt_count) + 1
                        ),
                        worker_lease_id=keyed_case(
                            col(ArtifactIngestion.worker_lease_id),
                            lease_by_artifact_id,
                            key_column=col(ArtifactIngestion.artifact_file_id),
                        ),
                        worker_lease_expires_at=lease_expires_at,
                        error_code=None,
                        error_message=None,
                    )
                )

            batch_ids = list(batch_deltas)
            batch_statuses: dict[UUID, UploadBatchStatus] = {}
            for batch_id in batch_ids:
                batch = next(batch for _item, batch in rows if batch.id == batch_id)
                staged_delta, processing_delta, failed_delta = batch_deltas[batch_id]
                new_staged = max(0, batch.staged_count + staged_delta)
                new_processing = max(0, batch.processing_count + processing_delta)
                new_failed = batch.failed_count + failed_delta
                terminal = batch.succeeded_count + new_failed + batch.cancelled_count
                batch_statuses[batch_id] = (
                    UploadBatchStatus.COMPLETED
                    if (
                        batch.status is not UploadBatchStatus.CANCELLED
                        and batch.uploading_count == 0
                        and new_staged == 0
                        and new_processing == 0
                        and terminal == batch.total_count
                    )
                    else batch.status
                )
            if batch_ids:
                await session.exec(
                    update(UploadBatch)
                    .where(col(UploadBatch.id).in_(batch_ids))
                    .values(
                        staged_count=case(
                            {
                                batch_id: max(
                                    0,
                                    next(
                                        batch for _item, batch in rows if batch.id == batch_id
                                    ).staged_count
                                    + batch_deltas[batch_id][0],
                                )
                                for batch_id in batch_ids
                            },
                            value=col(UploadBatch.id),
                            else_=col(UploadBatch.staged_count),
                        ),
                        processing_count=case(
                            {
                                batch_id: max(
                                    0,
                                    next(
                                        batch for _item, batch in rows if batch.id == batch_id
                                    ).processing_count
                                    + batch_deltas[batch_id][1],
                                )
                                for batch_id in batch_ids
                            },
                            value=col(UploadBatch.id),
                            else_=col(UploadBatch.processing_count),
                        ),
                        failed_count=case(
                            {
                                batch_id: next(
                                    batch for _item, batch in rows if batch.id == batch_id
                                ).failed_count
                                + batch_deltas[batch_id][2]
                                for batch_id in batch_ids
                            },
                            value=col(UploadBatch.id),
                            else_=col(UploadBatch.failed_count),
                        ),
                        status=keyed_case(
                            col(UploadBatch.status),
                            batch_statuses,
                            key_column=col(UploadBatch.id),
                        ),
                        updated_at=now,
                    )
                )
            await session.commit()
        return jobs

    @staticmethod
    async def renew_processing_lease(job: UploadProcessingJob) -> bool:
        settings = get_settings()
        async with session_factory() as session:
            item = (
                await session.exec(
                    select(UploadBatchItem)
                    .where(
                        col(UploadBatchItem.id) == job.item_id,
                        col(UploadBatchItem.batch_id) == job.batch_id,
                        col(UploadBatchItem.status) == UploadBatchItemStatus.PROCESSING,
                        col(UploadBatchItem.worker_lease_id) == job.lease_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if item is None:
                return False
            now = datetime.now(UTC)
            expires_at = now + timedelta(seconds=settings.upload_worker_lease_seconds)
            item.worker_lease_expires_at = expires_at
            item.updated_at = now
            if item.artifact_file_id is not None:
                ingestion = (
                    await session.exec(
                        select(ArtifactIngestion)
                        .where(
                            col(ArtifactIngestion.artifact_file_id) == item.artifact_file_id,
                            col(ArtifactIngestion.status) == ArtifactIngestionStatus.PROCESSING,
                            col(ArtifactIngestion.worker_lease_id) == job.lease_id,
                        )
                        .with_for_update()
                    )
                ).first()
                if ingestion is not None:
                    ingestion.worker_lease_expires_at = expires_at
                    session.add(ingestion)
            session.add(item)
            await session.commit()
            return True

    @staticmethod
    async def renew_processing_leases(jobs: list[UploadProcessingJob]) -> int:
        """Renew one upload-batch claim group using set-based updates.

        The streaming worker owns the only persistence consumer for a
        project/user group.  Heartbeats must therefore share that same
        coarse-grained shape; a per-file locking query otherwise creates a
        connection and a lock waiter for every claimed item.
        """

        if not jobs:
            return 0
        settings = get_settings()
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=settings.upload_worker_lease_seconds)
        item_predicate = or_(
            *(
                and_(
                    col(UploadBatchItem.id) == job.item_id,
                    col(UploadBatchItem.batch_id) == job.batch_id,
                    col(UploadBatchItem.status) == UploadBatchItemStatus.PROCESSING,
                    col(UploadBatchItem.worker_lease_id) == job.lease_id,
                )
                for job in jobs
            )
        )
        ingestion_predicate = or_(
            *(
                and_(
                    col(ArtifactIngestion.artifact_file_id) == job.artifact_file_id,
                    col(ArtifactIngestion.status) == ArtifactIngestionStatus.PROCESSING,
                    col(ArtifactIngestion.worker_lease_id) == job.lease_id,
                )
                for job in jobs
            )
        )
        async with session_factory() as session:
            item_result = await session.exec(
                update(UploadBatchItem)
                .where(item_predicate)
                .values(
                    worker_lease_expires_at=expires_at,
                    updated_at=now,
                )
            )
            await session.exec(
                update(ArtifactIngestion)
                .where(ingestion_predicate)
                .values(worker_lease_expires_at=expires_at)
            )
            await session.commit()
        return int(getattr(item_result, "rowcount", 0) or 0)

    @classmethod
    async def finish_processing(
        cls,
        job: UploadProcessingJob,
        *,
        result: object | None = None,
        error: Exception | None = None,
    ) -> UploadBatchItemView | None:
        """Commit a worker result only while its lease is still current."""

        from tricycle_reaction_db.application.dtos import ArtifactUploadResult

        upload_result = result if isinstance(result, ArtifactUploadResult) else None
        async with session_factory() as session:
            batch = (
                await session.exec(
                    select(UploadBatch).where(col(UploadBatch.id) == job.batch_id).with_for_update()
                )
            ).one_or_none()
            item = (
                await session.exec(
                    select(UploadBatchItem)
                    .where(
                        col(UploadBatchItem.id) == job.item_id,
                        col(UploadBatchItem.batch_id) == job.batch_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if batch is None or item is None:
                return None
            ingestion: ArtifactIngestion | None = None
            if (
                item.status is not UploadBatchItemStatus.PROCESSING
                or item.worker_lease_id != job.lease_id
            ):
                if item.artifact_file_id is not None:
                    ingestion = (
                        await session.exec(
                            select(ArtifactIngestion).where(
                                col(ArtifactIngestion.artifact_file_id) == item.artifact_file_id
                            )
                        )
                    ).first()
                return _item_view(item, ingestion)

            if upload_result is not None and upload_result.ingestion_id is not None:
                ingestion = await session.get(ArtifactIngestion, upload_result.ingestion_id)
            elif item.artifact_file_id is not None:
                ingestion = (
                    await session.exec(
                        select(ArtifactIngestion).where(
                            col(ArtifactIngestion.artifact_file_id) == item.artifact_file_id
                        )
                    )
                ).first()
            failure_statuses = {
                ArtifactIngestionStatus.PENDING,
                ArtifactIngestionStatus.PROCESSING,
                ArtifactIngestionStatus.FAILED,
                ArtifactIngestionStatus.FILTERED,
            }
            failed = (
                error is not None
                or upload_result is None
                or upload_result.ingestion_status in failure_statuses
            )
            now = datetime.now(UTC)
            batch.processing_count = max(0, batch.processing_count - 1)
            item.worker_lease_id = None
            item.worker_lease_expires_at = None
            if (
                failed
                and ingestion is not None
                and ingestion.status
                in {ArtifactIngestionStatus.PENDING, ArtifactIngestionStatus.PROCESSING}
            ):
                ingestion.status = ArtifactIngestionStatus.FAILED
                ingestion.completed_at = now
                ingestion.worker_lease_id = None
                ingestion.worker_lease_expires_at = None
                ingestion.error_code = (
                    getattr(error, "error_code", None) if error is not None else "ingestion_failed"
                ) or "ingestion_failed"
                ingestion.error_message = (
                    str(error) or type(error).__name__
                    if error is not None
                    else "artifact processing failed"
                )
                session.add(ingestion)
            if upload_result is not None:
                item.artifact_file_id = upload_result.artifact_id
                item.parse_revision_id = upload_result.parse_revision_id
                item.materialization_status = ImportMaterializationStatus.SUCCEEDED.value
            if failed:
                item.status = UploadBatchItemStatus.FAILED
                item.parse_status = (
                    ImportParseStatus.FILTERED.value
                    if upload_result is not None
                    and upload_result.ingestion_status is ArtifactIngestionStatus.FILTERED
                    else ImportParseStatus.FAILED.value
                )
                batch.failed_count += 1
                item.error_code = (
                    getattr(error, "error_code", None)
                    if error is not None
                    else ingestion.error_code
                    if ingestion is not None
                    else "ingestion_failed"
                ) or "ingestion_failed"
                item.error_message = (
                    str(error) or type(error).__name__
                    if error is not None
                    else ingestion.error_message
                    if ingestion is not None
                    else "artifact processing failed"
                )
                phase = "failed"
            else:
                item.status = UploadBatchItemStatus.SUCCEEDED
                item.parse_status = (
                    ImportParseStatus.PARTIAL.value
                    if upload_result is not None
                    and upload_result.ingestion_status is ArtifactIngestionStatus.PARTIAL
                    else ImportParseStatus.SUCCEEDED.value
                )
                batch.succeeded_count += 1
                item.error_code = None
                item.error_message = None
                phase = "completed"
            item.metadata_json = _with_upload_progress(
                item.metadata_json,
                phase=phase,
                completed=1,
                total=1,
            )
            item.updated_at = now
            batch.updated_at = now
            _finish_batch_if_terminal(batch)
            session.add(item)
            session.add(batch)
            await session.commit()
            return _item_view(item, ingestion)

    @classmethod
    async def finish_processing_batch(
        cls,
        jobs: list[UploadProcessingJob],
        results: Mapping[UUID, object | None],
    ) -> int:
        """Publish one worker group with one set of locks and one commit.

        The streaming worker already persists a project/user group in one
        microbatch. Finalizing every one-file upload batch through
        ``finish_processing`` immediately afterwards used to reopen one
        transaction per file, which made a large reparse look stalled after
        its actual parse/write work had completed. Lock all involved rows in
        deterministic order and update them together instead.
        """

        if not jobs:
            return 0

        from tricycle_reaction_db.application.dtos import ArtifactUploadResult

        batch_ids = tuple(sorted({job.batch_id for job in jobs}, key=str))
        item_ids = tuple(sorted({job.item_id for job in jobs}, key=str))
        artifact_ids = tuple(sorted({job.artifact_file_id for job in jobs}, key=str))
        result_ingestion_ids = tuple(
            sorted(
                {
                    value.ingestion_id
                    for value in results.values()
                    if isinstance(value, ArtifactUploadResult) and value.ingestion_id is not None
                },
                key=str,
            )
        )
        async with session_factory() as session:
            batches = (
                await session.exec(
                    select(UploadBatch)
                    .where(col(UploadBatch.id).in_(batch_ids))
                    .order_by(col(UploadBatch.id))
                    .with_for_update()
                )
            ).all()
            items = (
                await session.exec(
                    select(UploadBatchItem)
                    .where(col(UploadBatchItem.id).in_(item_ids))
                    .order_by(col(UploadBatchItem.id))
                    .with_for_update()
                )
            ).all()
            ingestions = (
                await session.exec(
                    select(ArtifactIngestion)
                    .where(
                        or_(
                            col(ArtifactIngestion.artifact_file_id).in_(artifact_ids),
                            col(ArtifactIngestion.id).in_(result_ingestion_ids),
                        )
                    )
                    .order_by(col(ArtifactIngestion.id))
                    .with_for_update()
                )
            ).all()
            batches_by_id = {batch.id: batch for batch in batches}
            items_by_id = {item.id: item for item in items}
            ingestions_by_id = {ingestion.id: ingestion for ingestion in ingestions}
            ingestions_by_artifact_id = {
                ingestion.artifact_file_id: ingestion for ingestion in ingestions
            }
            failure_statuses = {
                ArtifactIngestionStatus.PENDING,
                ArtifactIngestionStatus.PROCESSING,
                ArtifactIngestionStatus.FAILED,
                ArtifactIngestionStatus.FILTERED,
            }
            now = datetime.now(UTC)
            finalized = 0
            item_ids_to_update: list[UUID] = []
            item_statuses: dict[UUID, UploadBatchItemStatus] = {}
            item_parse_statuses: dict[UUID, str] = {}
            item_materialization_statuses: dict[UUID, str] = {}
            item_artifact_ids: dict[UUID, UUID] = {}
            item_revision_ids: dict[UUID, UUID | None] = {}
            item_error_codes: dict[UUID, str | None] = {}
            item_error_messages: dict[UUID, str | None] = {}
            item_metadata: dict[UUID, dict[str, object]] = {}
            ingestion_error_codes: dict[UUID, str] = {}
            ingestion_error_messages: dict[UUID, str] = {}
            batch_deltas: dict[UUID, list[int]] = {}
            for job in jobs:
                batch = batches_by_id.get(job.batch_id)
                item = items_by_id.get(job.item_id)
                if (
                    batch is None
                    or item is None
                    or item.status is not UploadBatchItemStatus.PROCESSING
                    or item.worker_lease_id != job.lease_id
                ):
                    continue
                batch_id = _required_uuid(batch.id, "UploadBatch")
                item_id = _required_uuid(item.id, "UploadBatchItem")
                item_ids_to_update.append(item_id)

                raw_result = results.get(job.artifact_file_id)
                upload_result = raw_result if isinstance(raw_result, ArtifactUploadResult) else None
                error = raw_result if isinstance(raw_result, Exception) else None
                result_ingestion_id = (
                    upload_result.ingestion_id if upload_result is not None else None
                )
                if result_ingestion_id is not None:
                    ingestion = ingestions_by_id.get(result_ingestion_id)
                else:
                    ingestion = ingestions_by_artifact_id.get(job.artifact_file_id)
                failed = (
                    error is not None
                    or upload_result is None
                    or upload_result.ingestion_status in failure_statuses
                )
                delta = batch_deltas.setdefault(batch_id, [0, 0, 0])
                delta[0] -= 1  # processing_count
                if (
                    failed
                    and ingestion is not None
                    and ingestion.status
                    in {ArtifactIngestionStatus.PENDING, ArtifactIngestionStatus.PROCESSING}
                ):
                    ingestion_id = _required_uuid(ingestion.id, "ArtifactIngestion")
                    ingestion_error_codes[ingestion_id] = (
                        getattr(error, "error_code", None)
                        if error is not None
                        else "ingestion_failed"
                    ) or "ingestion_failed"
                    ingestion_error_messages[ingestion_id] = (
                        str(error) or type(error).__name__
                        if error is not None
                        else "artifact processing failed"
                    )
                if upload_result is not None:
                    item_artifact_ids[item_id] = upload_result.artifact_id
                    item_revision_ids[item_id] = upload_result.parse_revision_id
                    item_materialization_statuses[item_id] = (
                        ImportMaterializationStatus.SUCCEEDED.value
                    )
                if failed:
                    item_statuses[item_id] = UploadBatchItemStatus.FAILED
                    item_parse_statuses[item_id] = (
                        ImportParseStatus.FILTERED.value
                        if upload_result is not None
                        and upload_result.ingestion_status is ArtifactIngestionStatus.FILTERED
                        else ImportParseStatus.FAILED.value
                    )
                    delta[2] += 1  # failed_count
                    item_error_codes[item_id] = (
                        getattr(error, "error_code", None)
                        if error is not None
                        else ingestion.error_code
                        if ingestion is not None
                        else "ingestion_failed"
                    ) or "ingestion_failed"
                    item_error_messages[item_id] = (
                        str(error) or type(error).__name__
                        if error is not None
                        else ingestion.error_message
                        if ingestion is not None
                        else "artifact processing failed"
                    )
                    phase = "failed"
                else:
                    item_statuses[item_id] = UploadBatchItemStatus.SUCCEEDED
                    item_parse_statuses[item_id] = (
                        ImportParseStatus.PARTIAL.value
                        if upload_result is not None
                        and upload_result.ingestion_status is ArtifactIngestionStatus.PARTIAL
                        else ImportParseStatus.SUCCEEDED.value
                    )
                    delta[1] += 1  # succeeded_count
                    item_error_codes[item_id] = None
                    item_error_messages[item_id] = None
                    phase = "completed"
                item_metadata[item_id] = _with_upload_progress(
                    item.metadata_json,
                    phase=phase,
                    completed=1,
                    total=1,
                )
                finalized += 1

            if finalized:

                def item_case(column: object, values: Mapping[UUID, object]) -> object:
                    return case(values, value=col(UploadBatchItem.id), else_=column)

                def item_jsonb_case(
                    column: object,
                    values: Mapping[UUID, object],
                ) -> object:
                    return case(
                        {key: cast(value, JSONB) for key, value in values.items()},
                        value=col(UploadBatchItem.id),
                        else_=column,
                    )

                await session.exec(
                    update(UploadBatchItem)
                    .where(col(UploadBatchItem.id).in_(item_ids_to_update))
                    .values(
                        status=item_case(col(UploadBatchItem.status), item_statuses),
                        parse_status=item_case(
                            col(UploadBatchItem.parse_status), item_parse_statuses
                        ),
                        materialization_status=item_case(
                            col(UploadBatchItem.materialization_status),
                            item_materialization_statuses,
                        ),
                        artifact_file_id=item_case(
                            col(UploadBatchItem.artifact_file_id), item_artifact_ids
                        ),
                        parse_revision_id=item_case(
                            col(UploadBatchItem.parse_revision_id), item_revision_ids
                        ),
                        worker_lease_id=None,
                        worker_lease_expires_at=None,
                        error_code=item_case(col(UploadBatchItem.error_code), item_error_codes),
                        error_message=item_case(
                            col(UploadBatchItem.error_message), item_error_messages
                        ),
                        metadata_json=item_jsonb_case(
                            col(UploadBatchItem.metadata_json),
                            item_metadata,
                        ),
                        updated_at=now,
                    )
                )

                if ingestion_error_codes:

                    def ingestion_case(column: object, values: Mapping[UUID, object]) -> object:
                        return case(values, value=col(ArtifactIngestion.id), else_=column)

                    await session.exec(
                        update(ArtifactIngestion)
                        .where(col(ArtifactIngestion.id).in_(ingestion_error_codes))
                        .values(
                            status=ArtifactIngestionStatus.FAILED,
                            completed_at=now,
                            worker_lease_id=None,
                            worker_lease_expires_at=None,
                            error_code=ingestion_case(
                                col(ArtifactIngestion.error_code), ingestion_error_codes
                            ),
                            error_message=ingestion_case(
                                col(ArtifactIngestion.error_message), ingestion_error_messages
                            ),
                        )
                    )

                batch_by_id = {_required_uuid(batch.id, "UploadBatch"): batch for batch in batches}
                final_batch_ids = list(batch_deltas)
                batch_statuses: dict[UUID, UploadBatchStatus] = {}
                for batch_id in final_batch_ids:
                    batch = batch_by_id[batch_id]
                    processing_delta, succeeded_delta, failed_delta = batch_deltas[batch_id]
                    new_processing = max(0, batch.processing_count + processing_delta)
                    new_succeeded = batch.succeeded_count + succeeded_delta
                    new_failed = batch.failed_count + failed_delta
                    terminal = new_succeeded + new_failed + batch.cancelled_count
                    batch_statuses[batch_id] = (
                        UploadBatchStatus.COMPLETED
                        if (
                            batch.status is not UploadBatchStatus.CANCELLED
                            and batch.uploading_count == 0
                            and batch.staged_count == 0
                            and new_processing == 0
                            and terminal == batch.total_count
                        )
                        else batch.status
                    )
                await session.exec(
                    update(UploadBatch)
                    .where(col(UploadBatch.id).in_(final_batch_ids))
                    .values(
                        processing_count=case(
                            {
                                batch_id: max(
                                    0,
                                    batch_by_id[batch_id].processing_count
                                    + batch_deltas[batch_id][0],
                                )
                                for batch_id in final_batch_ids
                            },
                            value=col(UploadBatch.id),
                            else_=col(UploadBatch.processing_count),
                        ),
                        succeeded_count=case(
                            {
                                batch_id: batch_by_id[batch_id].succeeded_count
                                + batch_deltas[batch_id][1]
                                for batch_id in final_batch_ids
                            },
                            value=col(UploadBatch.id),
                            else_=col(UploadBatch.succeeded_count),
                        ),
                        failed_count=case(
                            {
                                batch_id: batch_by_id[batch_id].failed_count
                                + batch_deltas[batch_id][2]
                                for batch_id in final_batch_ids
                            },
                            value=col(UploadBatch.id),
                            else_=col(UploadBatch.failed_count),
                        ),
                        status=case(
                            batch_statuses,
                            value=col(UploadBatch.id),
                            else_=col(UploadBatch.status),
                        ),
                        updated_at=now,
                    )
                )
                await session.commit()
            return finalized


__all__ = [
    "PendingIngestionJob",
    "StagedUploadSubmission",
    "UploadBatchConflictError",
    "UploadBatchError",
    "UploadBatchLimitError",
    "UploadBatchNotFoundError",
    "UploadBatchService",
]
