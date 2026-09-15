"""Repair pending reservations misclassified by the first processing-state migration."""

from alembic import op

revision: str = "0047_repair_processing_state"
down_revision: str | None = "0046_ingestion_processing_status"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 0046 was initially written against the assumption that every active
    # ingestion lease meant MolOP had started. Before that migration, staging
    # also reserved a lease while leaving the row pending. Reset only rows
    # without a processing attempt and without an active batch item claim.
    op.execute(
        """
        UPDATE artifact_ingestion
        SET status = 'pending',
            started_at = NULL,
            completed_at = NULL,
            worker_lease_id = NULL,
            worker_lease_expires_at = NULL
        WHERE artifact_ingestion.status = 'processing'
          AND artifact_ingestion.processing_attempt_count = 0
          AND NOT EXISTS (
              SELECT 1
              FROM upload_batch_item
              WHERE upload_batch_item.artifact_file_id = artifact_ingestion.artifact_file_id
                AND upload_batch_item.status = 'processing'
                AND upload_batch_item.worker_lease_id IS NOT NULL
                AND upload_batch_item.worker_lease_expires_at > CURRENT_TIMESTAMP
          )
        """
    )


def downgrade() -> None:
    # The repair is intentionally not reversible: restoring the ambiguous
    # pre-migration lease state would reintroduce the false processing signal.
    pass
