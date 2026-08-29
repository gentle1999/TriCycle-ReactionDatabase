"""Add covering indexes for geometry and reaction catalogue pagination."""

from alembic import op

revision: str = "0011_query_listing_indexes"
down_revision: str | None = "0010_ingestion_filtered"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index("ix_geometry_created_id", "geometry", ["created_at", "id"])
    op.create_index("ix_logical_reaction_created_id", "logical_reaction", ["created_at", "id"])
    op.create_index("ix_logical_reaction_reaction_key", "logical_reaction", ["reaction_key"])
    op.create_index(
        "ix_calculation_frame_geometry_revision",
        "calculation_frame",
        ["geometry_id", "parse_revision_id"],
        postgresql_include=["id", "frequency_count", "negative_frequency_count"],
    )


def downgrade() -> None:
    op.drop_index("ix_calculation_frame_geometry_revision", table_name="calculation_frame")
    op.drop_index("ix_logical_reaction_reaction_key", table_name="logical_reaction")
    op.drop_index("ix_logical_reaction_created_id", table_name="logical_reaction")
    op.drop_index("ix_geometry_created_id", table_name="geometry")
