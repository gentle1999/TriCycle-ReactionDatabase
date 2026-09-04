"""Separate logical participant topologies from concrete topology members."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0026_concrete_reaction_topology"
down_revision: str | None = "0025_topology_upstream_marker"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "logical_participant_concrete_topology",
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
        sa.Column(
            "logical_reaction_participant_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "concrete_topology_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "match_policy_version",
            sa.Text(),
            server_default="logical-participant-concrete-match-v1",
            nullable=False,
        ),
        sa.Column(
            "match_status",
            sa.Text(),
            server_default="matched",
            nullable=False,
        ),
        sa.Column(
            "match_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "match_status IN ('matched', 'ambiguous')",
            name="ck_logical_participant_concrete_match_status",
        ),
        sa.ForeignKeyConstraint(
            ["logical_reaction_participant_id"],
            ["logical_reaction_participant.id"],
            name="fk_logical_participant_concrete_logical_participant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["concrete_topology_id"],
            ["molecular_topology.id"],
            name="fk_logical_participant_concrete_topology",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "logical_reaction_participant_id",
            "concrete_topology_id",
            name="uq_logical_participant_concrete_topology",
        ),
    )
    op.create_index(
        "ix_logical_participant_concrete_topology_logical",
        "logical_participant_concrete_topology",
        ["logical_reaction_participant_id"],
    )
    op.create_index(
        "ix_logical_participant_concrete_topology_concrete",
        "logical_participant_concrete_topology",
        ["concrete_topology_id"],
    )

    # Preserve the old semantics for all rows already in the database.  The
    # following service writes will replace this identity-only evidence with a
    # graph-match record when those rows are touched again.
    op.execute(
        sa.text(
            """
            INSERT INTO logical_participant_concrete_topology (
                logical_reaction_participant_id,
                concrete_topology_id,
                match_policy_version,
                match_status,
                match_metadata
            )
            SELECT id, topology_id,
                   'legacy-topology-identity-v1',
                   'matched',
                   '{"source": "0026-legacy-backfill", "topology_match": "identity"}'::jsonb
            FROM logical_reaction_participant
            ON CONFLICT (logical_reaction_participant_id, concrete_topology_id) DO NOTHING
            """
        )
    )

    op.add_column(
        "mapped_reaction_participant",
        sa.Column("concrete_topology_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_mapped_reaction_participant_concrete_topology",
        "mapped_reaction_participant",
        "molecular_topology",
        ["concrete_topology_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_mapped_reaction_participant_concrete_topology",
        "mapped_reaction_participant",
        ["concrete_topology_id"],
    )
    op.execute(
        sa.text(
            """
            UPDATE mapped_reaction_participant AS mapped
            SET concrete_topology_id = logical.topology_id
            FROM logical_reaction_participant AS logical
            WHERE mapped.logical_reaction_participant_id = logical.id
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mapped_reaction_participant_concrete_topology",
        table_name="mapped_reaction_participant",
    )
    op.drop_constraint(
        "fk_mapped_reaction_participant_concrete_topology",
        "mapped_reaction_participant",
        type_="foreignkey",
    )
    op.drop_column("mapped_reaction_participant", "concrete_topology_id")
    op.drop_index(
        "ix_logical_participant_concrete_topology_concrete",
        table_name="logical_participant_concrete_topology",
    )
    op.drop_index(
        "ix_logical_participant_concrete_topology_logical",
        table_name="logical_participant_concrete_topology",
    )
    op.drop_table("logical_participant_concrete_topology")
