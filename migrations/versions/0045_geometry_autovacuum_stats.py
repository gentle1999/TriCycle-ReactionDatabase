"""Keep geometry candidate statistics fresh during large imports."""

from alembic import op

revision: str = "0045_geometry_autovacuum_stats"
down_revision: str | None = "0044_project_identity_provenance"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Geometry matching depends on the project/topology candidate index.  The
    # default 10% autovacuum threshold is too high for a multi-million-row
    # shared catalog, so PostgreSQL can keep estimating a project as empty for
    # the duration of a large staged import and choose a project-wide scan.
    op.execute(
        "ALTER TABLE geometry SET ("
        "autovacuum_analyze_scale_factor = 0.02, "
        "autovacuum_analyze_threshold = 1000"
        ")"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE geometry RESET (autovacuum_analyze_scale_factor, autovacuum_analyze_threshold)"
    )
