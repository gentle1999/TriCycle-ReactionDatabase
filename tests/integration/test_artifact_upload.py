import gzip
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import numpy as np
import pytest
from molop.unit import atom_ureg
from sqlalchemy import create_engine, func
from sqlalchemy.orm import undefer
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.dtos import ArtifactFileRecord, CreateReactionCommand
from tricycle_reaction_db.application.services.artifact_uploads import (
    _create_pending_ingestion,
    _FailedInference,
    _parse_calculation_output,
    _persist_parsed_artifact,
    _persist_uploaded_artifact,
)
from tricycle_reaction_db.application.services.reaction_commands import _create_reaction
from tricycle_reaction_db.application.services.reaction_geometry_reconciliation import (
    bind_transition_state_frame,
)
from tricycle_reaction_db.application.services.reactions import (
    atom_maps_from_source_order,
    mapped_smiles_for_topology,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    CalculationFrame,
    LogicalReaction,
    MappedReaction,
    MappedReactionEdge,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    ParseRevision,
    TransitionStateEndpoint,
    TransitionStateInference,
)
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactKind,
    ArtifactVisibility,
    FrameRole,
    GeometryAssignmentKind,
    MappedReactionKind,
    MappedReactionNodeRole,
    OptimizationStatus,
    StorageStatus,
    TransitionStateEndpointDirection,
    TransitionStateInferenceStatus,
)
from tricycle_reaction_db.domain.identity import DEVELOPMENT_USER_ID, SYSTEM_PROJECT_ID

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]

FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures/da_bench_minimal/complete_set/000000000000_000000403256/00/ts/"
    "000000000000_000000403256_00_conf_01_ts.43b3faa8fcc9.log.gz"
)


