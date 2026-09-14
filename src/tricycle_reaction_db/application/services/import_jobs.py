"""Manifest registration and control operations for durable import jobs."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlmodel import col, select

from tricycle_reaction_db.application.dtos import (
    UploadBatchCreate,
    UploadBatchFileCreate,
    UploadBatchItemPage,
    UploadBatchStatusUpdate,
    UploadBatchView,
)
from tricycle_reaction_db.application.services.artifact_uploads import ArtifactUploadPayload
from tricycle_reaction_db.application.services.audit import AuditService
from tricycle_reaction_db.application.services.authorization import (
    AuthorizationService,
    ProjectPermission,
)
from tricycle_reaction_db.application.services.upload_batches import (
    UploadBatchConflictError,
    UploadBatchService,
    _batch_view,
    _finish_batch_if_terminal,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import UploadBatch, UploadBatchItem
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    ImportMaterializationStatus,
    ImportParseStatus,
    ImportSelectionStatus,
    UploadBatchItemStatus,
    UploadBatchStatus,
)
from tricycle_reaction_db.ingestion.manifest import (
    ArtifactManifest,
    ManifestError,
    fingerprint_staged_file,
    validate_manifest_staging,
)


class ImportJobError(RuntimeError):
    """Base error for manifest import controls."""


class ImportJobNotFoundError(ImportJobError):
    pass


class ImportJobConflictError(ImportJobError):
    pass


def _required_id(value: UUID | None, label: str) -> UUID:
    if value is None:
        raise RuntimeError(f"persisted {label} is missing its UUID")
    return value


def _client_file_id(
    manifest: ArtifactManifest,
    *,
    project_id: UUID,
    relative_path: str,
    file_sha256: str,
) -> UUID:
    """Derive a stable queue key from the manifest idempotency tuple."""

    return uuid5(
        NAMESPACE_URL,
        json.dumps(
            [
                "tricycle-import",
                str(project_id),
                manifest.archive_sha256,
                relative_path,
                file_sha256,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )


async def _owned_import_job(
    job_id: UUID,
    *,
    user_id: UUID,
    lock: bool = False,
) -> UploadBatch:
    statement = select(UploadBatch).where(
        col(UploadBatch.id) == job_id,
        col(UploadBatch.created_by_user_id) == user_id,
        col(UploadBatch.manifest_sha256).is_not(None),
    )
    if lock:
        statement = statement.with_for_update()
    async with session_factory() as session:
        job = (await session.exec(statement)).one_or_none()
    if job is None:
        raise ImportJobNotFoundError("import job not found")
    return job


class ImportJobService:
    """Use the existing UploadBatch leases as the canonical ImportJob queue."""

    @classmethod
    async def register_manifest(
        cls,
        manifest: ArtifactManifest,
        *,
        project_id: UUID,
        user_id: UUID,
        artifact_kind: ArtifactKind = ArtifactKind.CALCULATION_OUTPUT,
    ) -> UploadBatchView:
        settings = get_settings()
        if settings.import_staging_root is None:
            raise ImportJobConflictError(
                "import_staging_root is not configured; manifest controls are disabled"
            )
        # Authorize before touching operator-supplied staging paths.  This
        # keeps path existence/security details from becoming a side channel
        # for users without project upload permission.
        await AuthorizationService.require_project_permission(
            user_id,
            project_id,
            ProjectPermission.ARTIFACT_UPLOAD,
        )
        try:
            validate_manifest_staging(manifest, staging_root=settings.import_staging_root)
        except ManifestError as error:
            raise ImportJobConflictError(str(error)) from error
        manifest_sha256 = manifest.content_sha256()
        files = [
            UploadBatchFileCreate(
                client_file_id=_client_file_id(
                    manifest,
                    project_id=project_id,
                    relative_path=entry.relative_path,
                    file_sha256=entry.file_sha256,
                ),
                original_filename=Path(entry.relative_path).name,
                relative_path=entry.relative_path,
                size_bytes=entry.size_bytes,
                media_type=entry.media_type,
                expected_file_sha256=entry.file_sha256,
                staged_file_path=entry.staged_file_path,
                is_gaussian_log=entry.is_gaussian_log,
                selection_status=ImportSelectionStatus(entry.selection_status),
            )
            for entry in manifest.entries
        ]
        try:
            view = await UploadBatchService.create(
                UploadBatchCreate(
                    project_id=project_id,
                    artifact_kind=artifact_kind,
                    archive_sha256=manifest.archive_sha256,
                    manifest_sha256=manifest_sha256,
                    manifest_schema_version=manifest.schema_version,
                    shared_metadata={
                        "manifest_schema_version": manifest.schema_version,
                        "manifest_sha256": manifest_sha256,
                    },
                    files=files,
                ),
                user_id=user_id,
                manifest_registration=True,
            )
        except UploadBatchConflictError:
            raise
        await AuditService.record(
            action="import_job.registered",
            entity_type="import_job",
            entity_id=view.id,
            actor_user_id=user_id,
            project_id=project_id,
            metadata={
                "archive_sha256": manifest.archive_sha256,
                "manifest_sha256": manifest_sha256,
                "entry_count": len(manifest.entries),
                "selected_entry_count": sum(
                    entry.selection_status == "selected" for entry in manifest.entries
                ),
            },
        )
        return view

    @staticmethod
    async def _record_start_failures(
        job_id: UUID,
        failures: list[tuple[UUID, str, str, str | None]],
        *,
        user_id: UUID,
    ) -> None:
        if not failures:
            return
        async with session_factory() as session:
            job = (
                await session.exec(
                    select(UploadBatch)
                    .where(
                        col(UploadBatch.id) == job_id,
                        col(UploadBatch.created_by_user_id) == user_id,
                        col(UploadBatch.manifest_sha256).is_not(None),
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if job is None:
                raise ImportJobNotFoundError("import job not found")
            item_ids = [item_id for item_id, _, _, _ in failures]
            items = (
                await session.exec(
                    select(UploadBatchItem)
                    .where(
                        col(UploadBatchItem.batch_id) == job_id,
                        col(UploadBatchItem.id).in_(item_ids),
                    )
                    .with_for_update()
                )
            ).all()
            failures_by_id = {
                item_id: (code, message, actual_sha256)
                for item_id, code, message, actual_sha256 in failures
            }
            now = datetime.now(UTC)
            for item in items:
                if item.status is not UploadBatchItemStatus.QUEUED:
                    continue
                item_id = _required_id(item.id, "UploadBatchItem")
                code, message, actual_sha256 = failures_by_id[item_id]
                item.status = UploadBatchItemStatus.FAILED
                item.parse_status = ImportParseStatus.FAILED.value
                item.materialization_status = ImportMaterializationStatus.FAILED.value
                item.error_code = code
                item.error_message = message
                item.content_sha256 = actual_sha256
                item.updated_at = now
                job.failed_count += 1
                session.add(item)
            job.updated_at = now
            _finish_batch_if_terminal(job)
            session.add(job)
            await session.commit()

    @classmethod
    async def start(cls, job_id: UUID, *, user_id: UUID) -> UploadBatchView:
        settings = get_settings()
        if settings.import_staging_root is None:
            raise ImportJobConflictError(
                "import_staging_root is not configured; manifest controls are disabled"
            )
        job = await _owned_import_job(job_id, user_id=user_id)
        if job.status in {UploadBatchStatus.CANCELLED, UploadBatchStatus.COMPLETED}:
            return _batch_view(job)
        if job.status is UploadBatchStatus.PAUSED:
            raise ImportJobConflictError("paused import jobs must be resumed before starting")

        async with session_factory() as session:
            items = list(
                (
                    await session.exec(
                        select(UploadBatchItem)
                        .where(
                            col(UploadBatchItem.batch_id) == job_id,
                            col(UploadBatchItem.status) == UploadBatchItemStatus.QUEUED,
                            col(UploadBatchItem.selection_status)
                            == ImportSelectionStatus.SELECTED.value,
                        )
                        .order_by(col(UploadBatchItem.position))
                    )
                ).all()
            )

        payloads: list[tuple[UUID, ArtifactUploadPayload]] = []
        start_failures: list[tuple[UUID, str, str, str | None]] = []
        for item in items:
            if item.staged_file_path is None:
                start_failures.append(
                    (
                        _required_id(item.id, "UploadBatchItem"),
                        "manifest_file_unavailable",
                        f"manifest item {item.relative_path} has no staged_file_path",
                        None,
                    )
                )
                continue
            try:
                path, size, digest = fingerprint_staged_file(
                    item.staged_file_path,
                    staging_root=settings.import_staging_root,
                )
            except ManifestError as error:
                failure_code = (
                    "manifest_file_unavailable"
                    if not os.path.lexists(item.staged_file_path)
                    else "manifest_file_security_violation"
                )
                start_failures.append(
                    (
                        _required_id(item.id, "UploadBatchItem"),
                        failure_code,
                        f"{item.relative_path}: {error}",
                        None,
                    )
                )
                continue
            if (
                item.expected_file_sha256 is not None and digest != item.expected_file_sha256
            ) or size != item.size_bytes:
                start_failures.append(
                    (
                        _required_id(item.id, "UploadBatchItem"),
                        "manifest_file_changed",
                        f"manifest file identity mismatch for {item.relative_path}: "
                        f"expected {item.expected_file_sha256}/{item.size_bytes}, "
                        f"got {digest}/{size}",
                        digest,
                    )
                )
                continue
            payloads.append(
                (
                    _required_id(item.id, "UploadBatchItem"),
                    ArtifactUploadPayload(
                        filename=item.original_filename,
                        media_type=item.media_type,
                        payload=None,
                        spool_path=path,
                        relative_path=item.relative_path,
                        expected_sha256=item.expected_file_sha256,
                        expected_size_bytes=item.size_bytes,
                    ),
                )
            )

        await cls._record_start_failures(job_id, start_failures, user_id=user_id)

        # UploadBatchService is deliberately bounded by the HTTP batch budget;
        # a manifest may be much larger, so stage it in bounded chunks while
        # retaining one durable item per manifest entry.
        max_files = settings.max_batch_files
        max_bytes = settings.max_batch_bytes
        offset = 0
        while offset < len(payloads):
            chunk: list[tuple[UUID, ArtifactUploadPayload]] = []
            chunk_bytes = 0
            for pair in payloads[offset:]:
                size = pair[1].expected_size_bytes or 0
                if size > max_bytes:
                    raise ImportJobConflictError(
                        f"manifest item exceeds the {max_bytes}-byte batch budget"
                    )
                if chunk and chunk_bytes + size > max_bytes:
                    break
                chunk.append(pair)
                chunk_bytes += size
                if len(chunk) >= max_files:
                    break
            if not chunk:
                raise ImportJobConflictError("manifest item exceeds the batch byte budget")
            # The queue API addresses items by client_file_id.  ``payloads``
            # stores the persisted item UUID only to make accidental reorder
            # impossible; resolve the stable client key in the same read.
            async with session_factory() as session:
                ids = [item_id for item_id, _ in chunk]
                rows = (
                    await session.exec(
                        select(UploadBatchItem).where(col(UploadBatchItem.id).in_(ids))
                    )
                ).all()
            client_ids = {row.id: row.client_file_id for row in rows}
            await UploadBatchService.upload_items(
                job_id,
                files=[
                    (client_ids[item_id], payload)
                    for item_id, payload in chunk
                    if item_id in client_ids
                ],
                user_id=user_id,
            )
            offset += len(chunk)

        refreshed = await _owned_import_job(job_id, user_id=user_id)
        view = _batch_view(refreshed)
        await AuditService.record(
            action="import_job.started",
            entity_type="import_job",
            entity_id=view.id,
            actor_user_id=user_id,
            project_id=view.project_id,
            metadata={
                "selected_item_count": len(payloads),
                "failed_item_count": len(start_failures),
            },
        )
        return view

    @classmethod
    async def status(cls, job_id: UUID, *, user_id: UUID) -> dict[str, Any]:
        job = await _owned_import_job(job_id, user_id=user_id)
        items = await UploadBatchService.list_items(
            job_id,
            user_id=user_id,
            project_id=job.project_id,
            limit=100_000,
        )
        return {"job": _batch_view(job), "items": items}

    @classmethod
    async def failures(cls, job_id: UUID, *, user_id: UUID) -> UploadBatchItemPage:
        job = await _owned_import_job(job_id, user_id=user_id)
        return await UploadBatchService.list_items(
            job_id,
            user_id=user_id,
            project_id=job.project_id,
            status=UploadBatchItemStatus.FAILED,
            limit=100_000,
        )

    @classmethod
    async def retry(cls, job_id: UUID, *, user_id: UUID) -> UploadBatchView:
        return await cls.retry_items(job_id, item_ids=None, user_id=user_id)

    @classmethod
    async def retry_items(
        cls,
        job_id: UUID,
        *,
        item_ids: list[UUID] | None,
        user_id: UUID,
    ) -> UploadBatchView:
        """Retry only the requested failed manifest items.

        ``item_ids`` are database item IDs exposed by the status endpoint, not
        filesystem paths or client-controlled server locations.  Omitting the
        list preserves the existing retry-all-failed operation.
        """

        await _owned_import_job(job_id, user_id=user_id)
        if item_ids is None:
            await UploadBatchService.retry_failed(job_id, user_id=user_id)
            view = await cls.start(job_id, user_id=user_id)
            await AuditService.record(
                action="import_job.items_retried",
                entity_type="import_job",
                entity_id=view.id,
                actor_user_id=user_id,
                project_id=view.project_id,
                metadata={"item_ids": None},
            )
            return view
        if not item_ids:
            raise ImportJobConflictError("item_ids must contain at least one item ID")
        if len(set(item_ids)) != len(item_ids):
            raise ImportJobConflictError("item_ids must be unique")

        async with session_factory() as session:
            rows = (
                await session.exec(
                    select(UploadBatchItem).where(
                        col(UploadBatchItem.batch_id) == job_id,
                        col(UploadBatchItem.id).in_(item_ids),
                    )
                )
            ).all()
        rows_by_id = {row.id: row for row in rows}
        missing = [item_id for item_id in item_ids if item_id not in rows_by_id]
        if missing:
            raise ImportJobNotFoundError("one or more import items do not belong to this job")
        for item_id in item_ids:
            row = rows_by_id[item_id]
            if row.status is not UploadBatchItemStatus.FAILED:
                raise ImportJobConflictError(f"only failed import items can be retried: {item_id}")
            await UploadBatchService.retry_item(
                job_id,
                row.client_file_id,
                user_id=user_id,
            )
        view = await cls.start(job_id, user_id=user_id)
        await AuditService.record(
            action="import_job.items_retried",
            entity_type="import_job",
            entity_id=view.id,
            actor_user_id=user_id,
            project_id=view.project_id,
            metadata={"item_ids": [str(item_id) for item_id in item_ids]},
        )
        return view

    @classmethod
    async def pause(cls, job_id: UUID, *, user_id: UUID) -> UploadBatchView:
        await _owned_import_job(job_id, user_id=user_id)
        view = await UploadBatchService.set_status(
            job_id,
            UploadBatchStatusUpdate(status=UploadBatchStatus.PAUSED),
            user_id=user_id,
        )
        await AuditService.record(
            action="import_job.paused",
            entity_type="import_job",
            entity_id=view.id,
            actor_user_id=user_id,
            project_id=view.project_id,
        )
        return view

    @classmethod
    async def resume(cls, job_id: UUID, *, user_id: UUID) -> UploadBatchView:
        await _owned_import_job(job_id, user_id=user_id)
        await UploadBatchService.set_status(
            job_id,
            UploadBatchStatusUpdate(status=UploadBatchStatus.ACTIVE),
            user_id=user_id,
        )
        view = await cls.start(job_id, user_id=user_id)
        await AuditService.record(
            action="import_job.resumed",
            entity_type="import_job",
            entity_id=view.id,
            actor_user_id=user_id,
            project_id=view.project_id,
        )
        return view

    @classmethod
    async def cancel(cls, job_id: UUID, *, user_id: UUID) -> UploadBatchView:
        await _owned_import_job(job_id, user_id=user_id)
        view = await UploadBatchService.cancel(job_id, user_id=user_id)
        await AuditService.record(
            action="import_job.cancelled",
            entity_type="import_job",
            entity_id=view.id,
            actor_user_id=user_id,
            project_id=view.project_id,
        )
        return view


__all__ = [
    "ImportJobConflictError",
    "ImportJobError",
    "ImportJobNotFoundError",
    "ImportJobService",
]
