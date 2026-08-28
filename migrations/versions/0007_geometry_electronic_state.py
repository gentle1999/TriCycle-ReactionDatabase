"""Store charge and spin multiplicity as part of Geometry identity."""

import sqlalchemy as sa
from alembic import op

revision: str = "0007_geometry_electronic_state"
down_revision: str | None = "0006_project_geometry_catalog"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "geometry",
        sa.Column("charge", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "geometry",
        sa.Column("multiplicity", sa.SmallInteger(), nullable=False, server_default="1"),
    )
    op.create_check_constraint(
        "ck_geometry_multiplicity_positive",
        "geometry",
        "multiplicity > 0",
    )
    op.drop_constraint("uq_geometry_topology_hash", "geometry", type_="unique")
    op.create_unique_constraint(
        "uq_geometry_topology_hash",
        "geometry",
        ["topology_id", "canonicalization_version", "geometry_hash", "charge", "multiplicity"],
    )
    op.alter_column("geometry", "charge", server_default=None)
    op.alter_column("geometry", "multiplicity", server_default=None)


def downgrade() -> None:
    op.drop_constraint("uq_geometry_topology_hash", "geometry", type_="unique")
    op.create_unique_constraint(
        "uq_geometry_topology_hash",
        "geometry",
        ["topology_id", "canonicalization_version", "geometry_hash"],
    )
    op.drop_constraint("ck_geometry_multiplicity_positive", "geometry", type_="check")
    op.drop_column("geometry", "multiplicity")
    op.drop_column("geometry", "charge")
