"""Index incremental upload queue status synchronization."""

from alembic import op

revision: str = "0032_upload_item_update_index"
down_revision: str | None = "0031_upload_item_sha256_check"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "ix_upload_batch_item_batch_updated_position",
        "upload_batch_item",
        ["batch_id", "updated_at", "position"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_upload_batch_item_batch_updated_position",
        table_name="upload_batch_item",
    )
