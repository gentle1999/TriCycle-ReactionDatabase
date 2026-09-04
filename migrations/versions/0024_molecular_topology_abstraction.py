"""Add the directed molecular-topology stereo abstraction graph."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024_topology_stereo_dag"
down_revision: str | None = "0023_ambiguous_stereo_status"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "molecular_topology_abstraction",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("uuidv7()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("specific_topology_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("general_topology_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "abstraction_policy_version",
            sa.String(length=64),
            server_default="topology-stereo-abstraction-v1",
            nullable=False,
        ),
        sa.Column(
            "abstraction_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "specific_topology_id <> general_topology_id",
            name="ck_molecular_topology_abstraction_distinct_endpoints",
        ),
        sa.ForeignKeyConstraint(
            ["specific_topology_id"],
            ["molecular_topology.id"],
            name="fk_molecular_topology_abstraction_specific",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["general_topology_id"],
            ["molecular_topology.id"],
            name="fk_molecular_topology_abstraction_general",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "specific_topology_id",
            "general_topology_id",
            "abstraction_policy_version",
            name="uq_molecular_topology_abstraction_edge_policy",
        ),
    )
    op.create_index(
        "ix_molecular_topology_abstraction_specific",
        "molecular_topology_abstraction",
        ["specific_topology_id"],
    )
    op.create_index(
        "ix_molecular_topology_abstraction_general",
        "molecular_topology_abstraction",
        ["general_topology_id"],
    )
    op.create_index(
        "ix_molecular_topology_abstraction_abstraction_policy_version",
        "molecular_topology_abstraction",
        ["abstraction_policy_version"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_molecular_topology_abstraction_abstraction_policy_version",
        table_name="molecular_topology_abstraction",
    )
    op.drop_index(
        "ix_molecular_topology_abstraction_general",
        table_name="molecular_topology_abstraction",
    )
    op.drop_index(
        "ix_molecular_topology_abstraction_specific",
        table_name="molecular_topology_abstraction",
    )
    op.drop_table("molecular_topology_abstraction")
