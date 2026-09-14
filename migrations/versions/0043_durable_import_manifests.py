"""Persist extracted-file manifests and their import state.

UploadBatch is the existing durable queue, so this migration extends it into
the canonical ImportJob/ImportJobItem representation instead of introducing a
second queue with competing leases.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0043_durable_import_manifests"
down_revision: str | None = "0042_protocol_schema_default_v3"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("artifact_file", sa.Column("source_relative_path", sa.Text(), nullable=True))

    op.add_column(
        "upload_batch",
        sa.Column("archive_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "upload_batch",
        sa.Column("manifest_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "upload_batch",
        sa.Column("manifest_schema_version", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_upload_batch_archive_sha256_hex",
        "upload_batch",
        "archive_sha256 IS NULL OR archive_sha256 ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_upload_batch_manifest_sha256_hex",
        "upload_batch",
        "manifest_sha256 IS NULL OR manifest_sha256 ~ '^[0-9a-f]{64}$'",
    )
    op.create_index(
        "uq_upload_batch_project_manifest",
        "upload_batch",
        ["project_id", "manifest_sha256"],
        unique=True,
        postgresql_where=sa.text("manifest_sha256 IS NOT NULL"),
    )

    op.add_column(
        "upload_batch_item",
        sa.Column("expected_file_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "upload_batch_item",
        sa.Column("staged_file_path", sa.Text(), nullable=True),
    )
    op.add_column(
        "upload_batch_item",
        sa.Column("is_gaussian_log", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "upload_batch_item",
        sa.Column(
            "selection_status",
            sa.String(length=32),
            nullable=False,
            server_default="selected",
        ),
    )
    op.add_column(
        "upload_batch_item",
        sa.Column(
            "parse_status",
            sa.String(length=32),
            nullable=False,
            server_default="not_started",
        ),
    )
    op.add_column(
        "upload_batch_item",
        sa.Column(
            "materialization_status",
            sa.String(length=32),
            nullable=False,
            server_default="not_started",
        ),
    )
    op.add_column(
        "upload_batch_item",
        sa.Column(
            "parse_revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("parse_revision.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_upload_batch_item_expected_sha256_hex",
        "upload_batch_item",
        "expected_file_sha256 IS NULL OR expected_file_sha256 ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_upload_batch_item_selection_status",
        "upload_batch_item",
        "selection_status IN ('selected', 'filtered', 'rejected')",
    )
    op.create_check_constraint(
        "ck_upload_batch_item_parse_status",
        "upload_batch_item",
        "parse_status IN ('not_started', 'pending', 'succeeded', 'partial', 'filtered', 'failed')",
    )
    op.create_check_constraint(
        "ck_upload_batch_item_materialization_status",
        "upload_batch_item",
        "materialization_status IN ('not_started', 'pending', 'succeeded', 'failed')",
    )
    op.create_index(
        "ix_upload_batch_item_parse_revision_id",
        "upload_batch_item",
        ["parse_revision_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_upload_batch_item_parse_revision_id", table_name="upload_batch_item")
    op.drop_constraint(
        "ck_upload_batch_item_materialization_status",
        "upload_batch_item",
        type_="check",
    )
    op.drop_constraint("ck_upload_batch_item_parse_status", "upload_batch_item", type_="check")
    op.drop_constraint(
        "ck_upload_batch_item_selection_status",
        "upload_batch_item",
        type_="check",
    )
    op.drop_constraint(
        "ck_upload_batch_item_expected_sha256_hex",
        "upload_batch_item",
        type_="check",
    )
    op.drop_column("upload_batch_item", "parse_revision_id")
    op.drop_column("upload_batch_item", "materialization_status")
    op.drop_column("upload_batch_item", "parse_status")
    op.drop_column("upload_batch_item", "selection_status")
    op.drop_column("upload_batch_item", "is_gaussian_log")
    op.drop_column("upload_batch_item", "staged_file_path")
    op.drop_column("upload_batch_item", "expected_file_sha256")
    op.drop_index("uq_upload_batch_project_manifest", table_name="upload_batch")
    op.drop_constraint("ck_upload_batch_manifest_sha256_hex", "upload_batch", type_="check")
    op.drop_constraint("ck_upload_batch_archive_sha256_hex", "upload_batch", type_="check")
    op.drop_column("upload_batch", "manifest_schema_version")
    op.drop_column("upload_batch", "manifest_sha256")
    op.drop_column("upload_batch", "archive_sha256")
    op.drop_column("artifact_file", "source_relative_path")
