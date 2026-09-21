"""Index topology candidates by the stereo-agnostic DAG graph hash."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from rdkit import Chem
from sqlmodel import Session

from tricycle_reaction_db.db.models import MolecularTopology
from tricycle_reaction_db.ingestion.normalization import stereo_agnostic_graph_hash

revision: str = "0057_stereo_agnostic_hash"
down_revision: str | None = "0056_profile_refresh_queue"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "molecular_topology",
        sa.Column("stereo_agnostic_graph_hash", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_molecular_topology_project_stereo_agnostic_graph_hash",
        "molecular_topology",
        ["project_id", "stereo_agnostic_graph_hash"],
    )
    _backfill_existing_topologies()


def _backfill_existing_topologies() -> None:
    """Populate every historical topology before the index is used by DAG scans."""

    connection = op.get_bind()
    session = Session(bind=connection, expire_on_commit=False)
    try:
        # This migration precedes the electronic-annotation sidecar. Do not
        # use the current model's MOL reader, which needs that later column.
        rows = session.execute(
            sa.text(
                "SELECT id, mol_send(mol) FROM molecular_topology "
                "WHERE stereo_agnostic_graph_hash IS NULL"
            )
        ).all()
        updates: list[dict[str, object]] = []
        for topology_id, binary in rows:
            molecule = Chem.Mol(bytes(binary)) if binary is not None else None
            if topology_id is None or not isinstance(molecule, Chem.Mol):
                raise RuntimeError(
                    "molecular topology backfill encountered an invalid RDKit molecule"
                )
            updates.append(
                {
                    "topology_id": topology_id,
                    "stereo_hash": stereo_agnostic_graph_hash(molecule),
                }
            )
        if updates:
            topology_table = MolecularTopology.__table__
            session.execute(
                sa.update(topology_table)
                .where(topology_table.c.id == sa.bindparam("topology_id"))
                .values(
                    stereo_agnostic_graph_hash=sa.bindparam("stereo_hash"),
                ),
                updates,
            )
        session.flush()
    finally:
        session.close()


def downgrade() -> None:
    op.drop_index(
        "ix_molecular_topology_project_stereo_agnostic_graph_hash",
        table_name="molecular_topology",
    )
    op.drop_column("molecular_topology", "stereo_agnostic_graph_hash")
