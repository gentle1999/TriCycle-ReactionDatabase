"""All logged candidates contribute cost, independently of energy screening."""

import os
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, select
from test_domain_query_filters import _create_domain_sample, _delete_domain_sample

from tricycle_reaction_db.application.services.mapped_reaction_runtime import (
    load_mapped_reaction_runtimes,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    CalculationFrame,
    Geometry,
    LogicalReactionParticipant,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionParticipant,
)
from tricycle_reaction_db.domain.enums import OptimizationStatus, StorageStatus

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="requires database tests",
    ),
]


def test_runtime_includes_unselected_unconverged_endpoints_and_all_bound_ts() -> None:
    engine = create_engine(get_settings().database_url)
    samples = []
    cleanup_samples = []
    candidate_frame = candidate_geometry = None
    try:
        with Session(engine, expire_on_commit=False) as session:
            samples = [_create_domain_sample(session) for _ in range(3)]
            cleanup_samples = [
                tuple(type(entity)(id=entity.id) for entity in sample) for sample in samples
            ]
            first, second, third = samples
            mapping = first[6]
            for sample, runtime in zip(samples, [100.0, 200.0, 300.0], strict=True):
                sample[11].running_time_seconds = runtime
                session.add(sample[11])
            participants = session.exec(
                select(LogicalReactionParticipant).where(
                    LogicalReactionParticipant.logical_reaction_id == mapping.logical_reaction_id
                )
            ).all()
            for p in participants:
                session.add(
                    MappedReactionParticipant(
                        mapped_reaction_id=mapping.id,
                        logical_reaction_participant_id=p.id,
                        concrete_topology_id=first[5].id,
                        side=p.side,
                        template_index=0,
                        atom_map_numbers=[1, 2],
                        mapped_smiles="[H:1][H:2]",
                    )
                )
            # Another observed geometry of the exact precursor/product graph
            # comes from a different file and did not converge.
            geometry_values = first[4].model_dump(exclude={"id", "created_at"})
            geometry_values.update(geometry_hash=sha256(uuid4().bytes).hexdigest())
            candidate_geometry = Geometry(id=uuid4(), **geometry_values)
            session.add(candidate_geometry)
            session.flush()
            frame_values = second[1].model_dump(exclude={"id", "created_at"})
            frame_values.update(
                file_frame_index=1,
                frame_index=1,
                geometry_id=candidate_geometry.id,
                topology_id=first[5].id,
                topology_derivation_id=first[12].id,
                optimization_status=OptimizationStatus.NOT_CONVERGED,
            )
            candidate_frame = CalculationFrame(id=uuid4(), **frame_values)
            session.add(candidate_frame)
            node = session.exec(
                select(MappedReactionNode).where(
                    MappedReactionNode.mapped_reaction_id == mapping.id
                )
            ).one()
            session.add(
                MappedReactionNodeGeometry(
                    mapped_reaction_node_id=node.id,
                    geometry_id=third[4].id,
                    component_key="transition-state",
                    component_index=0,
                    coordinate_index=1,
                    is_primary=False,
                )
            )
            session.flush()
            times = load_mapped_reaction_runtimes(session, [mapping.id])[mapping.id]
            assert times == {
                "reactants_running_time_seconds": 300.0,
                "transition_state_running_time_seconds": 400.0,
                "products_running_time_seconds": 300.0,
                "total_running_time_seconds": 600.0,
            }
            second[11].running_time_seconds = None
            session.add(second[11])
            session.flush()
            times = load_mapped_reaction_runtimes(session, [mapping.id])[mapping.id]
            assert times["total_running_time_seconds"] is None
            assert times["transition_state_running_time_seconds"] == 400.0
            second[10].storage_status = StorageStatus.RETIRED
            session.add(second[10])
            session.flush()
            times = load_mapped_reaction_runtimes(session, [mapping.id])[mapping.id]
            assert times["reactants_running_time_seconds"] == 100.0
            assert times["total_running_time_seconds"] == 400.0
            session.rollback()
    finally:
        with Session(engine) as session:
            for sample in cleanup_samples:
                _delete_domain_sample(session, sample)
        engine.dispose()
