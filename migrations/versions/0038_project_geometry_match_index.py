"""Index project-local geometry candidate matching."""

from alembic import op

revision: str = "0038_geometry_match_index"
down_revision: str | None = "0037_project_owned_protocol"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "ix_geometry_project_match_candidates",
        "geometry",
        [
            "project_id",
            "topology_id",
            "canonicalization_version",
            "charge",
            "multiplicity",
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_geometry_project_match_candidates", table_name="geometry")
