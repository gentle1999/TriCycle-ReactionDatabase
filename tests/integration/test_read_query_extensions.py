import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import numpy as np
import pytest
from sqlalchemy import create_engine, delete
from sqlmodel import Session, col
from test_domain_query_filters import (
    _create_domain_sample,
    _delete_domain_sample,
    _fixture_hash,
)

from tricycle_reaction_db.application.dtos import (
    ManifestArtifactBindingRecord,
    WorkflowManifestRecord,
)
from tricycle_reaction_db.application.services import (
    persist_artifact_file,
    persist_manifest_artifact_binding,
    persist_workflow_manifest,
)
from tricycle_reaction_db.application.services.advanced_queries import (
    CalculationResultQueryService,
)
from tricycle_reaction_db.application.services.operational_queries import (
    MolecularTopologyDerivationQueryService,
    StorageGarbageCollectionQueryService,
    WorkflowManifestQueryService,
)
from tricycle_reaction_db.application.services.queries import CalculationQueryService
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    AtomicPopulationSeries,
    CalculationFrame,
    ChargeSpinPopulationResult,
    ElectronicState,
    ElectronicStateSet,
    MolecularOrbitalResult,
    MolecularTopologyDerivation,
    ScientificArray,
    ScientificArrayAssignment,
    StorageGarbageCollectionRun,
    StorageGarbageCollectionState,
    WorkflowManifest,
)
from tricycle_reaction_db.db.session import dispose_engine
from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    ArtifactResolutionStatus,
    ElectronicStateSetKind,
    ManifestArtifactRole,
    ScientificArrayKind,
    StorageGarbageCollectionRunStatus,
    StorageStatus,
    WorkflowManifestStatus,
)
from tricycle_reaction_db.ingestion import artifact_record_from_path

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


def _artifact(
    session: Session,
    path: Path,
    *,
    artifact_kind: ArtifactKind,
) -> ArtifactFile:
    return persist_artifact_file(
        session,
        artifact_record_from_path(
            path,
            bucket=f"read-query-{uuid4().hex}",
            artifact_kind=artifact_kind,
            storage_status=StorageStatus.AVAILABLE,
        ),
    )


