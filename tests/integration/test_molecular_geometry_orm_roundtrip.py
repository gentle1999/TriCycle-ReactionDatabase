import os
from collections.abc import Iterator

import numpy as np
import pytest
from molalchemy.rdkit.types import RdkitMol
from pydantic import ConfigDict
from rdkit import Chem
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import defer, undefer
from sqlmodel import Field, Session, SQLModel, select

from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.types import NumpyArray

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


class MolecularGeometryProbe(SQLModel, table=True):
    __tablename__ = "_m1_molecular_geometry_probe"
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: int | None = Field(default=None, primary_key=True)
    topology: Chem.Mol = Field(
        sa_type=RdkitMol(return_type="mol"),
        nullable=False,
    )
    coordinates: np.ndarray = Field(
        sa_type=NumpyArray(max_inline_array_bytes=1024 * 1024),
        nullable=False,
    )


@pytest.fixture
def database_engine() -> Iterator[Engine]:
    database_engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    table = MolecularGeometryProbe.__table__

    try:
        with database_engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS rdkit"))
            table.drop(connection, checkfirst=True)
            table.create(connection)
        yield database_engine
    finally:
        with database_engine.begin() as connection:
            table.drop(connection, checkfirst=True)
        database_engine.dispose()


def _source_payload() -> tuple[Chem.Mol, np.ndarray]:
    topology = Chem.MolFromSmiles("[13CH3:1][C@@H:2](F)/C=C\\[2H:9]")
    assert topology is not None
    coordinates = np.asarray(
        [
            [0.1234567890123, -0.2345678901234, 0.3456789012345],
            [1.4567890123456, -1.5678901234567, 1.6789012345678],
            [2.7890123456789, -2.8901234567890, 2.9012345678901],
            [3.0123456789012, -3.1234567890123, 3.2345678901234],
            [4.3456789012345, -4.4567890123456, 4.5678901234567],
            [5.6789012345678, -5.7890123456789, 5.8901234567890],
        ],
        dtype=np.float64,
    )
    assert coordinates.shape == (topology.GetNumAtoms(), 3)
    return topology, coordinates


def test_chem_mol_and_numpy_array_round_trip_together(database_engine: Engine) -> None:
    source_topology, source_coordinates = _source_payload()

    with Session(database_engine) as session:
        session.add(
            MolecularGeometryProbe(
                topology=source_topology,
                coordinates=source_coordinates,
            )
        )
        session.commit()

    with Session(database_engine) as session:
        loaded = session.exec(select(MolecularGeometryProbe)).one()

        assert isinstance(loaded.topology, Chem.Mol)
        assert Chem.MolToCXSmiles(loaded.topology, canonical=False) == Chem.MolToCXSmiles(
            source_topology, canonical=False
        )
        assert isinstance(loaded.coordinates, np.ndarray)
        assert loaded.coordinates.dtype == source_coordinates.dtype
        assert loaded.coordinates.shape == source_coordinates.shape
        assert not loaded.coordinates.flags.writeable
        np.testing.assert_array_equal(loaded.coordinates, source_coordinates)

    with database_engine.connect() as connection:
        column_types = dict(
            connection.execute(
                text(
                    """
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_name = '_m1_molecular_geometry_probe'
                    """
                )
            ).all()
        )

    assert column_types["topology"] == "USER-DEFINED"
    assert column_types["coordinates"] == "bytea"


def test_matrix_payload_can_be_explicitly_deferred(database_engine: Engine) -> None:
    source_topology, source_coordinates = _source_payload()

    with Session(database_engine) as session:
        session.add(
            MolecularGeometryProbe(
                topology=source_topology,
                coordinates=source_coordinates,
            )
        )
        session.commit()

    with Session(database_engine) as session:
        loaded_without_coordinates = session.exec(
            select(MolecularGeometryProbe).options(
                defer(MolecularGeometryProbe.coordinates, raiseload=True)
            )
        ).one()

        assert "coordinates" in inspect(loaded_without_coordinates).unloaded
        with pytest.raises(InvalidRequestError, match="raiseload"):
            _ = loaded_without_coordinates.coordinates

    with Session(database_engine) as session:
        loaded_with_coordinates = session.exec(
            select(MolecularGeometryProbe).options(undefer(MolecularGeometryProbe.coordinates))
        ).one()

        np.testing.assert_array_equal(loaded_with_coordinates.coordinates, source_coordinates)
