"""Add user-maintained notes to artifact catalogue entries."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0052_artifact_notes"
down_revision: str | None = "0051_profile_source_insert_vis"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "artifact_file",
        sa.Column("notes", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("artifact_file", "notes")
