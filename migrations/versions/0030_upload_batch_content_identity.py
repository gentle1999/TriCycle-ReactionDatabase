"""Remember uploaded content identity for crash-safe queue recovery."""

import sqlalchemy as sa
from alembic import op

revision: str = "0030_upload_content_identity"
down_revision: str | None = "0029_server_owned_upload_queue"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "upload_batch_item",
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_upload_batch_item_batch_content",
        "upload_batch_item",
        ["batch_id", "content_sha256"],
    )


def downgrade() -> None:
    op.drop_index("ix_upload_batch_item_batch_content", table_name="upload_batch_item")
    op.drop_column("upload_batch_item", "content_sha256")
