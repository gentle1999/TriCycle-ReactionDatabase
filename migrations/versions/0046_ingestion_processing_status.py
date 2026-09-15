"""Expose the worker-owned MolOP processing state for artifact ingestions."""

import sqlalchemy as sa
from alembic import op

revision: str = "0046_ingestion_processing_status"
down_revision: str | None = "0045_geometry_autovacuum_stats"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_constraint(
        "artifact_ingestion_status",
        "artifact_ingestion",
        type_="check",
    )
    op.create_check_constraint(
        "artifact_ingestion_status",
        "artifact_ingestion",
        "status IN ('pending', 'processing', 'succeeded', 'partial', 'filtered', 'failed')",
    )
    op.drop_constraint(
        "ck_artifact_ingestion_terminal_timestamp",
        "artifact_ingestion",
        type_="check",
    )
    op.create_check_constraint(
        "ck_artifact_ingestion_terminal_timestamp",
        "artifact_ingestion",
        "status IN ('pending', 'processing') OR completed_at IS NOT NULL",
    )
    op.alter_column(
        "artifact_ingestion",
        "status",
        existing_type=sa.String(length=9),
        type_=sa.String(length=10),
        existing_nullable=False,
    )
    # A deployment can happen while the compatibility worker is already
    # parsing. Old pending reservations also carried a lease before the
    # worker claimed them, so require an actual processing-attempt marker or
    # an active upload-batch processing item instead of treating every lease
    # as proof that MolOP is running.
    op.execute(
        """
        UPDATE artifact_ingestion
        SET status = 'processing'
        WHERE artifact_ingestion.status = 'pending'
          AND artifact_ingestion.worker_lease_id IS NOT NULL
          AND artifact_ingestion.worker_lease_expires_at > CURRENT_TIMESTAMP
          AND (
              artifact_ingestion.processing_attempt_count > 0
              OR EXISTS (
                  SELECT 1
                  FROM upload_batch_item
                  WHERE upload_batch_item.artifact_file_id = artifact_ingestion.artifact_file_id
                    AND upload_batch_item.status = 'processing'
                    AND upload_batch_item.worker_lease_id IS NOT NULL
                    AND upload_batch_item.worker_lease_expires_at > CURRENT_TIMESTAMP
              )
          )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE artifact_ingestion
        SET status = 'pending'
        WHERE status = 'processing'
        """
    )
    op.drop_constraint(
        "ck_artifact_ingestion_terminal_timestamp",
        "artifact_ingestion",
        type_="check",
    )
    op.create_check_constraint(
        "ck_artifact_ingestion_terminal_timestamp",
        "artifact_ingestion",
        "status = 'pending' OR completed_at IS NOT NULL",
    )
    op.drop_constraint(
        "artifact_ingestion_status",
        "artifact_ingestion",
        type_="check",
    )
    op.create_check_constraint(
        "artifact_ingestion_status",
        "artifact_ingestion",
        "status IN ('pending', 'succeeded', 'partial', 'filtered', 'failed')",
    )
    op.alter_column(
        "artifact_ingestion",
        "status",
        existing_type=sa.String(length=10),
        type_=sa.String(length=9),
        existing_nullable=False,
    )
