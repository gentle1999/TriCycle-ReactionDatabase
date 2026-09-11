"""Make orphaned calculation ingestions recoverable by the upload worker."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034_ingestion_recovery_lease"
down_revision: str | None = "0033_calc_protocol_dispersion"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "artifact_ingestion",
        sa.Column("processing_attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "artifact_ingestion",
        sa.Column("worker_lease_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "artifact_ingestion",
        sa.Column("worker_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_artifact_ingestion_processing_attempts_nonnegative",
        "artifact_ingestion",
        "processing_attempt_count >= 0",
    )
    op.create_index(
        "ix_artifact_ingestion_recovery_lease",
        "artifact_ingestion",
        ["status", "worker_lease_expires_at", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_artifact_ingestion_recovery_lease", table_name="artifact_ingestion")
    op.drop_constraint(
        "ck_artifact_ingestion_processing_attempts_nonnegative",
        "artifact_ingestion",
        type_="check",
    )
    op.drop_column("artifact_ingestion", "worker_lease_expires_at")
    op.drop_column("artifact_ingestion", "worker_lease_id")
    op.drop_column("artifact_ingestion", "processing_attempt_count")
