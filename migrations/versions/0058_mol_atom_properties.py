"""Preserve MolGR metal spin alongside native RDKit search graphs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0058_mol_atom_properties"
down_revision = "0057_stereo_agnostic_hash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical atom spins cannot be inferred from the cartridge's MOL or
    # total radical count; keep them unknown until their sources are reparsed.
    for table in ("molecular_topology", "geometry"):
        op.add_column(
            table,
            sa.Column("mol_atom_properties", JSONB(), nullable=False, server_default="{}"),
        )


def downgrade() -> None:
    for table in ("geometry", "molecular_topology"):
        op.drop_column(table, "mol_atom_properties")
