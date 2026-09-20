"""Real RustFS-to-worker regressions for extreme Gaussian source files."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlmodel import col, select

from tricycle_reaction_db.application.services import (
    artifact_uploads,
    project_data_removal,
    thermodynamic_profile_refresh,
    upload_batches,
)
from tricycle_reaction_db.application.services.artifact_uploads import (
    ArtifactUploadPayload,
    ArtifactUploadService,
)
from tricycle_reaction_db.application.services.project_data_removal import (
    ProjectDataRemovalService,
)
from tricycle_reaction_db.application.services.thermodynamic_profile_refresh import (
    refresh_dirty_mapped_reaction_profiles,
)
from tricycle_reaction_db.core.config import Settings
from tricycle_reaction_db.db.models import (
    ArtifactIngestion,
    CalculationFrame,
    CalculationSegment,
    MappedReaction,
    MappedReactionThermodynamicProfile,
    MappedReactionThermodynamicProfileRefreshJob,
    MolecularTopologyAbstraction,
    ParseRevision,
    Project,
    ProjectMembership,
    TransitionStateEndpoint,
    TransitionStateInference,
    UploadBatch,
    UploadBatchItem,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.dev import upload_worker as upload_worker_module
from tricycle_reaction_db.dev.upload_worker import UploadBatchWorker
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactKind,
    ProjectRole,
    ThermodynamicProfileRefreshJobStatus,
    TransitionStateEndpointDirection,
    TransitionStateInferenceStatus,
    UploadBatchItemStatus,
    UploadBatchStatus,
)
from tricycle_reaction_db.domain.identity import DEVELOPMENT_USER_ID, SYSTEM_ORGANIZATION_ID

pytestmark = [
    pytest.mark.integration,
    pytest.mark.rustfs,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1"
        or os.getenv("TRICYCLE_RUN_RUSTFS_TESTS") != "1",
        reason="set database and RustFS integration flags to run staged MolOP tests",
    ),
]

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures/real_world_extremes"
REAL_FILES = (
    (FIXTURE_ROOT / "1-s2.0-S2451929422005617-mmc2__24.log.gz", 77, (76, 1)),
    (FIXTURE_ROOT / "1-s2.0-S2451929422005617-mmc2__26.log.gz", 219, (218, 1)),
)


@pytest.fixture(autouse=True)
def close_shared_molop_pool_after_test() -> Iterator[None]:
    yield
    asyncio.run(artifact_uploads.close_molop_process_pool())


@pytest.mark.asyncio
async def test_real_extreme_files_stage_then_share_worker_microbatch_and_profile_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise real staging, shared parsing, one DB microbatch, and deferred profiles."""

    settings = Settings(
        _env_file=None,
        molop_batch_n_jobs=1,
        molop_file_parse_timeout_seconds=60.0,
        molop_file_parse_timeout_size_multiplier=1.5,
        upload_max_concurrency=1,
        upload_worker_prefetch_files=1,
        upload_worker_persistence_batch_files=2,
        upload_worker_persistence_frame_limit=1_000,
        upload_worker_lease_seconds=3_600,
    )
    monkeypatch.setattr(artifact_uploads, "get_settings", lambda: settings)
    monkeypatch.setattr(upload_worker_module, "get_settings", lambda: settings)
    monkeypatch.setattr(thermodynamic_profile_refresh, "get_settings", lambda: settings)
    monkeypatch.setattr(artifact_uploads, "molop_process_worker_count", lambda: 1)
    monkeypatch.setattr(upload_worker_module, "molop_process_worker_count", lambda: 1)
    monkeypatch.setattr(
        upload_worker_module,
        "refresh_project_statistics",
        _skip_unrelated_statistics_refresh,
    )
    monkeypatch.setattr(
        project_data_removal,
        "refresh_project_statistics",
        _skip_project_statistics_refresh,
    )

    project_id = uuid4()
    project_slug = f"real-world-extreme-{project_id.hex}"
    project_created = False
    try:
        async with session_factory() as session:
            session.add_all(
                [
                    Project(
                        id=project_id,
                        organization_id=SYSTEM_ORGANIZATION_ID,
                        owner_user_id=DEVELOPMENT_USER_ID,
                        created_by_user_id=DEVELOPMENT_USER_ID,
                        slug=project_slug,
                        name="Real-world extreme ingestion integration test",
                    ),
                    ProjectMembership(
                        project_id=project_id,
                        user_id=DEVELOPMENT_USER_ID,
                        role=ProjectRole.MANAGER,
                    ),
                ]
            )
            await session.commit()
        project_created = True

        submissions = []
        for source_path, _frame_count, _segment_frame_counts in REAL_FILES:
            submissions.append(
                await upload_batches.UploadBatchService.create_and_stage(
                    files=[
                        ArtifactUploadPayload(
                            source_path.name.removesuffix(".gz"),
                            "application/gzip",
                            source_path.read_bytes(),
                        )
                    ],
                    artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                    project_id=project_id,
                    user_id=DEVELOPMENT_USER_ID,
                )
            )

        artifact_ids = tuple(submission.items[0].artifact_file_id for submission in submissions)
        item_ids = tuple(submission.items[0].id for submission in submissions)
        batch_ids = tuple(submission.batch.id for submission in submissions)
        assert all(isinstance(artifact_id, UUID) for artifact_id in artifact_ids)
        assert len(set(batch_ids)) == 2
        assert all(
            item.status is UploadBatchItemStatus.STAGED
            and item.ingestion_status is ArtifactIngestionStatus.PENDING
            for submission in submissions
            for item in submission.items
        )

        processing_observations: dict[UUID, ArtifactIngestionStatus] = {}
        original_parse = ArtifactUploadService.parse_staged_artifact

        async def observe_claimed_parse(artifact_id: UUID) -> object:
            async with session_factory() as session:
                ingestion = (
                    await session.exec(
                        select(ArtifactIngestion).where(
                            col(ArtifactIngestion.artifact_file_id) == artifact_id
                        )
                    )
                ).one()
                processing_observations[artifact_id] = ingestion.status
            return await original_parse(artifact_id)

        monkeypatch.setattr(
            ArtifactUploadService,
            "parse_staged_artifact",
            staticmethod(observe_claimed_parse),
        )

        persistence_batches: list[tuple[UUID, ...]] = []
        persistence_defer_flags: list[bool] = []
        original_persist = ArtifactUploadService.persist_parsed_microbatch

        async def observe_persistence_microbatch(*args: Any, **kwargs: Any) -> Any:
            parsed_tasks = args[0]
            persistence_batches.append(tuple(task.artifact_id for task in parsed_tasks))
            persistence_defer_flags.append(bool(kwargs["defer_thermodynamic_refresh"]))
            return await original_persist(*args, **kwargs)

        monkeypatch.setattr(
            ArtifactUploadService,
            "persist_parsed_microbatch",
            staticmethod(observe_persistence_microbatch),
        )

        assert await asyncio.wait_for(
            UploadBatchWorker()._run_streaming_cycle(),
            timeout=600,
        )
        assert set(processing_observations.values()) == {ArtifactIngestionStatus.PROCESSING}
        assert set(processing_observations) == set(artifact_ids)
        assert len(persistence_batches) == 1
        assert set(persistence_batches[0]) == set(artifact_ids)
        assert persistence_defer_flags == [True]

        revision_ids: list[UUID] = []
        mapped_reaction_ids: set[UUID] = set()
        async with session_factory() as session:
            for index, (artifact_id, item_id, batch_id) in enumerate(
                zip(artifact_ids, item_ids, batch_ids, strict=True)
            ):
                assert isinstance(artifact_id, UUID)
                assert isinstance(item_id, UUID)
                assert isinstance(batch_id, UUID)
                item = await session.get(UploadBatchItem, item_id)
                batch = await session.get(UploadBatch, batch_id)
                ingestion = (
                    await session.exec(
                        select(ArtifactIngestion).where(
                            col(ArtifactIngestion.artifact_file_id) == artifact_id
                        )
                    )
                ).one()
                revision = (
                    await session.exec(
                        select(ParseRevision).where(
                            col(ParseRevision.artifact_file_id) == artifact_id
                        )
                    )
                ).one()
                assert item is not None
                assert batch is not None
                assert item.status is UploadBatchItemStatus.SUCCEEDED
                assert batch.status is UploadBatchStatus.COMPLETED
                assert ingestion.status is ArtifactIngestionStatus.SUCCEEDED
                assert ingestion.source_frame_count == REAL_FILES[index][1]
                revision_id = revision.id
                assert isinstance(revision_id, UUID)
                revision_ids.append(revision_id)

                segments = (
                    await session.exec(
                        select(CalculationSegment)
                        .where(col(CalculationSegment.parse_revision_id) == revision_id)
                        .order_by(col(CalculationSegment.segment_index))
                    )
                ).all()
                assert (
                    tuple(segment.source_frame_count for segment in segments)
                    == REAL_FILES[index][2]
                )
                frame_indices = (
                    await session.exec(
                        select(CalculationFrame.file_frame_index)
                        .where(col(CalculationFrame.parse_revision_id) == revision_id)
                        .order_by(col(CalculationFrame.file_frame_index))
                    )
                ).all()
                assert frame_indices == list(range(REAL_FILES[index][1]))

            large_inferences = (
                await session.exec(
                    select(TransitionStateInference).where(
                        col(TransitionStateInference.parse_revision_id) == revision_ids[1]
                    )
                )
            ).all()
            assert len(large_inferences) == 1
            inference = large_inferences[0]
            assert inference.file_frame_index == 218
            assert inference.status is TransitionStateInferenceStatus.SUCCEEDED
            assert isinstance(inference.mapped_reaction_id, UUID)
            assert isinstance(inference.calculation_frame_id, UUID)
            mapped_reaction_ids.add(inference.mapped_reaction_id)

            endpoint_directions = (
                await session.exec(
                    select(TransitionStateEndpoint.direction).where(
                        col(TransitionStateEndpoint.calculation_frame_id)
                        == inference.calculation_frame_id
                    )
                )
            ).all()
            assert set(endpoint_directions) == {
                TransitionStateEndpointDirection.NEGATIVE,
                TransitionStateEndpointDirection.POSITIVE,
            }

            # The refresh API drains all due work for the project. Track every
            # mapped reaction created by these fixtures, not only the large
            # file's endpoint inference, so the assertion matches that scope.
            project_reaction_ids = (
                await session.exec(
                    select(MappedReaction.id).where(col(MappedReaction.project_id) == project_id)
                )
            ).all()
            mapped_reaction_ids.update(
                reaction_id for reaction_id in project_reaction_ids if isinstance(reaction_id, UUID)
            )
            assert inference.mapped_reaction_id in mapped_reaction_ids

            dag_edges = (
                await session.exec(
                    select(MolecularTopologyAbstraction).where(
                        col(MolecularTopologyAbstraction.project_id) == project_id
                    )
                )
            ).all()
            assert dag_edges, "the real segmented file must build its topology DAG before refresh"

            refresh_jobs = (
                await session.exec(
                    select(MappedReactionThermodynamicProfileRefreshJob).where(
                        col(MappedReactionThermodynamicProfileRefreshJob.mapped_reaction_id).in_(
                            mapped_reaction_ids
                        )
                    )
                )
            ).all()
            assert len(refresh_jobs) == len(mapped_reaction_ids)
            assert all(
                job.status is ThermodynamicProfileRefreshJobStatus.PENDING for job in refresh_jobs
            )

        # Normal ingestion requests are deliberately debounced so adjacent
        # uploads can coalesce. Exercise the real due-time instead of forcing
        # a manual high-priority refresh that would bypass queue scheduling.
        await asyncio.sleep(settings.upload_worker_profile_refresh_debounce_seconds + 0.05)

        profile_materializations: list[tuple[UUID, int, int]] = []
        original_refresh_profiles = (
            thermodynamic_profile_refresh.refresh_mapped_reactions_thermodynamics
        )

        def observe_profile_materialization(
            session: Any,
            mapped_reactions: Any,
            *,
            clear_refresh_jobs: bool = True,
        ) -> Any:
            result = original_refresh_profiles(
                session,
                mapped_reactions,
                clear_refresh_jobs=clear_refresh_jobs,
            )
            profile_materializations.extend(
                (
                    reaction.id,
                    reaction.thermodynamic_profile_generation,
                    reaction.thermodynamic_profile_materialized_generation,
                )
                for reaction in mapped_reactions
                if isinstance(reaction.id, UUID)
            )
            return result

        monkeypatch.setattr(
            thermodynamic_profile_refresh,
            "refresh_mapped_reactions_thermodynamics",
            observe_profile_materialization,
        )
        assert await asyncio.wait_for(
            refresh_dirty_mapped_reaction_profiles(
                (project_id,),
                reason="real-world-rustfs-ingestion-regression",
            ),
            timeout=300,
        )
        assert {row[0] for row in profile_materializations} == mapped_reaction_ids
        async with session_factory() as session:
            for mapped_reaction_id in mapped_reaction_ids:
                reaction = await session.get(MappedReaction, mapped_reaction_id)
                refresh_job = await session.get(
                    MappedReactionThermodynamicProfileRefreshJob,
                    mapped_reaction_id,
                )
                assert reaction is not None
                assert (
                    reaction.thermodynamic_profile_materialized_generation
                    == reaction.thermodynamic_profile_generation
                ), (
                    f"profile refresh did not materialize reaction {mapped_reaction_id}: "
                    f"in_memory={profile_materializations}, "
                    f"persisted=({reaction.thermodynamic_profile_generation}, "
                    f"{reaction.thermodynamic_profile_materialized_generation}), "
                    f"job={refresh_job!r}"
                )
                assert refresh_job is None
                profile_rows = (
                    await session.exec(
                        select(MappedReactionThermodynamicProfile).where(
                            col(MappedReactionThermodynamicProfile.mapped_reaction_id)
                            == mapped_reaction_id
                        )
                    )
                ).all()
                assert profile_rows
    finally:
        if project_created:
            await ProjectDataRemovalService.clear(
                project_id,
                user_id=DEVELOPMENT_USER_ID,
                confirmation=project_slug,
            )
            async with session_factory() as session:
                project = await session.get(Project, project_id)
                if project is not None:
                    await session.delete(project)
                    await session.commit()


async def _skip_unrelated_statistics_refresh(
    _project_ids: tuple[UUID, ...],
    *,
    reason: str,
) -> bool:
    assert reason == "upload-worker-queue-drained"
    return True


async def _skip_project_statistics_refresh(
    _project_ids: tuple[UUID, ...],
    *,
    reason: str,
) -> bool:
    assert reason == "project-data-removal"
    return True
