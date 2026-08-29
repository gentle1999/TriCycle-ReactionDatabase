"""Use partial frame-count indexes for the default thermodynamic Geometry view."""

from alembic import op

revision: str = "0016_geometry_frame_count"
down_revision: str | None = "0015_logical_reaction_sort_key"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX ix_project_geometry_catalog_thermo_frame_count_asc
        ON project_geometry_catalog (project_id, frame_count ASC, geometry_id ASC)
        WHERE has_thermodynamic_property
        """
    )
    op.execute(
        """
        CREATE INDEX ix_project_geometry_catalog_thermo_frame_count_desc
        ON project_geometry_catalog (project_id, frame_count DESC NULLS LAST, geometry_id ASC)
        WHERE has_thermodynamic_property
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_project_geometry_catalog_thermo_frame_count_desc",
        table_name="project_geometry_catalog",
    )
    op.drop_index(
        "ix_project_geometry_catalog_thermo_frame_count_asc",
        table_name="project_geometry_catalog",
    )
