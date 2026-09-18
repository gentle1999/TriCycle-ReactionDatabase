"""Replace the in-memory profile dirty set with a durable generation queue."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0056_profile_refresh_queue"
down_revision: str | None = "0055_defer_profile_visibility"
branch_labels: str | None = None
depends_on: str | None = None


_JOB_TABLE = "mapped_reaction_thermodynamic_profile_refresh_job"


def upgrade() -> None:
    op.add_column(
        "mapped_reaction",
        sa.Column(
            "thermodynamic_profile_generation",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "mapped_reaction",
        sa.Column(
            "thermodynamic_profile_materialized_generation",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_index(
        "ix_mapped_reaction_thermodynamic_profile_generation",
        "mapped_reaction",
        [
            "project_id",
            "thermodynamic_profile_generation",
            "thermodynamic_profile_materialized_generation",
        ],
    )
    op.execute(
        sa.text(
            "UPDATE mapped_reaction "
            "SET thermodynamic_profile_generation = 1 "
            "WHERE thermodynamic_profile_dirty IS TRUE"
        )
    )

    op.create_table(
        _JOB_TABLE,
        sa.Column(
            "mapped_reaction_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("requested_generation", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("lease_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("mapped_reaction_id", name="pk_profile_refresh_job"),
        sa.ForeignKeyConstraint(
            ["mapped_reaction_id"],
            ["mapped_reaction.id"],
            name="fk_profile_refresh_job_mapped_reaction",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing')",
            name="ck_profile_refresh_job_status",
        ),
        sa.CheckConstraint(
            "requested_generation >= 0",
            name="ck_profile_refresh_requested_generation_nonnegative",
        ),
        sa.CheckConstraint(
            "priority >= 0 AND priority <= 100",
            name="ck_profile_refresh_priority_range",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_profile_refresh_attempts_nonnegative",
        ),
    )
    op.create_index(
        "ix_profile_refresh_job_claim",
        _JOB_TABLE,
        ["status", "available_at", "priority", "requested_at"],
    )
    op.create_index(
        "ix_profile_refresh_job_lease",
        _JOB_TABLE,
        ["status", "lease_expires_at"],
    )
    op.execute(
        sa.text(
            f"INSERT INTO {_JOB_TABLE} (mapped_reaction_id, requested_generation) "
            "SELECT id, thermodynamic_profile_generation "
            "FROM mapped_reaction "
            "WHERE thermodynamic_profile_dirty IS TRUE"
        )
    )
    op.drop_index(
        "ix_mapped_reaction_thermodynamic_profile_dirty",
        table_name="mapped_reaction",
    )
    op.drop_column("mapped_reaction", "thermodynamic_profile_dirty")


def downgrade() -> None:
    op.add_column(
        "mapped_reaction",
        sa.Column(
            "thermodynamic_profile_dirty",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.execute(
        sa.text(
            f"UPDATE mapped_reaction AS reaction "
            "SET thermodynamic_profile_dirty = TRUE "
            "WHERE reaction.thermodynamic_profile_generation "
            "> reaction.thermodynamic_profile_materialized_generation "
            f"OR EXISTS (SELECT 1 FROM {_JOB_TABLE} AS job "
            "WHERE job.mapped_reaction_id = reaction.id)"
        )
    )
    op.create_index(
        "ix_mapped_reaction_thermodynamic_profile_dirty",
        "mapped_reaction",
        ["thermodynamic_profile_dirty"],
    )
    op.drop_index("ix_profile_refresh_job_lease", table_name=_JOB_TABLE)
    op.drop_index("ix_profile_refresh_job_claim", table_name=_JOB_TABLE)
    op.drop_table(_JOB_TABLE)
    op.drop_index(
        "ix_mapped_reaction_thermodynamic_profile_generation",
        table_name="mapped_reaction",
    )
    op.drop_column("mapped_reaction", "thermodynamic_profile_materialized_generation")
    op.drop_column("mapped_reaction", "thermodynamic_profile_generation")
