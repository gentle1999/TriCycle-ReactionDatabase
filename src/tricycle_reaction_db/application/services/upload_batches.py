"""Durable queue coordination for independently uploaded artifact files."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from tricycle_reaction_db.application.dtos import (
    UploadBatchCreate,
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

    batch_id: UUID
    item_id: UUID
    client_file_id: UUID
    artifact_file_id: UUID
    user_id: UUID
    lease_id: UUID


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
        artifact_file_id=item.artifact_file_id,
        ingestion_status=ingestion.status if ingestion is not None else ingestion_status,
        ingestion_error_message=(
            ingestion.error_message if ingestion is not None else ingestion_error_message
        ),
        error_code=item.error_code,
        error_message=item.error_message,
        metadata=item.metadata_json,
    )


async def _owned_batch(
    session: AsyncSession,
    batch_id: UUID,
    user_id: UUID,
    *,
    lock: bool = False,
) -> UploadBatch:
    statement = select(UploadBatch).where(
        col(UploadBatch.id) == batch_id,
        col(UploadBatch.created_by_user_id) == user_id,
    )
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


class UploadBatchService:
    """Manage a server-visible queue without combining file bodies into one request."""

    @staticmethod
    async def create(payload: UploadBatchCreate, *, user_id: UUID) -> UploadBatchView:
        settings = get_settings()
        if len(payload.files) > settings.max_upload_queue_files:
            raise UploadBatchLimitError(
                f"upload queue exceeds the {settings.max_upload_queue_files}-file limit"
            )
        oversized = next(
            (item for item in payload.files if item.size_bytes > settings.max_upload_bytes),
            None,
        )
        if oversized is not None:
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
            batch = UploadBatch(
                project_id=payload.project_id,
                created_by_user_id=user_id,
                artifact_kind=payload.artifact_kind,
                status=UploadBatchStatus.ACTIVE,
                shared_metadata=payload.shared_metadata,
                total_count=len(payload.files),
                total_bytes=total_bytes,
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
                        metadata_json=payload.shared_metadata,
                        updated_at=now,
                    )
                    for position, file in enumerate(payload.files)
                ]
            )
            await session.commit()
            await session.refresh(batch)
            return _batch_view(batch)

    @staticmethod
    async def list_batches(
        *,
        user_id: UUID,
        project_id: UUID | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> UploadBatchPage:
        criteria = [col(UploadBatch.created_by_user_id) == user_id]
        if project_id is not None:
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
    async def get(batch_id: UUID, *, user_id: UUID) -> UploadBatchView:
        async with session_factory() as session:
            return _batch_view(await _owned_batch(session, batch_id, user_id))

    @staticmethod
    async def list_items(
        batch_id: UUID,
        *,
        user_id: UUID,
        status: UploadBatchItemStatus | None = None,
        updated_after: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> UploadBatchItemPage:
        async with session_factory() as session:
            await _owned_batch(session, batch_id, user_id)
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
                        select(ArtifactIngestion).where(
                            col(ArtifactIngestion.artifact_file_id).in_(artifact_ids)
                        )
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
        if upload.payload is not None:
            return len(upload.payload)
        if upload.spool_path is not None:
            return upload.spool_path.stat().st_size
        return -1

    @staticmethod
    def _payload_bytes(upload: ArtifactUploadPayload) -> bytes:
        if upload.payload is not None:
            return upload.payload
        if upload.spool_path is not None:
            return upload.spool_path.read_bytes()
        raise UploadBatchConflictError("uploaded artifact has no payload")

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
                and (item.content_sha256 is None or artifact.content_sha256 == item.content_sha256)
                and artifact.storage_status is StorageStatus.AVAILABLE
            ):
                return artifact
        if item.content_sha256 is None:
            return None
        return (
            await session.exec(
                select(ArtifactFile)
                .where(
                    col(ArtifactFile.project_id) == batch.project_id,
                    col(ArtifactFile.content_sha256) == item.content_sha256,
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
                item.status = UploadBatchItemStatus.CANCELLED
                batch.cancelled_count += 1
                item.error_code = None
                item.error_message = None
                progress_phase = "cancelled"
            elif error is not None or upload_result is None:
                item.artifact_file_id = None
                ingestion = None
                item.status = UploadBatchItemStatus.FAILED
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
                if upload_result.ingestion_id is not None:
                    ingestion = await session.get(ArtifactIngestion, upload_result.ingestion_id)
                ingestion_status = upload_result.ingestion_status
                if ingestion_status is ArtifactIngestionStatus.PENDING:
                    item.status = UploadBatchItemStatus.STAGED
                    batch.staged_count += 1
                    item.error_code = None
                    item.error_message = None
                    progress_phase = "staged"
                elif ingestion_status in {
                    ArtifactIngestionStatus.FAILED,
                    ArtifactIngestionStatus.FILTERED,
                }:
                    item.status = UploadBatchItemStatus.FAILED
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
        total_bytes = sum(max(0, cls._payload_size(upload)) for _, upload in files)
        if total_bytes > settings.max_batch_bytes:
            raise UploadBatchLimitError(
                f"upload batch exceeds the {settings.max_batch_bytes}-byte limit"
            )

        now = datetime.now(UTC)
        content_sha256_by_client_id = {
            client_file_id: cls._payload_sha256(upload) for client_file_id, upload in files
        }
        pending: list[tuple[UUID, ArtifactUploadPayload, str]] = []
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
                item.content_sha256 = content_sha256_by_client_id[client_file_id]
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
                pending.append((client_file_id, upload, resolved_media_type))
                session.add(item)

            if pending_count:
                if batch.status is UploadBatchStatus.COMPLETED:
                    batch.status = UploadBatchStatus.ACTIVE
                batch.updated_at = now
                session.add(batch)
            artifact_kind = batch.artifact_kind
            project_id = batch.project_id
            await session.commit()

        # Each stage operation is independent.  A failed object store request
        # becomes one failed queue item, while all other files remain usable.
        for client_file_id, upload, resolved_media_type in pending:
            try:
                staged = await ArtifactUploadService.stage(
                    payload=cls._payload_bytes(upload),
                    filename=upload.filename,
                    media_type=resolved_media_type,
                    artifact_kind=artifact_kind,
                    project_id=project_id,
                    user_id=user_id,
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
                views_by_client_id[client_file_id] = finished
            else:
                views_by_client_id[client_file_id] = await cls._finish_staged_item(
                    batch_id,
                    client_file_id,
                    user_id=user_id,
                    result=staged,
                )

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
                    batch.staged_count += 1
                    phase = "staged"
                else:
                    item.status = UploadBatchItemStatus.QUEUED
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
                batch.staged_count += 1
                phase = "staged"
            else:
                item.status = UploadBatchItemStatus.QUEUED
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
    async def recover_stale(cls, *, limit: int = 100) -> int:
        """Recover leases left by a crashed API or worker process."""

        settings = get_settings()
        now = datetime.now(UTC)
        upload_cutoff = now - timedelta(seconds=settings.upload_client_lease_seconds)
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
                    and_(
                        col(UploadBatchItem.status) == UploadBatchItemStatus.PROCESSING,
                        or_(
                            col(UploadBatchItem.worker_lease_expires_at).is_(None),
                            col(UploadBatchItem.worker_lease_expires_at) <= now,
                        ),
                    ),
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
            if recovered:
                await session.commit()
            return recovered

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
                batch.cancelled_count += 1
                phase = "cancelled"
                error_code = "upload_cancelled"
                error_message = "批次已取消，服务端已停止处理此文件"
            elif await UploadBatchService._link_recovered_artifact(session, batch, item):
                item.status = UploadBatchItemStatus.STAGED
                batch.staged_count += 1
                phase = "staged"
                error_code = "worker_interrupted"
                error_message = "处理进程中断，文件仍保留在服务端等待重试"
            elif was_processing:
                item.status = UploadBatchItemStatus.FAILED
                batch.failed_count += 1
                phase = "failed"
                error_code = "processing_artifact_unavailable"
                error_message = "处理租约失效且服务端文件不可用"
            else:
                item.status = UploadBatchItemStatus.QUEUED
                phase = "queued"
                error_code = "upload_interrupted"
                error_message = "上传请求中断，文件已返回等待队列"
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
            for item, batch in rows:
                batch_id = _required_uuid(batch.id, "UploadBatch")
                item_id = _required_uuid(item.id, "UploadBatchItem")
                if item.artifact_file_id is None:
                    item.status = UploadBatchItemStatus.FAILED
                    item.error_code = "staged_artifact_missing"
                    item.error_message = "staged queue item has no artifact reference"
                    item.metadata_json = _with_upload_progress(
                        item.metadata_json,
                        phase="failed",
                        completed=1,
                        total=1,
                    )
                    item.updated_at = now
                    batch.staged_count = max(0, batch.staged_count - 1)
                    batch.failed_count += 1
                    session.add(item)
                    session.add(batch)
                    _finish_batch_if_terminal(batch)
                    continue
                lease_id = uuid4()
                item.status = UploadBatchItemStatus.PROCESSING
                item.processing_attempt_count += 1
                item.worker_lease_id = lease_id
                item.worker_lease_expires_at = now + timedelta(
                    seconds=settings.upload_worker_lease_seconds
                )
                item.error_code = None
                item.error_message = None
                item.metadata_json = _with_upload_progress(
                    item.metadata_json,
                    phase="processing",
                    completed=0,
                    total=1,
                )
                item.updated_at = now
                batch.staged_count = max(0, batch.staged_count - 1)
                batch.processing_count += 1
                batch.updated_at = now
                session.add(item)
                session.add(batch)
                jobs.append(
                    UploadProcessingJob(
                        batch_id=batch_id,
                        item_id=item_id,
                        client_file_id=item.client_file_id,
                        artifact_file_id=item.artifact_file_id,
                        user_id=batch.created_by_user_id,
                        lease_id=lease_id,
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
            item.worker_lease_expires_at = datetime.now(UTC) + timedelta(
                seconds=settings.upload_worker_lease_seconds
            )
            item.updated_at = datetime.now(UTC)
            session.add(item)
            await session.commit()
            return True

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
            if upload_result is not None:
                item.artifact_file_id = upload_result.artifact_id
            if failed:
                item.status = UploadBatchItemStatus.FAILED
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


__all__ = [
    "UploadBatchConflictError",
    "UploadBatchError",
    "UploadBatchLimitError",
    "UploadBatchNotFoundError",
    "UploadBatchService",
]