def test_gzip_upload_tracks_artifact_and_decoded_source_identities_separately() -> None:
    payload = FIXTURE.read_bytes()
    decoded = gzip.decompress(payload)
    digest = sha256(payload).hexdigest()
    parsed = _parse_calculation_output(payload, FIXTURE.name)
    now = datetime.now(UTC)
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            artifact = _persist_uploaded_artifact(
                session,
                record=ArtifactFileRecord(
                    project_id=SYSTEM_PROJECT_ID,
                    created_by_user_id=DEVELOPMENT_USER_ID,
                    visibility=ArtifactVisibility.PROJECT,
                    bucket="integration-test",
                    object_key=f"uploads/gzip/{digest[:2]}/{digest}",
                    content_sha256=digest,
                    size_bytes=len(payload),
                    original_filename=FIXTURE.name,
                    media_type="application/gzip",
                    artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                    storage_status=StorageStatus.AVAILABLE,
                    storage_verified_at=now,
                ),
            )
            ingestion, _ = _create_pending_ingestion(session, artifact=artifact, started_at=now)
            assert ingestion.id is not None
            first_revision_id, first_revision_created = _persist_parsed_artifact(
                session,
                ingestion_id=ingestion.id,
                parsed=parsed,
                started_at=now,
                completed_at=now,
            )
            revision = session.exec(
                select(ParseRevision)
                .where(ParseRevision.artifact_file_id == artifact.id)
                .order_by(col(ParseRevision.created_at).desc())
            ).first()

            assert revision is not None
            assert first_revision_id == revision.id
            assert first_revision_created is True
            assert artifact.content_sha256 == digest
            assert artifact.size_bytes == len(payload)
            assert revision.source_content_sha256 == sha256(decoded).hexdigest()
            assert revision.source_size_bytes == len(decoded)
            assert revision.source_compression == "gzip"
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_calculation_upload_persists_every_frame_and_reuses_ts_reaction() -> None:
    payload = gzip.decompress(FIXTURE.read_bytes()) + b"\n"
    digest = sha256(payload).hexdigest()
    parsed = _parse_calculation_output(payload, FIXTURE.name.removesuffix(".gz"))
    now = datetime.now(UTC)
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            artifact = _persist_uploaded_artifact(
                session,
                record=ArtifactFileRecord(
                    project_id=SYSTEM_PROJECT_ID,
                    created_by_user_id=DEVELOPMENT_USER_ID,
                    visibility=ArtifactVisibility.PROJECT,
                    bucket="integration-test",
                    object_key=f"uploads/sha256/{digest[:2]}/{digest}",
                    content_sha256=digest,
                    size_bytes=len(payload),
                    original_filename=FIXTURE.name.removesuffix(".gz"),
                    media_type="text/plain",
                    artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                    storage_status=StorageStatus.AVAILABLE,
                    storage_verified_at=now,
                ),
            )
            ingestion, _ = _create_pending_ingestion(
                session,
                artifact=artifact,
                started_at=now,
            )
            assert ingestion.id is not None
            _persist_parsed_artifact(
                session,
                ingestion_id=ingestion.id,
                parsed=parsed,
                started_at=now,
                completed_at=now,
            )
            session.flush()

            revision = session.exec(
                select(ParseRevision)
                .where(ParseRevision.artifact_file_id == artifact.id)
                .order_by(col(ParseRevision.created_at).desc())
            ).first()
            assert revision is not None
            frame_count = session.exec(
                select(func.count())
                .select_from(CalculationFrame)
                .where(CalculationFrame.parse_revision_id == revision.id)
            ).one()
            inference = session.exec(
                select(TransitionStateInference).where(
                    TransitionStateInference.parse_revision_id == revision.id
                )
            ).one()
            assert frame_count == parsed.source_frame_count == 23
            assert ingestion.status is ArtifactIngestionStatus.SUCCEEDED
            assert ingestion.transition_state_frame_count == 1
            assert inference.status is TransitionStateInferenceStatus.SUCCEEDED
            assert inference.parse_revision_id == revision.id
            assert inference.file_frame_index == 22
            assert (
                inference.inference_settings["endpoint_selection"] == "molop.possible_pre_post_ts"
            )
            assert inference.inference_settings["sampling_min_ratio"] == 0.75
            assert inference.inference_settings["sampling_max_ratio"] == 1.75
            assert inference.inference_settings["sampling_steps"] == 7
            assert inference.logical_reaction_id is not None
            assert inference.mapped_reaction_id is not None
            ts_frame = session.exec(
                select(CalculationFrame)
                .where(
                    CalculationFrame.parse_revision_id == revision.id,
                    CalculationFrame.file_frame_index == 22,
                )
                .options(undefer(CalculationFrame.observed_coordinates))
            ).one()
            assert inference.calculation_frame_id == ts_frame.id
            assert ts_frame.geometry_assignment_kind in {
                GeometryAssignmentKind.PARSED_EXACT,
                GeometryAssignmentKind.MATCHED_EXISTING_GEOMETRY,
            }
            ts_node = session.exec(
                select(MappedReactionNode).where(
                    MappedReactionNode.mapped_reaction_id == inference.mapped_reaction_id,
                    MappedReactionNode.role == MappedReactionNodeRole.TRANSITION_STATE,
                )
            ).one()
            ts_geometries = session.exec(
                select(MappedReactionNodeGeometry).where(
                    MappedReactionNodeGeometry.mapped_reaction_node_id == ts_node.id
                )
            ).all()
            inferred_geometry = next(
                binding for binding in ts_geometries if binding.geometry_id == ts_frame.geometry_id
            )
            assert len(inferred_geometry.mapping_bindings) == 1
            mapping = inferred_geometry.mapping_bindings[0]
            expected_map_set = list(range(1, ts_frame.geometry.atom_count + 1))
            frame_source_to_geometry = list(ts_frame.observed_to_geometry_atom_indices)
            parsed_ts_frame = next(
                record
                for record in parsed.frame_records
                if record.frame.file_frame_index == ts_frame.file_frame_index
            )
            assert sorted(frame_source_to_geometry) == list(range(ts_frame.geometry.atom_count))
            assert [
                ts_frame.geometry.mol.GetAtomWithIdx(geometry_index).GetAtomicNum()
                for geometry_index in frame_source_to_geometry
            ] == parsed_ts_frame.molecule.observed_atomic_numbers
            assert mapping.geometry_atom_map_numbers == atom_maps_from_source_order(
                ts_frame.geometry,
                expected_map_set,
                frame_source_to_geometry,
            )
            assert mapping.mapped_smiles == mapped_smiles_for_topology(
                ts_frame.geometry.topology,
                mapping.geometry_atom_map_numbers,
            )
            visible_frames = session.exec(
                select(CalculationFrame).where(
                    CalculationFrame.geometry_id == inferred_geometry.geometry_id
                )
            ).all()
            assert {frame.id for frame in visible_frames} >= {ts_frame.id}
            assert ts_frame.frame_role is FrameRole.TERMINAL
            assert ts_frame.optimization_status is OptimizationStatus.CONVERGED
            assert not np.allclose(
                np.asarray(ts_frame.observed_to_geometry_transform, dtype=np.float64).reshape(4, 4),
                np.eye(4),
                rtol=0,
                atol=1e-6,
            )
            endpoints = session.exec(
                select(TransitionStateEndpoint)
                .where(TransitionStateEndpoint.calculation_frame_id == ts_frame.id)
                .options(undefer(TransitionStateEndpoint.source_coordinates))
            ).all()
            assert {endpoint.direction for endpoint in endpoints} == {
                TransitionStateEndpointDirection.NEGATIVE,
                TransitionStateEndpointDirection.POSITIVE,
            }
            for endpoint in endpoints:
                endpoint_mapping = list(endpoint.source_to_topology_atom_indices)
                assert sorted(endpoint_mapping) == list(range(endpoint.atom_count))
                assert [
                    endpoint.topology.mol.GetAtomWithIdx(topology_index).GetAtomicNum()
                    for topology_index in endpoint_mapping
                ] == parsed_ts_frame.molecule.observed_atomic_numbers
            endpoint_by_direction = {endpoint.direction: endpoint for endpoint in endpoints}
            mapped_reaction = session.get(MappedReaction, inference.mapped_reaction_id)
            assert mapped_reaction is not None
            assert mapped_reaction.mapped_reaction_smiles == parsed.inferences[0].reaction_smiles
            negative_coordinates = np.asarray(
                endpoint_by_direction[TransitionStateEndpointDirection.NEGATIVE].source_coordinates,
                dtype=np.float64,
            )
            positive_coordinates = np.asarray(
                endpoint_by_direction[TransitionStateEndpointDirection.POSITIVE].source_coordinates,
                dtype=np.float64,
            )
            center_coordinates = np.asarray(ts_frame.observed_coordinates, dtype=np.float64)
            assert (
                negative_coordinates.shape == positive_coordinates.shape == center_coordinates.shape
            )
            assert np.linalg.norm(negative_coordinates - center_coordinates) > 0
            assert np.linalg.norm(positive_coordinates - center_coordinates) > 0
            # MolOP selects each signed side independently across amplitudes, so
            # the two endpoints are not required to bracket the center
            # symmetrically.  Only their direction along the imaginary mode and
            # the persisted displacement ratios are asserted below.
            source_ts_frame = next(
                frame
                for frame in parsed.chem_file
                if frame.file_frame_index == inference.file_frame_index
            )
            assert source_ts_frame.vibrations is not None
            imaginary_position = source_ts_frame.vibrations.imaginary_idxs[0]
            imaginary_mode = np.asarray(
                source_ts_frame.vibrations[imaginary_position]
                .vibration_mode.to(atom_ureg.angstrom)
                .magnitude,
                dtype=np.float64,
            )
            mode_norm = float(np.sum(np.square(imaginary_mode)))
            assert mode_norm > 0
            signed_displacements = []
            for endpoint_coordinates in (negative_coordinates, positive_coordinates):
                displacement = endpoint_coordinates - center_coordinates
                signed_scale = float(np.sum(displacement * imaginary_mode) / mode_norm)
                np.testing.assert_allclose(
                    displacement,
                    signed_scale * imaginary_mode,
                    rtol=0,
                    atol=2e-5,
                )
                signed_displacements.append(signed_scale)
            assert signed_displacements[0] * signed_displacements[1] < 0
            # Persisted ratios are the measured signed displacements of the
            # MolOP-selected endpoints, not a fixed inference amplitude.
            assert endpoint_by_direction[
                TransitionStateEndpointDirection.NEGATIVE
            ].displacement_ratio == pytest.approx(abs(signed_displacements[0]))
            assert endpoint_by_direction[
                TransitionStateEndpointDirection.POSITIVE
            ].displacement_ratio == pytest.approx(abs(signed_displacements[1]))
            assert (
                session.exec(
                    select(MappedReactionEdge).where(
                        MappedReactionEdge.mapped_reaction_id == inference.mapped_reaction_id,
                        MappedReactionEdge.transition_state_node_id == ts_node.id,
                    )
                ).first()
                is not None
            )

            alternate_frame = session.exec(
                select(CalculationFrame).where(
                    CalculationFrame.parse_revision_id == revision.id,
                    CalculationFrame.file_frame_index == 19,
                )
            ).one()
            assert alternate_frame.geometry_id != ts_frame.geometry_id
            original_alternate_frame_role = alternate_frame.frame_role
            original_alternate_optimization_status = alternate_frame.optimization_status
            mapped_reaction = session.get(MappedReaction, inference.mapped_reaction_id)
            assert mapped_reaction is not None
            with pytest.raises(
                ValueError,
                match="single-point or terminal frame",
            ):
                bind_transition_state_frame(
                    session,
                    mapped_reaction=mapped_reaction,
                    calculation_frame=alternate_frame,
                )
            assert session.exec(
                select(func.count())
                .select_from(MappedReactionNodeGeometry)
                .where(MappedReactionNodeGeometry.mapped_reaction_node_id == ts_node.id)
            ).one() == len(ts_geometries)

            alternate_frame.optimization_status = OptimizationStatus.CONVERGED
            alternate_frame.frame_role = FrameRole.TERMINAL
            session.flush()
            with pytest.raises(
                ValueError,
                match="requires at least one thermodynamic property",
            ):
                bind_transition_state_frame(
                    session,
                    mapped_reaction=mapped_reaction,
                    calculation_frame=alternate_frame,
                )
            assert session.exec(
                select(func.count())
                .select_from(MappedReactionNodeGeometry)
                .where(MappedReactionNodeGeometry.mapped_reaction_node_id == ts_node.id)
            ).one() == len(ts_geometries)
            assert (
                session.exec(
                    select(func.count())
                    .select_from(MappedReactionNode)
                    .where(
                        MappedReactionNode.mapped_reaction_id == inference.mapped_reaction_id,
                        MappedReactionNode.role == MappedReactionNodeRole.TRANSITION_STATE,
                    )
                ).one()
                == 1
            )
            assert (
                session.exec(
                    select(func.count())
                    .select_from(MappedReactionEdge)
                    .where(
                        MappedReactionEdge.mapped_reaction_id == inference.mapped_reaction_id,
                        MappedReactionEdge.transition_state_node_id == ts_node.id,
                    )
                ).one()
                == 1
            )

            alternate_frame.optimization_status = original_alternate_optimization_status
            alternate_frame.frame_role = original_alternate_frame_role
            session.flush()

            inferred = parsed.inferences[0]
            assert hasattr(inferred, "reaction_smiles")
            curated = _create_reaction(
                session,
                CreateReactionCommand(
                    reaction=inferred.reaction_smiles,
                    mapped_reaction_kind=MappedReactionKind.CURATED,
                ),
            )
            assert curated.mapped_reaction_id == inference.mapped_reaction_id

            reaction_count = session.exec(select(func.count()).select_from(LogicalReaction)).one()
            second_revision_id, second_revision_created = _persist_parsed_artifact(
                session,
                ingestion_id=ingestion.id,
                parsed=parsed,
                started_at=now + timedelta(seconds=1),
                completed_at=now + timedelta(seconds=2),
            )
            session.flush()
            assert second_revision_id == revision.id
            assert second_revision_created is False
            assert (
                session.exec(select(func.count()).select_from(LogicalReaction)).one()
                == reaction_count
            )
            assert (
                session.exec(
                    select(func.count())
                    .select_from(TransitionStateInference)
                    .where(TransitionStateInference.parse_revision_id == revision.id)
                ).one()
                == 1
            )
            assert (
                session.exec(
                    select(func.count())
                    .select_from(CalculationFrame)
                    .where(CalculationFrame.parse_revision_id == revision.id)
                ).one()
                == frame_count
            )
            third_revision_id, third_revision_created = _persist_parsed_artifact(
                session,
                ingestion_id=ingestion.id,
                parsed=parsed,
                started_at=now + timedelta(seconds=3),
                completed_at=now + timedelta(seconds=4),
                force_new_revision=True,
            )
            session.flush()
            assert third_revision_created is True
            assert third_revision_id != revision.id
            reparse_revision = session.get(ParseRevision, third_revision_id)
            assert reparse_revision is not None
            assert reparse_revision.revision_number == revision.revision_number + 1
            assert reparse_revision.reparse_of_id == revision.id
            assert (
                session.exec(
                    select(func.count())
                    .select_from(CalculationFrame)
                    .where(CalculationFrame.parse_revision_id == third_revision_id)
                ).one()
                == frame_count
            )
            reparse_inference = session.exec(
                select(TransitionStateInference).where(
                    TransitionStateInference.parse_revision_id == third_revision_id
                )
            ).one()
            assert reparse_inference.calculation_frame_id != inference.calculation_frame_id
            assert reparse_inference.mapped_reaction_id == inference.mapped_reaction_id
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_nonconverged_ts_binds_geometry_and_converged_reparse_adds_evidence() -> None:
    payload = gzip.decompress(FIXTURE.read_bytes()) + b"\n\n"
    digest = sha256(payload).hexdigest()
    parsed = _parse_calculation_output(payload, FIXTURE.name.removesuffix(".gz"))
    ts_frame_indices = {item.file_frame_index for item in parsed.inferences}
    nonconverged_records = tuple(
        replace(
            record,
            frame=record.frame.model_copy(
                update={"optimization_status": OptimizationStatus.NOT_CONVERGED}
            ),
        )
        if index in ts_frame_indices
        else record
        for index, record in enumerate(parsed.frame_records)
    )
    nonconverged = replace(parsed, frame_records=nonconverged_records)
    now = datetime.now(UTC)
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            artifact = _persist_uploaded_artifact(
                session,
                record=ArtifactFileRecord(
                    project_id=SYSTEM_PROJECT_ID,
                    created_by_user_id=DEVELOPMENT_USER_ID,
                    visibility=ArtifactVisibility.PROJECT,
                    bucket="integration-test",
                    object_key=f"uploads/nonconverged/{digest[:2]}/{digest}",
                    content_sha256=digest,
                    size_bytes=len(payload),
                    original_filename=FIXTURE.name.removesuffix(".gz"),
                    media_type="text/plain",
                    artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                    storage_status=StorageStatus.AVAILABLE,
                    storage_verified_at=now,
                ),
            )
            ingestion, _ = _create_pending_ingestion(session, artifact=artifact, started_at=now)
            assert ingestion.id is not None
            first_revision_id, _ = _persist_parsed_artifact(
                session,
                ingestion_id=ingestion.id,
                parsed=nonconverged,
                started_at=now,
                completed_at=now,
            )
            session.flush()

            first_inference = session.exec(
                select(TransitionStateInference).where(
                    TransitionStateInference.parse_revision_id == first_revision_id
                )
            ).one()
            assert first_inference.status is TransitionStateInferenceStatus.SUCCEEDED
            assert first_inference.mapped_reaction_id is not None
            first_frame = session.get(CalculationFrame, first_inference.calculation_frame_id)
            assert first_frame is not None
            assert first_frame.optimization_status is OptimizationStatus.NOT_CONVERGED
            ts_node = session.exec(
                select(MappedReactionNode).where(
                    MappedReactionNode.mapped_reaction_id == first_inference.mapped_reaction_id,
                    MappedReactionNode.role == MappedReactionNodeRole.TRANSITION_STATE,
                )
            ).one()
            assert (
                session.exec(
                    select(func.count())
                    .select_from(MappedReactionEdge)
                    .where(MappedReactionEdge.transition_state_node_id == ts_node.id)
                ).one()
                == 1
            )
            first_geometry_bindings = session.exec(
                select(MappedReactionNodeGeometry).where(
                    MappedReactionNodeGeometry.geometry_id == first_frame.geometry_id
                )
            ).all()
            assert first_geometry_bindings

            mapped_reaction = session.get(MappedReaction, first_inference.mapped_reaction_id)
            assert mapped_reaction is not None
            duplicate_first_binding = bind_transition_state_frame(
                session,
                mapped_reaction=mapped_reaction,
                calculation_frame=first_frame,
            )
            assert duplicate_first_binding.geometry_id == first_frame.geometry_id

            second_revision_id, created = _persist_parsed_artifact(
                session,
                ingestion_id=ingestion.id,
                parsed=parsed,
                started_at=now + timedelta(seconds=1),
                completed_at=now + timedelta(seconds=2),
                force_new_revision=True,
            )
            session.flush()
            assert created is True
            second_inference = session.exec(
                select(TransitionStateInference).where(
                    TransitionStateInference.parse_revision_id == second_revision_id
                )
            ).one()
            assert second_inference.mapped_reaction_id == first_inference.mapped_reaction_id
            second_frame = session.get(CalculationFrame, second_inference.calculation_frame_id)
            assert second_frame is not None
            assert second_frame.optimization_status is OptimizationStatus.CONVERGED
            assert session.exec(
                select(MappedReactionNodeGeometry).where(
                    MappedReactionNodeGeometry.mapped_reaction_node_id == ts_node.id,
                    MappedReactionNodeGeometry.geometry_id == second_frame.geometry_id,
                )
            ).all()
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_successful_parse_with_failed_ts_inference_is_partial() -> None:
    payload = gzip.decompress(FIXTURE.read_bytes()) + b"\n\n\n"
    digest = sha256(payload).hexdigest()
    parsed = _parse_calculation_output(payload, FIXTURE.name.removesuffix(".gz"))
    inferred = parsed.inferences[0]
    failed = replace(
        parsed,
        inferences=(
            _FailedInference(
                file_frame_index=inferred.file_frame_index,
                imaginary_mode_index=inferred.imaginary_mode_index,
                imaginary_frequency_cm1=inferred.imaginary_frequency_cm1,
                error_code="ts_endpoint_inference_failed",
                error_message="fixture endpoint inference failure",
            ),
        ),
    )
    now = datetime.now(UTC)
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            artifact = _persist_uploaded_artifact(
                session,
                record=ArtifactFileRecord(
                    project_id=SYSTEM_PROJECT_ID,
                    created_by_user_id=DEVELOPMENT_USER_ID,
                    visibility=ArtifactVisibility.PROJECT,
                    bucket="integration-test",
                    object_key=f"uploads/partial/{digest[:2]}/{digest}",
                    content_sha256=digest,
                    size_bytes=len(payload),
                    original_filename=FIXTURE.name.removesuffix(".gz"),
                    media_type="text/plain",
                    artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                    storage_status=StorageStatus.AVAILABLE,
                    storage_verified_at=now,
                ),
            )
            ingestion, _ = _create_pending_ingestion(session, artifact=artifact, started_at=now)
            assert ingestion.id is not None
            revision_id, _ = _persist_parsed_artifact(
                session,
                ingestion_id=ingestion.id,
                parsed=failed,
                started_at=now,
                completed_at=now,
            )
            session.flush()

            revision = session.get(ParseRevision, revision_id)
            assert revision is not None
            assert revision.status.value == "succeeded"
            assert ingestion.status is ArtifactIngestionStatus.PARTIAL
            inference = session.exec(
                select(TransitionStateInference).where(
                    TransitionStateInference.parse_revision_id == revision_id
                )
            ).one()
            assert inference.status is TransitionStateInferenceStatus.FAILED
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()
