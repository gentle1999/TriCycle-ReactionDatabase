"""Validate the optional queue content identity."""

from alembic import op

revision: str = "0031_upload_item_sha256_check"
down_revision: str | None = "0030_upload_content_identity"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_upload_batch_item_content_sha256_hex",
        "upload_batch_item",
        "content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_upload_batch_item_content_sha256_hex",
        "upload_batch_item",
        type_="check",
    )
