"""Match the frame-count index to the API's NULLS LAST ordering."""

from alembic import op

revision: str = "0014_frame_count_null_order"
down_revision: str | None = "0013_catalog_frame_count_indexes"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_index(
        "ix_project_geometry_catalog_frame_count_desc",
        table_name="project_geometry_catalog",
    )
    op.execute(
        """
        CREATE INDEX ix_project_geometry_catalog_frame_count_desc
        ON project_geometry_catalog
            (project_id, frame_count DESC NULLS LAST, geometry_id ASC)
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_project_geometry_catalog_frame_count_desc",
        table_name="project_geometry_catalog",
    )
    op.execute(
        """
        CREATE INDEX ix_project_geometry_catalog_frame_count_desc
        ON project_geometry_catalog
            (project_id, frame_count DESC, geometry_id ASC)
        """
    )
