import os
from pathlib import Path

import pytest
from rdkit import Chem
from sqlalchemy import create_engine
from sqlmodel import Session

from tricycle_reaction_db.application.services import topology_abstraction
from tricycle_reaction_db.application.services.molecular_geometry import (
    GeometryPersistenceContext,
    persist_molecular_topology,
)
from tricycle_reaction_db.application.services.topology_abstraction import (
    assigned_stereo_features,
    persist_stereo_abstraction_projection,
    topology_dag_components_by_root,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import MolecularTopology
from tricycle_reaction_db.domain.identity import SYSTEM_PROJECT_ID
from tricycle_reaction_db.ingestion.normalization import normalize_topology

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]

ENDPOINT_FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures/real_world_extremes/1-s2.0-S2451929422005617-mmc2__24.endpoints.sdf"
)


def test_real_world_large_ts_endpoint_builds_hash_scoped_stereo_dag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoints = tuple(
        candidate
        for candidate in Chem.SDMolSupplier(str(ENDPOINT_FIXTURE), removeHs=False)
        if candidate is not None
    )
    assert len(endpoints) == 2
    molecule = endpoints[1]
    assert molecule.GetProp("_Name").endswith("positive endpoint")
    record = normalize_topology(
        molecule,
        add_hydrogens=False,
        reconstruction_method="tests/real-world-extremes",
        reconstruction_version="1",
    )
    assert record.topology.atom_count == 78
    assert record.topology.stereo_agnostic_graph_hash is not None

    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            context = GeometryPersistenceContext(project_id=SYSTEM_PROJECT_ID)
            specific = persist_molecular_topology(session, record, context=context).topology
            assert specific.id is not None
            features = assigned_stereo_features(specific.mol)
            assert len(features) == 2
            assert specific.is_stereo_abstraction_upstream is False

            def fail_if_global_graph_matching_is_used(*_: object, **__: object) -> object:
                raise AssertionError(
                    "hash-confirmed real-world DAG projection must not scan molecular graphs"
                )

            monkeypatch.setattr(
                topology_abstraction,
                "find_stereo_abstraction_match",
                fail_if_global_graph_matching_is_used,
            )
            general, edge = persist_stereo_abstraction_projection(
                session,
                specific,
                (features[0],),
                context=context,
                abstraction_metadata={
                    "source_file": "1-s2.0-S2451929422005617-mmc2__24.log",
                    "source_frame_index": 76,
                },
            )
            session.flush()

            assert general.id is not None
            assert general.id != specific.id
            assert general.graph_hash != specific.graph_hash
            assert general.stereo_agnostic_graph_hash == specific.stereo_agnostic_graph_hash
            assert general.is_stereo_abstraction_upstream is True
            assert edge.project_id == SYSTEM_PROJECT_ID
            assert edge.specific_topology_id == specific.id
            assert edge.general_topology_id == general.id

            components = topology_dag_components_by_root(
                session,
                (specific.id, general.id),
                project_id=SYSTEM_PROJECT_ID,
            )
            expected_component = {specific.id, general.id}
            assert set(components[specific.id]) == expected_component
            assert set(components[general.id]) == expected_component
            assert all(
                isinstance(session.get(MolecularTopology, topology_id), MolecularTopology)
                for topology_id in expected_component
            )
            session.rollback()
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()
