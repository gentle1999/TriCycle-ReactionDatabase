"""Add a durable queue for UniTS transition-state dataset exports."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0059_units_ts_dataset_export"
down_revision: str | None = "0058_mol_atom_properties"
branch_labels: None = None
depends_on: None = None

_TABLE = "units_ts_dataset_export_job"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("download_token_hash", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("object_key", sa.Text(), nullable=True),
        sa.Column("bucket", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "skip_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("lease_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_units_ts_dataset_export_job"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["project.id"], name="fk_units_ts_export_project", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["user_account.id"],
            name="fk_units_ts_export_requested_by",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed', 'expired')",
            name="ck_units_ts_dataset_export_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_units_ts_dataset_export_attempts"),
        sa.CheckConstraint(
            "sample_count >= 0 AND skipped_count >= 0",
            name="ck_units_ts_dataset_export_counts",
        ),
        sa.CheckConstraint(
            "content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_units_ts_dataset_export_sha256",
        ),
        sa.UniqueConstraint(
            "download_token_hash",
            name="uq_units_ts_dataset_export_token_hash",
        ),
    )
    op.create_index(
        "ix_units_ts_dataset_export_job_project_id",
        _TABLE,
        ["project_id"],
    )
    op.create_index(
        "ix_units_ts_dataset_export_job_requested_by_user_id",
        _TABLE,
        ["requested_by_user_id"],
    )
    op.create_index(
        "ix_units_ts_dataset_export_claim",
        _TABLE,
        ["status", "available_at", "requested_at"],
    )
    op.create_index(
        "ix_units_ts_dataset_export_lease",
        _TABLE,
        ["status", "lease_expires_at"],
    )
    op.create_index(
        "ix_units_ts_dataset_export_expiration",
        _TABLE,
        ["status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