def test_manifest_revision_and_binding_queries(tmp_path: Path) -> None:
    first_path = tmp_path / "manifest-v1.json"
    second_path = tmp_path / "manifest-v2.json"
    calculation_path = tmp_path / "calculation.log"
    first_path.write_text('{"revision":1}\n', encoding="utf-8")
    second_path.write_text('{"revision":2}\n', encoding="utf-8")
    calculation_path.write_text("calculation\n", encoding="utf-8")
    manifest_key = f"read-query:{uuid4().hex}"
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    manifest_ids: list[UUID] = []
    artifact_ids: list[UUID] = []
    try:
        with Session(engine) as session:
            first_artifact = _artifact(
                session, first_path, artifact_kind=ArtifactKind.WORKFLOW_MANIFEST
            )
            second_artifact = _artifact(
                session, second_path, artifact_kind=ArtifactKind.WORKFLOW_MANIFEST
            )
            calculation_artifact = _artifact(
                session, calculation_path, artifact_kind=ArtifactKind.CALCULATION_OUTPUT
            )
            first = persist_workflow_manifest(
                session,
                first_artifact,
                WorkflowManifestRecord(
                    manifest_key=manifest_key,
                    revision=1,
                    schema_version="read-query-v1",
                    payload_sha256=first_artifact.content_sha256,
                    qc_policy_version="qc-v1",
                    status=WorkflowManifestStatus.VALIDATED,
                    validation_metadata={"revision": 1},
                ),
            )
            second = persist_workflow_manifest(
                session,
                second_artifact,
                WorkflowManifestRecord(
                    manifest_key=manifest_key,
                    revision=2,
                    schema_version="read-query-v1",
                    payload_sha256=second_artifact.content_sha256,
                    qc_policy_version="qc-v2",
                    status=WorkflowManifestStatus.VALIDATED,
                    validation_metadata={"revision": 2},
                ),
                supersedes=first,
            )
            geometry_binding = persist_manifest_artifact_binding(
                session,
                second,
                ManifestArtifactBindingRecord(
                    artifact_key="geometry",
                    expected_content_sha256=calculation_artifact.content_sha256,
                    artifact_role=ManifestArtifactRole.GAUSSIAN_OPT_FREQ,
                    reaction_key="reaction",
                    path_key="path",
                    node_key="reactant",
                    segment_index=0,
                    frame_index=0,
                    resolution_status=ArtifactResolutionStatus.RESOLVED,
                ),
                artifact_file=calculation_artifact,
            )
            persist_manifest_artifact_binding(
                session,
                second,
                ManifestArtifactBindingRecord(
                    artifact_key="single-point",
                    expected_content_sha256=calculation_artifact.content_sha256,
                    artifact_role=ManifestArtifactRole.ORCA_SINGLE_POINT,
                    reaction_key="reaction",
                    path_key="path",
                    node_key="reactant",
                    segment_index=0,
                    frame_index=1,
                    source_geometry_artifact_key="geometry",
                    resolution_status=ArtifactResolutionStatus.RESOLVED,
                ),
                artifact_file=calculation_artifact,
                source_geometry_binding=geometry_binding,
            )
            session.commit()
            for manifest in (first, second):
                assert manifest.id is not None
                manifest_ids.append(manifest.id)
            for artifact in (first_artifact, second_artifact, calculation_artifact):
                assert artifact.id is not None
                artifact_ids.append(artifact.id)

        page = asyncio.run(
            WorkflowManifestQueryService.list_workflow_manifests(
                manifest_key=manifest_key,
                limit=10,
                offset=0,
            )
        )
        detail = asyncio.run(
            WorkflowManifestQueryService.get_workflow_manifest(workflow_manifest_id=manifest_ids[1])
        )
        reverse = asyncio.run(
            WorkflowManifestQueryService.list_workflow_manifests(
                bound_artifact_file_id=artifact_ids[2],
                limit=10,
                offset=0,
            )
        )
        bindings = asyncio.run(
            WorkflowManifestQueryService.list_manifest_artifact_bindings(
                workflow_manifest_id=manifest_ids[1],
                resolution_status=ArtifactResolutionStatus.RESOLVED,
                limit=10,
                offset=0,
            )
        )
        source = next(item for item in bindings.items if item.artifact_key == "geometry")
        source_detail = asyncio.run(
            WorkflowManifestQueryService.get_manifest_artifact_binding(binding_id=source.id)
        )

        assert [item.revision for item in page.items] == [1, 2]
        assert detail is not None
        assert json.loads(detail.validation_metadata_json) == {"revision": 2}
        assert [item.revision for item in detail.revisions] == [1, 2]
        assert detail.artifact_binding_count == 2
        assert reverse.page.total == 1
        assert bindings.page.total == 2
        assert source_detail is not None
        assert len(source_detail.dependent_binding_ids) == 1
    finally:
        with Session(engine) as session:
            for manifest_id in reversed(manifest_ids):
                stored_manifest = session.get(WorkflowManifest, manifest_id)
                if stored_manifest is not None:
                    session.delete(stored_manifest)
            session.flush()
            for artifact_id in artifact_ids:
                stored_artifact = session.get(ArtifactFile, artifact_id)
                if stored_artifact is not None:
                    session.delete(stored_artifact)
            session.commit()
        engine.dispose()
        asyncio.run(dispose_engine())


