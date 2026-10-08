"""Lease staged uploads in FIFO order without sorting the whole queue."""

import sqlalchemy as sa
from alembic import op

revision = "0062_staged_upload_claim_index"
down_revision = "0061_reaction_normal_form"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The queue remains active during deployment. Build outside the migration
    # transaction so PostgreSQL does not block its status updates and inserts.
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_upload_batch_item_staged_claim",
            "upload_batch_item",
            ["created_at", "id"],
            postgresql_where=sa.text("status = 'staged'"),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_upload_batch_item_staged_claim",
            table_name="upload_batch_item",
            postgresql_concurrently=True,
        )
