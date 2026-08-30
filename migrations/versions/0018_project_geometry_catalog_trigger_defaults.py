"""Provide defaults required by the legacy Geometry catalogue triggers.

The original frame-catalogue triggers insert only project, Geometry, and frame
count.  Migration 0012 added non-null summary columns and refresh triggers, but
removed their database defaults, so a newly seen Geometry fails before the
summary refresh can run.
"""

from alembic import op

revision: str = "0018_catalog_trigger_defaults"
down_revision: str | None = "0017_reactant_sort_key"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    for column in (
        "has_frequency_data",
        "has_imaginary_frequency",
        "has_thermodynamic_property",
    ):
        op.execute(f"ALTER TABLE project_geometry_catalog ALTER COLUMN {column} SET DEFAULT false")


def downgrade() -> None:
    for column in (
        "has_thermodynamic_property",
        "has_imaginary_frequency",
        "has_frequency_data",
    ):
        op.execute(f"ALTER TABLE project_geometry_catalog ALTER COLUMN {column} DROP DEFAULT")
