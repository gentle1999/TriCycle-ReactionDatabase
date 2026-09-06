"""Make web uploads durable before parsing begins."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029_server_owned_upload_queue"
down_revision: str | None = "0028_restore_mapped_text_id"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "upload_batch",
        sa.Column("staged_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "upload_batch",
        sa.Column("processing_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.drop_constraint("ck_upload_batch_counts_nonnegative", "upload_batch", type_="check")
    op.create_check_constraint(
        "ck_upload_batch_counts_nonnegative",
        "upload_batch",
        "succeeded_count >= 0 AND failed_count >= 0 AND cancelled_count >= 0 AND "
        "uploading_count >= 0 AND staged_count >= 0 AND processing_count >= 0",
    )
    op.drop_constraint("ck_upload_batch_counts_lte_total", "upload_batch", type_="check")
    op.create_check_constraint(
        "ck_upload_batch_counts_lte_total",
        "upload_batch",
        "succeeded_count + failed_count + cancelled_count + uploading_count + "
        "staged_count + processing_count <= total_count",
    )

    op.add_column(
        "upload_batch_item",
        sa.Column("processing_attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "upload_batch_item",
        sa.Column("worker_lease_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "upload_batch_item",
        sa.Column("worker_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint("upload_batch_item_status", "upload_batch_item", type_="check")
    op.create_check_constraint(
        "upload_batch_item_status",
        "upload_batch_item",
        "status IN ("
        "'queued', 'uploading', 'staged', 'processing', "
        "'succeeded', 'failed', 'cancelled'"
        ")",
    )
    op.create_check_constraint(
        "ck_upload_batch_item_processing_attempts_nonnegative",
        "upload_batch_item",
        "processing_attempt_count >= 0",
    )
    op.create_index(
        "ix_upload_batch_item_processing_lease",
        "upload_batch_item",
        ["status", "worker_lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_upload_batch_item_processing_lease", table_name="upload_batch_item")
    op.drop_constraint(
        "ck_upload_batch_item_processing_attempts_nonnegative",
        "upload_batch_item",
        type_="check",
    )
    op.drop_constraint("upload_batch_item_status", "upload_batch_item", type_="check")
    op.create_check_constraint(
        "upload_batch_item_status",
        "upload_batch_item",
        "status IN ('queued', 'uploading', 'succeeded', 'failed', 'cancelled')",
    )
    op.drop_column("upload_batch_item", "worker_lease_expires_at")
    op.drop_column("upload_batch_item", "worker_lease_id")
    op.drop_column("upload_batch_item", "processing_attempt_count")

    op.drop_constraint("ck_upload_batch_counts_lte_total", "upload_batch", type_="check")
    op.create_check_constraint(
        "ck_upload_batch_counts_lte_total",
        "upload_batch",
        "succeeded_count + failed_count + cancelled_count + uploading_count <= total_count",
    )
    op.drop_constraint("ck_upload_batch_counts_nonnegative", "upload_batch", type_="check")
    op.create_check_constraint(
        "ck_upload_batch_counts_nonnegative",
        "upload_batch",
        "succeeded_count >= 0 AND failed_count >= 0 AND "
        "cancelled_count >= 0 AND uploading_count >= 0",
    )
    op.drop_column("upload_batch", "processing_count")
    op.drop_column("upload_batch", "staged_count")
