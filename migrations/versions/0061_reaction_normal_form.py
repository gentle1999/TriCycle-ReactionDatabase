"""Remember the selected reaction normal form without relabelling legacy data."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0061_reaction_normal_form"
down_revision = "0060_coordinate_complete_mol"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Legacy rows remain unknown until reconstructed from their source TS frames.
    op.add_column("mapped_reaction", sa.Column("normalization_metadata", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("mapped_reaction", "normalization_metadata")
