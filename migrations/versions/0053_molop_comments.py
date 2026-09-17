"""Persist MolOP's unified file and frame comment containers."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0053_molop_comments"
down_revision: str | None = "0052_artifact_notes"
branch_labels: str | None = None
depends_on: str | None = None

_COMMENTS_DEFAULT = sa.text("'{\"items\": []}'::jsonb")


def upgrade() -> None:
    op.add_column(
        "parse_revision",
        sa.Column(
            "comments",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_COMMENTS_DEFAULT,
        ),
    )
    op.add_column(
        "calculation_frame",
        sa.Column(
            "comments",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_COMMENTS_DEFAULT,
        ),
    )


def downgrade() -> None:
    op.drop_column("calculation_frame", "comments")
    op.drop_column("parse_revision", "comments")
