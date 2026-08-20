import os

import pytest
from molalchemy.helpers import rdkit_col
from molalchemy.rdkit.index import RdkitIndex
from molalchemy.rdkit.types import RdkitMol
from sqlalchemy import create_engine, text
from sqlmodel import Field, Session, SQLModel, select

from tricycle_reaction_db.core.config import get_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


class MoleculeProbe(SQLModel, table=True):
    __tablename__ = "_m1_molecule_probe"
    __table_args__ = (RdkitIndex("ix_m1_molecule_probe_structure", "structure"),)

    id: int | None = Field(default=None, primary_key=True)
    structure: str = Field(
        sa_type=RdkitMol(return_type="smiles"),
        nullable=False,
    )


def test_rdkit_extension_and_molalchemy_round_trip() -> None:
    database_engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    table = MoleculeProbe.__table__

    try:
        with database_engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS rdkit"))
            table.drop(connection, checkfirst=True)
            table.create(connection)

        with Session(database_engine) as session:
            session.add(MoleculeProbe(structure="C[C@H](O)F"))
            session.commit()

            exact = session.exec(
                select(MoleculeProbe).where(rdkit_col(MoleculeProbe.structure).equals("C[C@H](O)F"))
            ).one()
            substructure = session.exec(
                select(MoleculeProbe).where(
                    rdkit_col(MoleculeProbe.structure).has_substructure("CO")
                )
            ).one()

            assert exact.structure == "C[C@H](O)F"
            assert substructure.id == exact.id

        with database_engine.connect() as connection:
            extension_version = connection.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'rdkit'")
            ).scalar_one()
            index_definition = connection.execute(
                text(
                    """
                    SELECT indexdef
                    FROM pg_indexes
                    WHERE tablename = '_m1_molecule_probe'
                      AND indexname = 'ix_m1_molecule_probe_structure'
                    """
                )
            ).scalar_one()

        assert extension_version
        assert "USING gist" in index_definition
    finally:
        with database_engine.begin() as connection:
            table.drop(connection, checkfirst=True)
        database_engine.dispose()
