"""Regression tests for durable thermodynamic profile refreshes."""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, delete, text
from sqlmodel import Session, col, select
from test_domain_query_filters import _create_domain_sample, _delete_domain_sample

from tricycle_reaction_db.application.services import (
    mapped_reaction_thermodynamics_persistence,
    thermodynamic_profile_refresh,
)
from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence import (
    MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION,
    enqueue_mapped_reaction_profile_refresh,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    CalculationFrame,
    LogicalReaction,
    LogicalReactionParticipant,
    MappedReaction,
    MappedReactionEdge,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionParticipant,
    MappedReactionThermodynamicProfile,
    MappedReactionThermodynamicProfileRefreshJob,
    MappedReactionThermodynamicProfileSource,
    Organization,
    Project,
    ThermochemistryResult,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import (
    LogicalReactionParticipantSide,
    MappedReactionEdgeKind,
    MappedReactionKind,
    MappedReactionNodeRole,
    OptimizationStatus,
    ThermodynamicProfileRefreshJobStatus,
    ThermodynamicProfileSourceVisibility,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _uuid(value: UUID | None, *, label: str) -> UUID:
    if not isinstance(value, UUID):
        raise AssertionError(f"{label} must have a UUID")
    return value


def _create_lock_test_graph(engine: object) -> tuple[UUID, UUID, UUID, UUID]:
    """Create a project with exactly one profile-refresh job."""

    organization_id = uuid4()
    project_id = uuid4()
    logical_reaction_id = uuid4()
    mapped_reaction_id = uuid4()
    suffix = uuid4().hex
    with Session(engine, expire_on_commit=False) as session:
        session.add_all(
            [
                Organization(
                    id=organization_id,
                    slug=f"profile-lock-{suffix}",
                    name="Profile lock regression",
                ),
                Project(
                    id=project_id,
                    organization_id=organization_id,
                    slug="profile-lock-test",
                    name="Profile lock regression project",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                LogicalReaction(
                    id=logical_reaction_id,
                    project_id=project_id,
                    reaction_key=f"profile-lock-{suffix}",
                    reaction_hash=_hash(f"profile-lock-reaction:{suffix}"),
                ),
                MappedReaction(
                    id=mapped_reaction_id,
                    project_id=project_id,
                    logical_reaction_id=logical_reaction_id,
                    mapped_reaction_key="profile-lock-path",
                    mapped_reaction_kind=MappedReactionKind.OTHER,
                    mapped_reaction_smiles="[H:1][H:2]>>[H:1][H:2]",
                    mapping_hash=_hash(f"profile-lock-mapping:{suffix}"),
                ),
            ]
        )
        session.flush()
        mapped_reaction = session.get(MappedReaction, mapped_reaction_id)
        assert mapped_reaction is not None
        enqueue_mapped_reaction_profile_refresh(
            session,
            [mapped_reaction],
            immediate=True,
        )
        session.commit()
    return organization_id, project_id, logical_reaction_id, mapped_reaction_id


async def _delete_lock_test_graph(
    organization_id: UUID,
    project_id: UUID,
    logical_reaction_id: UUID,
) -> None:
    async with session_factory() as session:
        await session.exec(
            delete(LogicalReaction).where(col(LogicalReaction.id) == logical_reaction_id)
        )
        await session.exec(delete(Project).where(col(Project.id) == project_id))
        await session.exec(delete(Organization).where(col(Organization.id) == organization_id))
        await session.commit()


@pytest.mark.asyncio
async def test_profile_calculation_does_not_hold_upload_generation_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source write can advance a generation while profile calculation is paused."""

    sync_engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    organization_id, project_id, logical_reaction_id, mapped_reaction_id = _create_lock_test_graph(
        sync_engine
    )
    started = Event()
    release = Event()
    write_succeeded = Event()
    thread_errors: list[BaseException] = []

    async def run() -> None:
        async with session_factory() as session:
            job = await session.get(
                MappedReactionThermodynamicProfileRefreshJob,
                mapped_reaction_id,
            )
            assert job is not None
            lease_id = uuid4()
            job.status = ThermodynamicProfileRefreshJobStatus.PROCESSING
            job.lease_id = lease_id
            job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            session.add(job)
            await session.commit()
            claimed = thermodynamic_profile_refresh.ClaimedProfileRefreshJob(
                mapped_reaction_id=mapped_reaction_id,
                requested_generation=job.requested_generation,
                lease_id=lease_id,
                lease_expires_at=job.lease_expires_at,
            )

        original_build = (
            mapped_reaction_thermodynamics_persistence._build_mapped_reaction_thermodynamics
        )

        def paused_build(*args: object, **kwargs: object) -> object:
            started.set()
            release.wait(timeout=10)
            return original_build(*args, **kwargs)

        monkeypatch.setattr(
            mapped_reaction_thermodynamics_persistence,
            "_build_mapped_reaction_thermodynamics",
            paused_build,
        )

        def perform_upload_generation_write() -> None:
            try:
                if not started.wait(timeout=10):
                    raise AssertionError("profile refresh did not enter its calculation phase")
                with sync_engine.begin() as connection:
                    connection.execute(text("SET LOCAL lock_timeout = '500ms'"))
                    result = connection.execute(
                        text(
                            "UPDATE mapped_reaction "
                            "SET thermodynamic_profile_generation = "
                            "thermodynamic_profile_generation + 1 "
                            "WHERE id = :mapped_reaction_id"
                        ),
                        {"mapped_reaction_id": mapped_reaction_id},
                    )
                    if result.rowcount != 1:
                        raise AssertionError("upload generation update did not match its reaction")
                write_succeeded.set()
            except BaseException as error:  # pragma: no cover - asserted in the test thread
                thread_errors.append(error)
            finally:
                release.set()

        write_thread = Thread(target=perform_upload_generation_write)
        write_thread.start()
        try:
            completed = await asyncio.wait_for(
                thermodynamic_profile_refresh._process_profile_refresh_jobs((claimed,)),
                timeout=20,
            )
        finally:
            release.set()
            write_thread.join(timeout=10)

        assert completed == 0
        assert not write_thread.is_alive()
        assert not thread_errors, thread_errors
        assert write_succeeded.is_set()

        async with session_factory() as session:
            reaction = await session.get(MappedReaction, mapped_reaction_id)
            job = await session.get(
                MappedReactionThermodynamicProfileRefreshJob,
                mapped_reaction_id,
            )
            assert reaction is not None
            assert job is not None
            assert job.status is ThermodynamicProfileRefreshJobStatus.PENDING
            assert job.requested_generation == reaction.thermodynamic_profile_generation
            assert reaction.thermodynamic_profile_generation == 2

    try:
        await run()
    finally:
        await _delete_lock_test_graph(organization_id, project_id, logical_reaction_id)
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_profile_refresh_replaces_stale_rows_and_coalesces_generations() -> None:
    """Queued refreshes replace stale profiles and materialize the newest generation."""

    sync_engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    sample: tuple[object, ...] | None = None
    mapped_reaction_id: UUID | None = None
    old_profile_id: UUID | None = None
    try:
        with Session(sync_engine, expire_on_commit=False) as session:
            sample = _create_domain_sample(session)
            frame = session.get(
                CalculationFrame,
                _uuid(sample[1].id, label="CalculationFrame"),
            )
            mapped_reaction = session.get(
                MappedReaction,
                _uuid(sample[6].id, label="MappedReaction"),
            )
            logical_reaction = session.get(
                LogicalReaction,
                _uuid(sample[7].id, label="LogicalReaction"),
            )
            geometry_id = _uuid(sample[4].id, label="Geometry")
            assert (
                frame is not None and mapped_reaction is not None and logical_reaction is not None
            )
            assert mapped_reaction.id is not None
            mapped_reaction_id = mapped_reaction.id
            old_profile = session.exec(
                select(MappedReactionThermodynamicProfile).where(
                    col(MappedReactionThermodynamicProfile.mapped_reaction_id) == mapped_reaction.id
                )
            ).one()
            old_profile_id = _uuid(old_profile.id, label="old profile")

            thermochemistry = session.exec(
                select(ThermochemistryResult).where(col(ThermochemistryResult.frame_id) == frame.id)
            ).one()
            thermochemistry.enthalpy_hartree = -0.95
            thermochemistry.entropy_cal_mol_k = 10.0
            frame.optimization_status = OptimizationStatus.CONVERGED
            frame.negative_frequency_count = 0
            frame.lowest_frequency_cm1 = 100.0
            session.add_all([frame, thermochemistry])

            logical_participants = session.exec(
                select(LogicalReactionParticipant).where(
                    col(LogicalReactionParticipant.logical_reaction_id) == logical_reaction.id
                )
            ).all()
            participants_by_side = {
                participant.side: participant for participant in logical_participants
            }
            reactant_logical = participants_by_side[LogicalReactionParticipantSide.REACTANT]
            product_logical = participants_by_side[LogicalReactionParticipantSide.PRODUCT]
            reactant_participant = MappedReactionParticipant(
                id=uuid4(),
                mapped_reaction_id=mapped_reaction.id,
                logical_reaction_participant_id=_uuid(
                    reactant_logical.id,
                    label="reactant logical participant",
                ),
                side=LogicalReactionParticipantSide.REACTANT,
                template_index=0,
                atom_map_numbers=[1, 2],
                mapped_smiles="[H:1][H:2]",
            )
            product_participant = MappedReactionParticipant(
                id=uuid4(),
                mapped_reaction_id=mapped_reaction.id,
                logical_reaction_participant_id=_uuid(
                    product_logical.id,
                    label="product logical participant",
                ),
                side=LogicalReactionParticipantSide.PRODUCT,
                template_index=0,
                atom_map_numbers=[1, 2],
                mapped_smiles="[H:1][H:2]",
            )
            session.add_all([reactant_participant, product_participant])
            session.flush()

            transition_state_node = session.exec(
                select(MappedReactionNode).where(
                    col(MappedReactionNode.mapped_reaction_id) == mapped_reaction.id,
                    col(MappedReactionNode.role) == MappedReactionNodeRole.TRANSITION_STATE,
                )
            ).one()
            reactant_node = MappedReactionNode(
                id=uuid4(),
                mapped_reaction_id=mapped_reaction.id,
                node_key="profile-refresh-reactant",
                node_index=1,
                role=MappedReactionNodeRole.REACTANT,
            )
            product_node = MappedReactionNode(
                id=uuid4(),
                mapped_reaction_id=mapped_reaction.id,
                node_key="profile-refresh-product",
                node_index=2,
                role=MappedReactionNodeRole.PRODUCT,
            )
            session.add_all([reactant_node, product_node])
            session.flush()
            assert reactant_node.id is not None
            assert product_node.id is not None
            assert transition_state_node.id is not None
            session.add_all(
                [
                    MappedReactionNodeGeometry(
                        mapped_reaction_node_id=reactant_node.id,
                        geometry_id=geometry_id,
                        mapped_reaction_participant_id=_uuid(
                            reactant_participant.id,
                            label="reactant mapped participant",
                        ),
                        component_key="reactant",
                        component_index=0,
                    ),
                    MappedReactionNodeGeometry(
                        mapped_reaction_node_id=product_node.id,
                        geometry_id=geometry_id,
                        mapped_reaction_participant_id=_uuid(
                            product_participant.id,
                            label="product mapped participant",
                        ),
                        component_key="product",
                        component_index=0,
                    ),
                    MappedReactionEdge(
                        id=uuid4(),
                        mapped_reaction_id=mapped_reaction.id,
                        edge_key="profile-refresh-elementary-step",
                        source_node_id=reactant_node.id,
                        target_node_id=product_node.id,
                        transition_state_node_id=transition_state_node.id,
                        edge_kind=MappedReactionEdgeKind.ELEMENTARY_STEP,
                    ),
                ]
            )
            enqueue_mapped_reaction_profile_refresh(
                session,
                [mapped_reaction],
                immediate=True,
            )
            enqueue_mapped_reaction_profile_refresh(
                session,
                [mapped_reaction],
                immediate=True,
            )
            session.commit()
            assert mapped_reaction.thermodynamic_profile_generation == 2

        assert mapped_reaction_id is not None
        async with session_factory() as session:
            job = await session.get(
                MappedReactionThermodynamicProfileRefreshJob,
                mapped_reaction_id,
            )
            assert job is not None
            lease_id = uuid4()
            job.status = ThermodynamicProfileRefreshJobStatus.PROCESSING
            job.lease_id = lease_id
            job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            session.add(job)
            await session.commit()
            claimed = thermodynamic_profile_refresh.ClaimedProfileRefreshJob(
                mapped_reaction_id=mapped_reaction_id,
                requested_generation=job.requested_generation,
                lease_id=lease_id,
                lease_expires_at=job.lease_expires_at,
            )

        assert claimed.requested_generation == 2
        assert await thermodynamic_profile_refresh._process_profile_refresh_jobs((claimed,)) == 1

        async with session_factory() as session:
            reaction = await session.get(MappedReaction, mapped_reaction_id)
            assert reaction is not None
            assert reaction.thermodynamic_profile_generation == 2
            assert reaction.thermodynamic_profile_materialized_generation == 2
            assert reaction.thermodynamic_profile_policy_version == (
                MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION
            )
            profiles = (
                await session.exec(
                    select(MappedReactionThermodynamicProfile).where(
                        col(MappedReactionThermodynamicProfile.mapped_reaction_id)
                        == mapped_reaction_id
                    )
                )
            ).all()
            assert len(profiles) == 1
            profile = profiles[0]
            assert _uuid(profile.id, label="new profile") != old_profile_id
            assert profile.source_evidence_complete is True
            assert profile.source_visibility_status is ThermodynamicProfileSourceVisibility.VISIBLE
            assert profile.reactants is not None
            assert profile.transition_state is not None
            assert profile.products is not None
            assert profile.reactants_gibbs_free_energy_hartree == pytest.approx(-0.9)
            assert profile.transition_state_gibbs_free_energy_hartree == pytest.approx(-0.9)
            assert profile.products_gibbs_free_energy_hartree == pytest.approx(-0.9)
            assert profile.reaction_gibbs_free_energy_kcal_mol == pytest.approx(0.0)
            assert reaction.minimum_reaction_gibbs_free_energy_kcal_mol == pytest.approx(0.0)
            sources = (
                await session.exec(
                    select(MappedReactionThermodynamicProfileSource).where(
                        col(MappedReactionThermodynamicProfileSource.profile_id) == profile.id
                    )
                )
            ).all()
            assert len(sources) >= 1
            assert (
                await session.get(
                    MappedReactionThermodynamicProfileRefreshJob,
                    mapped_reaction_id,
                )
                is None
            )
    finally:
        if sample is not None:
            with Session(sync_engine) as session:
                _delete_domain_sample(session, sample)
        sync_engine.dispose()
