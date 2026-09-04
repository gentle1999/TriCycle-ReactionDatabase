import os

import pytest
from rdkit import Chem
from sqlalchemy import create_engine
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.services.molecular_geometry import (
    persist_molecular_topology,
)
from tricycle_reaction_db.application.services.topology_abstraction import (
    assigned_stereo_features,
    ensure_topology_upstreams,
    find_upstream_topologies,
    persist_stereo_abstraction_projection,
    specialized_topologies,
    specialized_topology_ids,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import MolecularTopologyAbstraction
from tricycle_reaction_db.ingestion.normalization import normalize_topology

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


def test_persisted_stereo_abstraction_is_a_dag() -> None:
    molecule = Chem.MolFromSmiles("F[C@H](Cl)[C@H](Br)I")
    assert molecule is not None
    record = normalize_topology(
        molecule,
        add_hydrogens=False,
        reconstruction_method="tests/topology-abstraction",
        reconstruction_version="1",
    )
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)

    try:
        with Session(engine) as session:
            persisted = persist_molecular_topology(session, record)
            assert persisted.topology.is_stereo_abstraction_upstream is False
            abstraction_edge_ids_before_ensure = set(
                session.exec(select(MolecularTopologyAbstraction.id)).all()
            )
            assert ensure_topology_upstreams(session, persisted.topology) == (persisted.topology,)
            abstraction_edge_ids_after_ensure = set(
                session.exec(select(MolecularTopologyAbstraction.id)).all()
            )
            assert abstraction_edge_ids_after_ensure == abstraction_edge_ids_before_ensure
            features = assigned_stereo_features(persisted.topology.mol)
            assert len(features) == 2
            one_center_a, edge_a = persist_stereo_abstraction_projection(
                session,
                persisted.topology,
                (features[0],),
            )
            one_center_b, edge_b = persist_stereo_abstraction_projection(
                session,
                persisted.topology,
                (features[1],),
            )
            zero_center_a, edge_a_zero = persist_stereo_abstraction_projection(
                session,
                one_center_a,
                assigned_stereo_features(one_center_a.mol),
            )
            zero_center_b, edge_b_zero = persist_stereo_abstraction_projection(
                session,
                one_center_b,
                assigned_stereo_features(one_center_b.mol),
            )
            session.flush()

            assert one_center_a.is_stereo_abstraction_upstream is True
            assert one_center_b.is_stereo_abstraction_upstream is True
            assert zero_center_a.is_stereo_abstraction_upstream is True

            alternate_molecule = Chem.MolFromSmiles("F[C@@H](Cl)[C@H](Br)I")
            assert alternate_molecule is not None
            alternate_record = normalize_topology(
                alternate_molecule,
                add_hydrogens=False,
                reconstruction_method="tests/topology-abstraction-alternate",
                reconstruction_version="1",
            )
            alternate = persist_molecular_topology(session, alternate_record)
            upstreams = find_upstream_topologies(session, alternate.topology)
            assert one_center_a.id in {topology.id for topology in upstreams}
            assert (
                session.exec(
                    select(MolecularTopologyAbstraction).where(
                        col(MolecularTopologyAbstraction.specific_topology_id)
                        == alternate.topology.id,
                        col(MolecularTopologyAbstraction.general_topology_id) == one_center_a.id,
                    )
                ).first()
                is not None
            )

            edges = (edge_a, edge_b, edge_a_zero, edge_b_zero)
            assert len(edges) == 4
            assert zero_center_a.id == zero_center_b.id
            zero_center_id = zero_center_a.id
            assert zero_center_id is not None
            reachable_ids = specialized_topology_ids(session, zero_center_id)
            loaded = specialized_topologies(session, zero_center_id, include_general=True)

            assert len(reachable_ids) == 4
            assert persisted.topology.id in reachable_ids
            assert len(loaded) == 5

            repeated_a = persist_stereo_abstraction_projection(
                session,
                persisted.topology,
                (features[0],),
            )
            repeated_b = persist_stereo_abstraction_projection(
                session,
                persisted.topology,
                (features[1],),
            )
            assert {repeated_a[1].id, repeated_b[1].id} == {edge_a.id, edge_b.id}

            session.rollback()
    finally:
        engine.dispose()
