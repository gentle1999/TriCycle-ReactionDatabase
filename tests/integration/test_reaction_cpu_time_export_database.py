"""Group costs union files across mappings, independent of the exported subset."""

import csv
import io
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete
from sqlmodel import Session, col, select
from test_domain_query_filters import _create_domain_sample

from tricycle_reaction_db.application.services import reaction_thermodynamic_analytics as analytics
from tricycle_reaction_db.application.services.reaction_cpu_time_export import ReactionCpuTimeExport
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    LogicalReactionParticipant,
    MappedReaction,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionParticipant,
    MappedReactionThermodynamicProfile,
)
from tricycle_reaction_db.domain.enums import StorageStatus
from tricycle_reaction_db.domain.identity import SYSTEM_PROJECT_ID

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1", reason="database tests disabled"
    ),
]


@pytest.mark.asyncio
async def test_cpu_export_unions_all_group_members_and_ignores_pagination(monkeypatch):
    # External transaction contains fixture commits and is always rolled back.
    service = analytics.ReactionThermodynamicAnalyticsService
    engine = create_engine(get_settings().database_url)
    with engine.connect() as connection:
        outer = connection.begin()
        try:
            with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                samples = [_create_domain_sample(session) for _ in range(3)]
                ids = [sample[6].id for sample in samples]
                visible = col(MappedReaction.id).in_(ids)
                for index, sample in enumerate(samples):
                    revision = sample[11]
                    revision.cpu_time_seconds = (index + 1) * 3600.0
                    revision.cpu_time_source = "observed"
                    revision.running_time_seconds = 10.0
                    revision.wall_time_seconds = 10.0
                    session.add(revision)
                    slots = session.exec(
                        select(LogicalReactionParticipant).where(
                            LogicalReactionParticipant.logical_reaction_id == sample[7].id
                        )
                    ).all()
                    for slot in slots:
                        session.add(
                            MappedReactionParticipant(
                                mapped_reaction_id=sample[6].id,
                                logical_reaction_participant_id=slot.id,
                                concrete_topology_id=sample[5].id,
                                side=slot.side,
                                template_index=0,
                                atom_map_numbers=[1, 2],
                                mapped_smiles="[H:1][H:2]",
                            )
                        )
                # Two mappings witness the same two TS files. File identity,
                # rather than the sum of per-mapping totals, defines group cost.
                for target, source in [(0, 1), (1, 0)]:
                    node = session.exec(
                        select(MappedReactionNode).where(
                            MappedReactionNode.mapped_reaction_id == ids[target]
                        )
                    ).one()
                    session.add(
                        MappedReactionNodeGeometry(
                            mapped_reaction_node_id=node.id,
                            geometry_id=samples[source][4].id,
                            component_key="transition-state",
                            component_index=0,
                            coordinate_index=1,
                            is_primary=False,
                        )
                    )
                session.flush()
                costs = ReactionCpuTimeExport(session, SYSTEM_PROJECT_ID, visible)
                first = costs.load(session, [ids[0]])[ids[0]]
                assert first["reactants_cpu_time_hours"] == 1
                assert first["transition_state_cpu_time_hours"] == 3
                assert first["total_cpu_time_hours"] == 3
                assert first["substrate_pair_reaction_count"] == 3
                for stage in ("reactants", "transition_state", "products", "total"):
                    assert first[f"substrate_pair_{stage}_cpu_time_hours"] == 6
                other = costs.load(session, [ids[2]])[ids[2]]
                assert other["substrate_pair_key"] == first["substrate_pair_key"]
                assert other["substrate_pair_total_cpu_time_hours"] == 6

                # A member with source files but no energy profile still
                # contributes to the shared cost of exported members.
                session.execute(
                    delete(MappedReactionThermodynamicProfile).where(
                        col(MappedReactionThermodynamicProfile.mapped_reaction_id) == ids[2]
                    )
                )
                session.flush()

                # Exercise the CSV's actual SQL filter/limit/offset using the
                # same rollback-only connection and real ORM source queries.
                class Stream:
                    def __init__(self, statement):
                        self.rows = session.exec(statement).all()

                    async def partitions(self, size):
                        for start in range(0, len(self.rows), size):
                            yield self.rows[start : start + size]

                class AsyncSession:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *args):
                        pass

                    async def stream(self, statement):
                        return Stream(statement)

                    async def run_sync(self, callback):
                        return callback(session)

                monkeypatch.setattr(analytics, "session_factory", AsyncSession)
                for predicate, offset in [(col(MappedReaction.id) == ids[0], 0), (visible, 1)]:
                    payload = "".join(
                        [
                            chunk
                            async for chunk in service._export_csv_rows(
                                predicate,
                                visible_mappings=visible,
                                project_id=SYSTEM_PROJECT_ID,
                                frontend_base_url="https://example.test",
                                limit=1,
                                offset=offset,
                            )
                        ]
                    )
                    rows = list(csv.DictReader(io.StringIO(payload)))
                    assert len(rows) == 1
                    assert rows[0]["substrate_pair_total_cpu_time_hours"] == "6.0"
                    assert rows[0]["substrate_pair_reaction_count"] == "3"

                # Export reads current file CPU time; it neither falls back to
                # wall/frame time nor needs the thermodynamic queue to refresh.
                samples[1][11].cpu_time_seconds = None
                samples[1][11].cpu_time_source = None
                session.add(samples[1][11])
                session.flush()
                current = ReactionCpuTimeExport(session, SYSTEM_PROJECT_ID, visible)
                assert (
                    current.load(session, [ids[2]])[ids[2]]["substrate_pair_total_cpu_time_hours"]
                    is None
                )
                samples[1][10].storage_status = StorageStatus.RETIRED
                session.add(samples[1][10])
                session.flush()
                current = ReactionCpuTimeExport(session, SYSTEM_PROJECT_ID, visible)
                assert (
                    current.load(session, [ids[0]])[ids[0]]["substrate_pair_total_cpu_time_hours"]
                    == 4
                )
                # An inaccessible mapping's file must not be added by grouping.
                restricted = ReactionCpuTimeExport(
                    session, SYSTEM_PROJECT_ID, col(MappedReaction.id) == ids[2]
                )
                assert (
                    restricted.load(session, [ids[2]])[ids[2]]["substrate_pair_reaction_count"] == 1
                )
                assert not ReactionCpuTimeExport(session, uuid4(), visible).members
        finally:
            outer.rollback()
    engine.dispose()
