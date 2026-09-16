"""Index project-scoped geometry hash lookups."""

from __future__ import annotations

from alembic import op

revision: str = "0049_geometry_project_hash"
down_revision: str | None = "0048_mapped_rxn_project_logical"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "ix_geometry_project_hash_id",
        "geometry",
        ["project_id", "geometry_hash", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_geometry_project_hash_id", table_name="geometry")
