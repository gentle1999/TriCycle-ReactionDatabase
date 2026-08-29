"""Index project Geometry catalogue frame-count ordering."""

from alembic import op

revision: str = "0013_catalog_frame_count_indexes"
down_revision: str | None = "0012_catalog_summary"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # PostgreSQL can scan a btree backwards, but one index cannot satisfy a
    # mixed-direction order such as ``frame_count DESC, geometry_id ASC``.
    # Keep both directions available so deep pages do not sort the full
    # project catalogue.
    op.create_index(
        "ix_project_geometry_catalog_frame_count_asc",
        "project_geometry_catalog",
        ["project_id", "frame_count", "geometry_id"],
    )
    op.execute(
        """
        CREATE INDEX ix_project_geometry_catalog_frame_count_desc
        ON project_geometry_catalog
            (project_id, frame_count DESC, geometry_id ASC)
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_project_geometry_catalog_frame_count_desc",
        table_name="project_geometry_catalog",
    )
    op.drop_index(
        "ix_project_geometry_catalog_frame_count_asc",
        table_name="project_geometry_catalog",
    )
