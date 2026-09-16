"""Index project-scoped logical reaction membership lookups."""

from __future__ import annotations

from alembic import op

revision: str = "0048_mapped_rxn_project_logical"
down_revision: str | None = "0047_repair_processing_state"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "ix_mapped_reaction_project_logical_reaction_id",
        "mapped_reaction",
        ["project_id", "logical_reaction_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mapped_reaction_project_logical_reaction_id",
        table_name="mapped_reaction",
    )
