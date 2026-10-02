"""Keep a coordinate-complete atom-ordered molecule beside RDKit search MOL.

Recovered from the already-deployed migration. Freeze its format literal here
so future application policy changes cannot alter this historical revision.
"""

import sqlalchemy as sa
from alembic import op

revision = "0060_coordinate_complete_mol"
down_revision = "0059_units_ts_dataset_export"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "molecular_topology",
        sa.Column("coordinate_complete_mol", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "molecular_topology",
        sa.Column("coordinate_complete_mol_format", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_molecular_topology_coordinate_complete_pair",
        "molecular_topology",
        "(coordinate_complete_mol IS NULL AND coordinate_complete_mol_format IS NULL) "
        "OR (coordinate_complete_mol IS NOT NULL "
        "AND coordinate_complete_mol_format = 'rdkit-mol-binary-v1')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_molecular_topology_coordinate_complete_pair", "molecular_topology", type_="check"
    )
    op.drop_column("molecular_topology", "coordinate_complete_mol_format")
    op.drop_column("molecular_topology", "coordinate_complete_mol")
