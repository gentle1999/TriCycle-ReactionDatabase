"""Persist the quantum-chemistry file-level running time."""

import sqlalchemy as sa
from alembic import op

revision: str = "0021_parse_revision_running_time"
down_revision: str | None = "0020_cross_project_artifact"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("parse_revision")}
    if "running_time_seconds" not in columns:
        op.add_column(
            "parse_revision",
            sa.Column("running_time_seconds", sa.Float(), nullable=True),
        )
    constraints = {
        constraint.get("name")
        for constraint in sa.inspect(bind).get_check_constraints("parse_revision")
    }
    if "ck_parse_revision_running_time_nonnegative" not in constraints:
        op.create_check_constraint(
            "ck_parse_revision_running_time_nonnegative",
            "parse_revision",
            "running_time_seconds IS NULL OR running_time_seconds >= 0",
        )


def downgrade() -> None:
    op.drop_constraint(
        "ck_parse_revision_running_time_nonnegative",
        "parse_revision",
        type_="check",
    )
    op.drop_column("parse_revision", "running_time_seconds")