def test_storage_gc_state_and_run_queries() -> None:
    now = datetime.now(UTC)
    bucket = f"read-query-gc-{uuid4().hex}"
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    state_id: UUID | None = None
    run_ids: list[UUID] = []
    try:
        with Session(engine) as session:
            state = StorageGarbageCollectionState(
                bucket=bucket,
                root_prefix="uploads",
                watermark_at=now - timedelta(hours=2),
                updated_at=now,
            )
            session.add(state)
            session.flush()
            assert state.id is not None
            state_id = state.id
            succeeded = StorageGarbageCollectionRun(
                state_id=state_id,
                started_at=now - timedelta(hours=1),
                completed_at=now - timedelta(minutes=55),
                scan_after=now - timedelta(hours=3),
                scan_until=now - timedelta(hours=2),
                status=StorageGarbageCollectionRunStatus.SUCCEEDED,
                objects_seen=5,
                objects_deleted=1,
                objects_retained=4,
            )
            failed = StorageGarbageCollectionRun(
                state_id=state_id,
                started_at=now,
                completed_at=now + timedelta(minutes=1),
                scan_after=now - timedelta(hours=2),
                scan_until=now - timedelta(hours=1),
                status=StorageGarbageCollectionRunStatus.FAILED,
                objects_seen=1,
                objects_failed=1,
                error_message="read-query failure",
            )
            session.add(succeeded)
            session.add(failed)
            session.flush()
            assert succeeded.id is not None and failed.id is not None
            state.last_successful_run_id = succeeded.id
            run_ids = [succeeded.id, failed.id]
            session.commit()

        states = asyncio.run(
            StorageGarbageCollectionQueryService.list_storage_gc_states(
                bucket=bucket,
                limit=10,
                offset=0,
            )
        )
        detail = asyncio.run(
            StorageGarbageCollectionQueryService.get_storage_gc_state(
                state_id=state_id,
                recent_run_limit=10,
            )
        )
        failed_runs = asyncio.run(
            StorageGarbageCollectionQueryService.list_storage_gc_runs(
                bucket=bucket,
                status=StorageGarbageCollectionRunStatus.FAILED,
                started_after=now - timedelta(minutes=1),
                started_before=now + timedelta(minutes=1),
                limit=10,
                offset=0,
            )
        )
        failed_detail = asyncio.run(
            StorageGarbageCollectionQueryService.get_storage_gc_run(run_id=run_ids[1])
        )

        assert states.page.total == 1
        assert states.items[0].latest_run_status == "failed"
        assert states.items[0].latest_failed_run_id == run_ids[1]
        assert detail is not None
        assert [run.status for run in detail.recent_runs] == ["failed", "succeeded"]
        assert failed_runs.page.total == 1
        assert failed_detail is not None
        assert failed_detail.error_message == "read-query failure"
    finally:
        if state_id is not None:
            with Session(engine) as session:
                session.execute(
                    delete(StorageGarbageCollectionRun).where(
                        col(StorageGarbageCollectionRun.state_id) == state_id
                    )
                )
                stored_state = session.get(StorageGarbageCollectionState, state_id)
                if stored_state is not None:
                    session.delete(stored_state)
                session.commit()
        engine.dispose()
        asyncio.run(dispose_engine())


