"""Persist explicit project ownership and scientific project context."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044_project_identity_provenance"
down_revision: str | None = "0043_durable_import_manifests"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "project",
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "project",
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_project_owner_user_id_user_account",
        "project",
        "user_account",
        ["owner_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_project_created_by_user_id_user_account",
        "project",
        "user_account",
        ["created_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_project_owner_user_id", "project", ["owner_user_id"])
    op.create_index("ix_project_created_by_user_id", "project", ["created_by_user_id"])

    op.add_column(
        "project",
        sa.Column(
            "data_source",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "project",
        sa.Column(
            "model_checkpoint",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "project",
        sa.Column(
            "calculation_protocol",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    # Preserve an auditable owner/creator for legacy rows whenever a project
    # already has a manager. Rows without a manager remain nullable and are
    # repaired by the next explicit project-management operation.
    op.execute(
        sa.text(
            """
            UPDATE project AS p
            SET owner_user_id = COALESCE(p.owner_user_id, manager.user_id),
                created_by_user_id = COALESCE(p.created_by_user_id, manager.user_id)
            FROM (
                SELECT DISTINCT ON (project_id) project_id, user_id
                FROM project_membership
                WHERE role = 'manager'
                ORDER BY project_id, created_at, id
            ) AS manager
            WHERE p.id = manager.project_id
              AND (p.owner_user_id IS NULL OR p.created_by_user_id IS NULL)
            """
        )
    )


def downgrade() -> None:
    op.drop_column("project", "calculation_protocol")
    op.drop_column("project", "model_checkpoint")
    op.drop_column("project", "data_source")
    op.drop_index("ix_project_created_by_user_id", table_name="project")
    op.drop_index("ix_project_owner_user_id", table_name="project")
    op.drop_constraint(
        "fk_project_created_by_user_id_user_account",
        "project",
        type_="foreignkey",
    )
    op.drop_constraint("fk_project_owner_user_id_user_account", "project", type_="foreignkey")
    op.drop_column("project", "created_by_user_id")
    op.drop_column("project", "owner_user_id")
