"""Track mapped-reaction profiles awaiting post-queue refresh."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0054_profile_refresh_dirty"
down_revision: str | None = "0053_molop_comments"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "mapped_reaction",
        sa.Column(
            "thermodynamic_profile_dirty",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_mapped_reaction_thermodynamic_profile_dirty",
        "mapped_reaction",
        ["thermodynamic_profile_dirty"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mapped_reaction_thermodynamic_profile_dirty",
        table_name="mapped_reaction",
    )
    op.drop_column("mapped_reaction", "thermodynamic_profile_dirty")
