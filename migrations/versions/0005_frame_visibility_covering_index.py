"""Cover project-scoped frame and geometry visibility reads."""

from alembic import op

revision: str = "0005_frame_visibility_idx"
down_revision: str | None = "0004_optional_reaction_class"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "ix_calculation_frame_parse_revision_visibility",
        "calculation_frame",
        ["parse_revision_id"],
        unique=False,
        postgresql_include=["id", "geometry_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_calculation_frame_parse_revision_visibility", table_name="calculation_frame")
