"""Publish results already completed after a staged upload was queued."""

from datetime import datetime

from sqlalchemy import and_, or_, text
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from tricycle_reaction_db.db.models import ArtifactIngestion, ParseRevision, UploadBatchItem
from tricycle_reaction_db.domain.enums import ArtifactIngestionStatus

TERMINAL_INGESTION_STATUSES = (
    ArtifactIngestionStatus.SUCCEEDED,
    ArtifactIngestionStatus.PARTIAL,
    ArtifactIngestionStatus.FAILED,
    ArtifactIngestionStatus.FILTERED,
)


def completed_staged_ingestion_predicate() -> ColumnElement[bool]:
    """Require a result newer than the queue request, with intact materialization."""
    revision_exists = (
        select(1)
        .where(col(ParseRevision.artifact_file_id) == col(ArtifactIngestion.artifact_file_id))
        .exists()
    )
    return and_(
        col(ArtifactIngestion.status).in_(TERMINAL_INGESTION_STATUSES),
        col(ArtifactIngestion.completed_at) >= col(UploadBatchItem.updated_at),
        or_(
            col(ArtifactIngestion.status).in_(
                (ArtifactIngestionStatus.FAILED, ArtifactIngestionStatus.FILTERED)
            ),
            revision_exists,
        ),
    )


async def reconcile_completed_staged_items(
    session: AsyncSession, *, limit: int, now: datetime, scan_all: bool = False
) -> int:
    """Finish bounded stale queue pages without changing ingestion or parse rows.

    Explicit reparse requests reset the ingestion to pending. A terminal result
    predating the queue request is also left for the parser. Locks fence an API
    reparse against this acknowledgment, and counters reflect actual item rows.
    """
    if limit < 1:
        return 0
    # Startup sweeps all stale work once. Refills inspect only a bounded FIFO
    # page, so a queue containing only pending files does not cost a full scan.
    candidate_window = (
        ""
        if scan_all
        else """
        JOIN (
            SELECT wi.id FROM upload_batch_item wi
            JOIN upload_batch wb ON wb.id = wi.batch_id
            WHERE wi.status = 'staged' AND wb.status = 'active'
            ORDER BY wi.created_at, wi.id LIMIT :limit
        ) queued_window ON queued_window.id = i.id
    """
    )
    connection = await session.connection()
    result = await connection.execute(
        text(
            """
        WITH eligible AS (
            SELECT i.id, i.batch_id, ai.status AS parse_status,
                   ai.error_code, ai.error_message, latest.id AS revision_id
            FROM upload_batch_item i
            /* candidate_window */
            JOIN upload_batch b ON b.id = i.batch_id
            JOIN artifact_ingestion ai ON ai.artifact_file_id = i.artifact_file_id
            LEFT JOIN LATERAL (
                SELECT pr.id FROM parse_revision pr
                WHERE pr.artifact_file_id = ai.artifact_file_id
                ORDER BY pr.revision_number DESC LIMIT 1
            ) latest ON true
            WHERE i.status = 'staged' AND b.status = 'active'
              AND ai.status IN ('succeeded', 'partial', 'failed', 'filtered')
              AND ai.completed_at >= i.updated_at
              AND (ai.status IN ('failed', 'filtered') OR latest.id IS NOT NULL)
            ORDER BY i.created_at, i.id
            LIMIT :limit
            FOR UPDATE OF b, i, ai SKIP LOCKED
        ), changed AS (
            UPDATE upload_batch_item i
            SET status = CASE WHEN e.parse_status IN ('succeeded', 'partial')
                              THEN 'succeeded' ELSE 'failed' END,
                parse_status = e.parse_status,
                materialization_status = 'succeeded',
                parse_revision_id = e.revision_id,
                error_code = CASE WHEN e.parse_status IN ('succeeded', 'partial')
                                  THEN NULL ELSE COALESCE(e.error_code, 'ingestion_failed') END,
                error_message = CASE WHEN e.parse_status IN ('succeeded', 'partial')
                                     THEN NULL ELSE e.error_message END,
                worker_lease_id = NULL, worker_lease_expires_at = NULL,
                updated_at = :now,
                metadata_json = jsonb_set(i.metadata_json, '{__tricycle_upload_progress}',
                    jsonb_build_object('phase', CASE
                        WHEN e.parse_status IN ('succeeded', 'partial') THEN 'completed'
                        ELSE 'failed' END, 'completed', 1, 'total', 1), true)
            FROM eligible e WHERE i.id = e.id
            RETURNING i.id, i.batch_id, i.status
        ), counts AS (
            SELECT i.batch_id,
                count(*) FILTER (WHERE COALESCE(c.status, i.status) = 'succeeded') AS succeeded,
                count(*) FILTER (WHERE COALESCE(c.status, i.status) = 'failed') AS failed,
                count(*) FILTER (WHERE COALESCE(c.status, i.status) = 'cancelled') AS cancelled,
                count(*) FILTER (WHERE COALESCE(c.status, i.status) = 'uploading') AS uploading,
                count(*) FILTER (WHERE COALESCE(c.status, i.status) = 'staged') AS staged,
                count(*) FILTER (WHERE COALESCE(c.status, i.status) = 'processing') AS processing
            FROM upload_batch_item i LEFT JOIN changed c ON c.id = i.id
            WHERE i.batch_id IN (SELECT batch_id FROM changed)
            GROUP BY i.batch_id
        ), updated_batches AS (
            UPDATE upload_batch b
            SET succeeded_count = c.succeeded, failed_count = c.failed,
                cancelled_count = c.cancelled, uploading_count = c.uploading,
                staged_count = c.staged, processing_count = c.processing,
                status = CASE WHEN c.uploading = 0 AND c.staged = 0 AND c.processing = 0
                              AND c.succeeded + c.failed + c.cancelled = b.total_count
                              THEN 'completed' ELSE b.status END,
                updated_at = :now
            FROM counts c WHERE b.id = c.batch_id RETURNING b.id
        )
        SELECT count(*) FROM changed
        """.replace("/* candidate_window */", candidate_window)
        ),
        {"limit": limit, "now": now},
    )
    return int(result.scalar_one())
