"""Create the initial production schema from the current v1 baseline."""

from pathlib import Path

from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def _baseline_sql() -> str:
    # SQLAlchemy passes an empty parameter tuple to psycopg, so literal `%`
    # operators would otherwise be parsed as DB-API placeholders.
    return (
        Path(__file__)
        .with_name("0001_initial_schema.sql")
        .read_text(encoding="utf-8")
        .replace("%", "%%")
    )


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_baseline_sql())


def downgrade() -> None:
    raise RuntimeError(
        "The initial schema is not downgradable; recreate the database or restore a backup."
    )