def test_advanced_results_and_derivation_queries_use_explicit_fixture(
    development_query_principal: object,
) -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    sample: tuple[Any, ...] | None = None
    try:
        with Session(engine, expire_on_commit=False) as session:
            sample = _create_domain_sample(session)
            frame = sample[1]
            derivation = sample[12]
            assert isinstance(frame, CalculationFrame)
            assert isinstance(derivation, MolecularTopologyDerivation)
            assert frame.id is not None
            assert derivation.id is not None
            orbital_frame_id = frame.id
            state_frame_id = frame.id
            derivation_id = derivation.id

            orbital_result = MolecularOrbitalResult(
                id=uuid4(),
                frame_id=frame.id,
                alpha_orbital_count=1,
                beta_orbital_count=0,
                coefficient_count=1,
                alpha_occupancies=[2.0],
                beta_occupancies=[],
                alpha_symmetries=["A"],
                beta_symmetries=[],
                source_schema_version="read-query-test-v1",
            )
            population_result = ChargeSpinPopulationResult(
                id=uuid4(),
                frame_id=frame.id,
                series_count=1,
                source_schema_version="read-query-test-v1",
            )
            state_set = ElectronicStateSet(
                id=uuid4(),
                frame_id=frame.id,
                kind=ElectronicStateSetKind.FRAME,
                state_count=1,
                source_schema_version="read-query-test-v1",
            )
            session.add_all([orbital_result, population_result, state_set])
            session.flush()
            assert orbital_result.id is not None
            assert population_result.id is not None
            assert state_set.id is not None
            population_series = AtomicPopulationSeries(
                id=uuid4(),
                result_id=population_result.id,
                series_key="mulliken-charge",
                scheme="mulliken",
                quantity="charge",
                value_count=2,
                series_metadata={},
            )
            state = ElectronicState(
                id=uuid4(),
                state_set_id=state_set.id,
                state_ordinal=0,
                state_index=0,
                label="S0",
                multiplicity=1,
                energy_hartree=-1.0,
            )
            session.add_all([population_series, state])
            session.flush()
            assert population_series.id is not None

            orbital_values = np.array([-0.5], dtype=np.float64)
            population_values = np.array([0.0, 0.0], dtype=np.float64)
            orbital_array = ScientificArray(
                id=uuid4(),
                frame_id=frame.id,
                kind=ScientificArrayKind.ORBITAL_ALPHA_ENERGIES,
                ordinal=0,
                unit="hartree",
                dtype=str(orbital_values.dtype),
                shape=list(orbital_values.shape),
                array_nbytes=orbital_values.nbytes,
                payload_sha256=_fixture_hash(f"read-query-orbital:{uuid4().hex}"),
                data=orbital_values,
            )
            population_array = ScientificArray(
                id=uuid4(),
                frame_id=frame.id,
                kind=ScientificArrayKind.ATOMIC_POPULATION,
                ordinal=0,
                unit="dimensionless",
                dtype=str(population_values.dtype),
                shape=list(population_values.shape),
                array_nbytes=population_values.nbytes,
                payload_sha256=_fixture_hash(f"read-query-population:{uuid4().hex}"),
                data=population_values,
            )
            session.add_all([orbital_array, population_array])
            session.flush()
            assert orbital_array.id is not None and population_array.id is not None
            session.add_all(
                [
                    ScientificArrayAssignment(
                        scientific_array_id=orbital_array.id,
                        slot="alpha_energies",
                        molecular_orbital_result_id=orbital_result.id,
                    ),
                    ScientificArrayAssignment(
                        scientific_array_id=population_array.id,
                        slot="values",
                        atomic_population_series_id=population_series.id,
                    ),
                ]
            )
            session.commit()

        orbital_page = asyncio.run(
            CalculationResultQueryService.list_calculation_results(
                result_kind="molecular_orbitals",
                frame_id=orbital_frame_id,
                limit=10,
                offset=0,
            )
        )
        orbital_detail = asyncio.run(
            CalculationResultQueryService.get_calculation_results(frame_id=orbital_frame_id)
        )
        state_detail = asyncio.run(
            CalculationResultQueryService.get_calculation_results(frame_id=state_frame_id)
        )
        derivation_detail = asyncio.run(
            MolecularTopologyDerivationQueryService.get_topology_derivation(
                derivation_id=derivation_id
            )
        )
        frame_detail = asyncio.run(
            CalculationQueryService.get_calculation_frame(frame_id=orbital_frame_id)
        )

        assert orbital_page.page.total == 1
        assert "molecular_orbitals" in orbital_page.items[0].result_kinds
        assert orbital_detail is not None and orbital_detail.molecular_orbitals is not None
        assert orbital_detail.molecular_orbitals.alpha_orbital_count > 0
        assert orbital_detail.molecular_orbitals.scientific_arrays
        assert orbital_detail.charge_spin_populations is not None
        assert orbital_detail.charge_spin_populations.series
        assert (
            json.loads(orbital_detail.charge_spin_populations.series[0].series_metadata_json) == {}
        )
        assert state_detail is not None and state_detail.electronic_state_sets
        assert state_detail.electronic_state_sets[0].states
        assert derivation_detail is not None
        assert json.loads(derivation_detail.reconstruction_metadata_json)
        assert frame_detail is not None
        assert derivation_detail.provenance_hash == frame_detail.topology_derivation.provenance_hash
    finally:
        if sample is not None:
            with Session(engine) as session:
                _delete_domain_sample(session, sample)
        engine.dispose()
        asyncio.run(dispose_engine())
