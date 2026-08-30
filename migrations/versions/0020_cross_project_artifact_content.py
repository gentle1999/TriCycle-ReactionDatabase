"""Allow project-owned artifact records to share one stored object."""

from alembic import op

revision: str = "0020_cross_project_artifact"
down_revision: str | None = "0019_catalog_delete_trigger"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_constraint(
        "artifact_file_content_sha256_key",
        "artifact_file",
        type_="unique",
    )
    op.drop_constraint("uq_artifact_file_object", "artifact_file", type_="unique")
    op.create_unique_constraint(
        "uq_artifact_file_project_content",
        "artifact_file",
        ["project_id", "content_sha256"],
    )
    op.create_index(
        "ix_artifact_file_content_sha256",
        "artifact_file",
        ["content_sha256"],
    )
    op.create_index(
        "ix_artifact_file_object_reference",
        "artifact_file",
        ["bucket", "object_key", "storage_status", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_artifact_file_object_reference", table_name="artifact_file")
    op.drop_index("ix_artifact_file_content_sha256", table_name="artifact_file")
    op.drop_constraint(
        "uq_artifact_file_project_content",
        "artifact_file",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_artifact_file_object",
        "artifact_file",
        ["bucket", "object_key"],
    )
    op.create_unique_constraint(
        "artifact_file_content_sha256_key",
        "artifact_file",
        ["content_sha256"],
    )
