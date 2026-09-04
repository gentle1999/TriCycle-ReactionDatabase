"""Mark topology rows that are valid stereo-abstraction upstreams."""

import sqlalchemy as sa
from alembic import op

revision: str = "0025_topology_upstream_marker"
down_revision: str | None = "0024_topology_stereo_dag"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "molecular_topology",
        sa.Column(
            "is_stereo_abstraction_upstream",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_molecular_topology_upstream_candidates",
        "molecular_topology",
        ["formula_id", "is_stereo_abstraction_upstream"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_molecular_topology_upstream_candidates",
        table_name="molecular_topology",
    )
    op.drop_column("molecular_topology", "is_stereo_abstraction_upstream")
