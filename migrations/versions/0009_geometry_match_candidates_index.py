"""Index the scalar predicates used before geometry equivalence matching."""

from alembic import op

revision: str = "0009_geom_match_idx"
down_revision: str | None = "0008_ts_endpoint_state"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "ix_geometry_match_candidates",
        "geometry",
        ["topology_id", "canonicalization_version", "charge", "multiplicity"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_geometry_match_candidates", table_name="geometry")
