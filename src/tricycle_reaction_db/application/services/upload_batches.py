"""Durable queue coordination for independently uploaded artifact files."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, update
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
from tricycle_reaction_db.db.models import UploadBatch, UploadBatchItem
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import (
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
class _UploadItemOutcome:
    client_file_id: UUID
    succeeded: bool
    artifact_file_id: UUID | None
    error_code: str | None
    error_message: str | None


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
    )


def _item_view(item: UploadBatchItem) -> UploadBatchItemView:
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
        artifact_file_id=item.artifact_file_id,
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
    async def recover_interrupted(batch_id: UUID, *, user_id: UUID) -> UploadBatchView:
        """Return files left uploading by a lost client request to the retry queue."""

        async with session_factory() as session:
            batch = await _owned_batch(session, batch_id, user_id, lock=True)
            if batch.status is UploadBatchStatus.CANCELLED:
                raise UploadBatchConflictError("cancelled upload batches cannot be recovered")
            if batch.status is UploadBatchStatus.COMPLETED:
                raise UploadBatchConflictError("completed upload batches cannot be recovered")
            now = datetime.now(UTC)
            interrupted_count = int(
                (
                    await session.exec(
                        select(func.count())
                        .select_from(UploadBatchItem)
                        .where(
                            col(UploadBatchItem.batch_id) == batch_id,
                            col(UploadBatchItem.status) == UploadBatchItemStatus.UPLOADING,
                        )
                    )
                ).one()
            )
            if interrupted_count:
                await session.execute(
                    update(UploadBatchItem)
                    .where(
                        col(UploadBatchItem.batch_id) == batch_id,
                        col(UploadBatchItem.status) == UploadBatchItemStatus.UPLOADING,
                    )
                    .values(
                        status=UploadBatchItemStatus.QUEUED,
                        error_code="upload_interrupted",
                        error_message="上传请求中断，文件已返回等待队列",
                        updated_at=now,
                    )
                )
                batch.uploading_count -= interrupted_count
            batch.status = UploadBatchStatus.ACTIVE
            batch.updated_at = now
            session.add(batch)
            await session.commit()
            await session.refresh(batch)
            return _batch_view(batch)

    @staticmethod
    async def list_items(
        batch_id: UUID,
        *,
        user_id: UUID,
        status: UploadBatchItemStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> UploadBatchItemPage:
        async with session_factory() as session:
            await _owned_batch(session, batch_id, user_id)
            criteria = [col(UploadBatchItem.batch_id) == batch_id]
            if status is not None:
                criteria.append(col(UploadBatchItem.status) == status)
            total = int(
                (
                    await session.exec(
                        select(func.count()).select_from(UploadBatchItem).where(*criteria)
                    )
                ).one()
            )
            items = (
                await session.exec(
                    select(UploadBatchItem)
                    .where(*criteria)
                    .order_by(col(UploadBatchItem.position), col(UploadBatchItem.id))
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
        return UploadBatchItemPage(
            items=[_item_view(item) for item in items],
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

    @staticmethod
    async def cancel(batch_id: UUID, *, user_id: UUID) -> UploadBatchView:
        async with session_factory() as session:
            batch = await _owned_batch(session, batch_id, user_id, lock=True)
            if batch.status is UploadBatchStatus.CANCELLED:
                return _batch_view(batch)
            if batch.status is UploadBatchStatus.COMPLETED:
                raise UploadBatchConflictError("completed upload batches cannot be cancelled")
            now = datetime.now(UTC)
            queued_count = int(
                (
                    await session.exec(
                        select(func.count())
                        .select_from(UploadBatchItem)
                        .where(
                            col(UploadBatchItem.batch_id) == batch_id,
                            col(UploadBatchItem.status) == UploadBatchItemStatus.QUEUED,
                        )
                    )
                ).one()
            )
            await session.execute(
                update(UploadBatchItem)
                .where(
                    col(UploadBatchItem.batch_id) == batch_id,
                    col(UploadBatchItem.status) == UploadBatchItemStatus.QUEUED,
                )
                .values(status=UploadBatchItemStatus.CANCELLED, updated_at=now)
            )
            batch.cancelled_count += queued_count
            batch.status = UploadBatchStatus.CANCELLED
            batch.updated_at = now
            session.add(batch)
            await session.commit()
            await session.refresh(batch)
            return _batch_view(batch)

    @staticmethod
    async def retry_failed(batch_id: UUID, *, user_id: UUID) -> UploadBatchView:
        async with session_factory() as session:
            batch = await _owned_batch(session, batch_id, user_id, lock=True)
            if batch.status is UploadBatchStatus.CANCELLED:
                raise UploadBatchConflictError("cancelled upload batches cannot be retried")
            now = datetime.now(UTC)
            failed_count = int(
                (
                    await session.exec(
                        select(func.count())
                        .select_from(UploadBatchItem)
                        .where(
                            col(UploadBatchItem.batch_id) == batch_id,
                            col(UploadBatchItem.status) == UploadBatchItemStatus.FAILED,
                        )
                    )
                ).one()
            )
            if failed_count:
                await session.execute(
                    update(UploadBatchItem)
                    .where(
                        col(UploadBatchItem.batch_id) == batch_id,
                        col(UploadBatchItem.status) == UploadBatchItemStatus.FAILED,
                    )
                    .values(
                        status=UploadBatchItemStatus.QUEUED,
                        error_code=None,
                        error_message=None,
                        updated_at=now,
                    )
                )
                batch.failed_count -= failed_count
            batch.status = UploadBatchStatus.ACTIVE
            batch.updated_at = now
            session.add(batch)
            await session.commit()
            await session.refresh(batch)
            return _batch_view(batch)

    @staticmethod
    async def retry_item(
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
            now = datetime.now(UTC)
            item.status = UploadBatchItemStatus.QUEUED
            item.error_code = None
            item.error_message = None
            item.updated_at = now
            batch.failed_count -= 1
            batch.status = UploadBatchStatus.ACTIVE
            batch.updated_at = now
            session.add(item)
            session.add(batch)
            await session.commit()
            await session.refresh(item)
            return _item_view(item)

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
        """Upload one request group and parse its calculation files as one MolOP batch."""

        if not files:
            raise UploadBatchConflictError("upload batch request contains no files")
        client_file_ids = [client_file_id for client_file_id, _ in files]
        if len(set(client_file_ids)) != len(client_file_ids):
            raise UploadBatchConflictError("client_file_id must be unique within an upload request")

        now = datetime.now(UTC)
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
            has_pending = any(
                items_by_client_id[client_file_id].status is not UploadBatchItemStatus.SUCCEEDED
                for client_file_id in client_file_ids
            )
            if batch.status is UploadBatchStatus.COMPLETED and has_pending:
                batch.status = UploadBatchStatus.ACTIVE
            elif batch.status is not UploadBatchStatus.ACTIVE and has_pending:
                raise UploadBatchConflictError("upload batch is not active")
            await AuthorizationService.require_project_permission(
                user_id,
                batch.project_id,
                ProjectPermission.ARTIFACT_UPLOAD,
            )

            pending_files: list[tuple[UUID, ArtifactUploadPayload]] = []
            completed_items: dict[UUID, UploadBatchItemView] = {}
            for client_file_id, upload in files:
                item = items_by_client_id[client_file_id]
                payload_size = (
                    len(upload.payload)
                    if upload.payload is not None
                    else upload.spool_path.stat().st_size
                    if upload.spool_path is not None
                    else -1
                )
                if item.original_filename != upload.filename or item.size_bytes != payload_size:
                    raise UploadBatchConflictError(
                        "uploaded file does not match the filename and size reserved by "
                        "client_file_id"
                    )
                if item.status is UploadBatchItemStatus.SUCCEEDED:
                    completed_items[client_file_id] = _item_view(item)
                    continue
                if item.status is UploadBatchItemStatus.UPLOADING:
                    raise UploadBatchConflictError("upload batch file is already being processed")
                if item.status is UploadBatchItemStatus.CANCELLED:
                    raise UploadBatchConflictError("cancelled upload batch files cannot be retried")
                if item.status is UploadBatchItemStatus.FAILED:
                    batch.failed_count -= 1
                resolved_media_type = _upload_media_type(
                    upload,
                    item.original_filename,
                    item.media_type,
                )
                item.media_type = resolved_media_type
                item.status = UploadBatchItemStatus.UPLOADING
                item.attempt_count += 1
                item.error_code = None
                item.error_message = None
                item.metadata_json = _with_upload_progress(
                    item.metadata_json,
                    phase="uploading",
                    completed=0,
                    total=0,
                )
                item.updated_at = now
                session.add(item)
                pending_files.append(
                    (
                        client_file_id,
                        ArtifactUploadPayload(
                            filename=item.original_filename,
                            media_type=resolved_media_type,
                            payload=upload.payload,
                            spool_path=upload.spool_path,
                        ),
                    )
                )

            for pending_client_file_id, _ in pending_files:
                item = items_by_client_id[pending_client_file_id]
                item.metadata_json = _with_upload_progress(
                    item.metadata_json,
                    phase="parsing",
                    completed=0,
                    total=len(pending_files),
                )
                session.add(item)
            batch.uploading_count += len(pending_files)
            batch.updated_at = now
            session.add(batch)
            artifact_kind = batch.artifact_kind
            project_id = batch.project_id
            await session.commit()

        if not pending_files:
            return [completed_items[client_file_id] for client_file_id in client_file_ids]

        try:
            result = await ArtifactUploadService.upload_batch(
                files=[upload for _, upload in pending_files],
                artifact_kind=artifact_kind,
                project_id=project_id,
                user_id=user_id,
            )
        except asyncio.CancelledError:
            await cls._finish_items(
                batch_id,
                outcomes=[
                    _UploadItemOutcome(
                        client_file_id=client_file_id,
                        succeeded=False,
                        artifact_file_id=None,
                        error_code="upload_request_cancelled",
                        error_message="upload request was cancelled before completion",
                    )
                    for client_file_id, _ in pending_files
                ],
                user_id=user_id,
            )
            raise
        except Exception as error:
            await cls._finish_items(
                batch_id,
                outcomes=[
                    _UploadItemOutcome(
                        client_file_id=client_file_id,
                        succeeded=False,
                        artifact_file_id=None,
                        error_code="artifact_upload_failed",
                        error_message=str(error) or type(error).__name__,
                    )
                    for client_file_id, _ in pending_files
                ],
                user_id=user_id,
            )
            raise

        if len(result.items) != len(pending_files):
            mismatch_error = RuntimeError(
                "artifact batch result count does not match the upload request"
            )
            await cls._finish_items(
                batch_id,
                outcomes=[
                    _UploadItemOutcome(
                        client_file_id=client_file_id,
                        succeeded=False,
                        artifact_file_id=None,
                        error_code="artifact_batch_result_mismatch",
                        error_message=str(mismatch_error),
                    )
                    for client_file_id, _ in pending_files
                ],
                user_id=user_id,
            )
            raise mismatch_error
        finished = await cls._finish_items(
            batch_id,
            outcomes=[
                _UploadItemOutcome(
                    client_file_id=client_file_id,
                    succeeded=item.succeeded,
                    artifact_file_id=item.result.artifact_id if item.result is not None else None,
                    error_code=item.error_code,
                    error_message=item.error_message,
                )
                for (client_file_id, _), item in zip(pending_files, result.items, strict=True)
            ],
            user_id=user_id,
        )
        finished_by_client_id = {item.client_file_id: item for item in finished}
        finished_by_client_id.update(completed_items)
        return [finished_by_client_id[client_file_id] for client_file_id in client_file_ids]

    @staticmethod
    async def _finish_items(
        batch_id: UUID,
        *,
        outcomes: list[_UploadItemOutcome],
        user_id: UUID,
    ) -> list[UploadBatchItemView]:
        client_file_ids = [outcome.client_file_id for outcome in outcomes]
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
            now = datetime.now(UTC)
            for outcome in outcomes:
                item = items_by_client_id.get(outcome.client_file_id)
                if item is None:
                    raise UploadBatchNotFoundError("upload batch file not found")
                if item.status is not UploadBatchItemStatus.UPLOADING:
                    continue
                batch.uploading_count -= 1
                if outcome.succeeded:
                    item.status = UploadBatchItemStatus.SUCCEEDED
                    batch.succeeded_count += 1
                else:
                    item.status = UploadBatchItemStatus.FAILED
                    batch.failed_count += 1
                item.artifact_file_id = outcome.artifact_file_id
                item.error_code = outcome.error_code
                item.error_message = outcome.error_message
                item.metadata_json = _with_upload_progress(
                    item.metadata_json,
                    phase="completed" if outcome.succeeded else "failed",
                    completed=None,
                    total=None,
                )
                item.updated_at = now
                session.add(item)
            batch.updated_at = now
            _finish_batch_if_terminal(batch)
            session.add(batch)
            await session.commit()
            return [
                _item_view(items_by_client_id[client_file_id]) for client_file_id in client_file_ids
            ]


__all__ = [
    "UploadBatchConflictError",
    "UploadBatchError",
    "UploadBatchLimitError",
    "UploadBatchNotFoundError",
    "UploadBatchService",
]
