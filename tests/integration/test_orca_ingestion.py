import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, select

from tricycle_reaction_db.application.dtos import ArtifactFileRecord
from tricycle_reaction_db.application.services.molop_artifact_ingestion import (
    persist_molop_calculation_artifact,
)
from tricycle_reaction_db.application.services.transition_state_uploads import (
    _parse_calculation_output,
    _persist_uploaded_artifact,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    CalculationFrame,
    CalculationProtocol,
)
from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    ArtifactVisibility,
    FrameRole,
    GeometryAssignmentKind,
    QMSoftware,
    StorageStatus,
)
from tricycle_reaction_db.domain.identity import DEVELOPMENT_USER_ID, SYSTEM_PROJECT_ID
from tricycle_reaction_db.ingestion import artifact_record_from_path

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]

ORCA_FIXTURE = Path(__file__).parents[1] / "fixtures/qm/minimal_orca_water_sp.orcaout"
GAUSSIAN_FIXTURE = Path(__file__).parents[1] / "fixtures/qm/minimal_gaussian_water_sp.log"


def test_molop_content_probe_maps_minimal_gaussian_single_point() -> None:
    parsed = _parse_calculation_output(GAUSSIAN_FIXTURE.read_bytes(), "unstructured-upload.bin")
    record = parsed.frame_records[0]
    segment = parsed.chem_file.source_segments[0]

    assert parsed.source_format == "g16log"
    assert parsed.source_frame_count == len(parsed.frame_records) == 1
    assert parsed.chem_file.source_complete is True
    assert parsed.chem_file.status.normal_terminated is True
    assert segment.qm_software == "Gaussian"
    assert segment.qm_software_version == "ES64L-G16RevA.03"
    assert segment.task_types == ["sp"]
    assert record.frame.frame_role is FrameRole.SINGLE_POINT
    assert record.molecule.formula.hill_formula == "H2O"
    assert record.energy is not None
    assert record.energy.reference_energy_hartree == -74.965901


def test_molop_content_probe_maps_minimal_orca_single_point() -> None:
    parsed = _parse_calculation_output(ORCA_FIXTURE.read_bytes(), "unstructured-upload.bin")
    record = parsed.frame_records[0]
    segment = parsed.chem_file.source_segments[0]

    assert parsed.source_format == "orcaout"
    assert parsed.source_frame_count == len(parsed.frame_records) == 1
    assert parsed.chem_file.source_complete is True
    assert parsed.chem_file.status.normal_terminated is True
    assert segment.qm_software == "ORCA"
    assert segment.qm_software_version == "6.1.1"
    assert segment.task_types == ["sp"]
    assert record.frame.frame_role is FrameRole.SINGLE_POINT
    assert record.molecule.formula.hill_formula == "H2O"
    assert record.energy is not None
    assert record.energy.electronic_energy_hartree == -74.965901


def test_minimal_orca_single_point_persists_protocol_geometry_and_frame() -> None:
    parsed = _parse_calculation_output(ORCA_FIXTURE.read_bytes(), "unstructured-upload.bin")
    record = artifact_record_from_path(
        ORCA_FIXTURE,
        bucket="integration-test",
        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
        storage_status=StorageStatus.AVAILABLE,
    ).model_copy(
        update={
            "project_id": SYSTEM_PROJECT_ID,
            "created_by_user_id": DEVELOPMENT_USER_ID,
            "visibility": ArtifactVisibility.PROJECT,
        }
    )
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            artifact = _persist_uploaded_artifact(
                session,
                record=ArtifactFileRecord.model_validate(record),
            )
            persisted = persist_molop_calculation_artifact(
                session,
                artifact=artifact,
                chem_file=parsed.chem_file,
                records=list(parsed.frame_records),
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
            session.flush()
            frame = session.get(CalculationFrame, persisted.frames_by_file_index[0].id)
            assert frame is not None
            protocol = session.exec(
                select(CalculationProtocol).where(
                    CalculationProtocol.id == frame.segment.protocol_id
                )
            ).one()

            assert persisted.parse_revision.source_format.value == "orca_output"
            assert persisted.frame_count == 1
            assert frame.frame_role is FrameRole.SINGLE_POINT
            assert frame.geometry_id is not None
            assert frame.selected_energy_hartree == -74.965901
            assert protocol.qm_software is QMSoftware.ORCA
            assert protocol.qm_software_version == "6.1.1"
            assert protocol.method == "HF"
            assert protocol.basis_set == "STO-3G"
            assert protocol.task_requests == ["sp"]
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_gaussian_and_orca_same_single_point_geometry_reuses_one_coordinate() -> None:
    parsed_by_path = {
        GAUSSIAN_FIXTURE: _parse_calculation_output(
            GAUSSIAN_FIXTURE.read_bytes(),
            "unstructured-gaussian.bin",
        ),
        ORCA_FIXTURE: _parse_calculation_output(
            ORCA_FIXTURE.read_bytes(),
            "unstructured-orca.bin",
        ),
    }
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            frames: list[CalculationFrame] = []
            for path, parsed in parsed_by_path.items():
                artifact_record = artifact_record_from_path(
                    path,
                    bucket="integration-test",
                    artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                    storage_status=StorageStatus.AVAILABLE,
                ).model_copy(
                    update={
                        "project_id": SYSTEM_PROJECT_ID,
                        "created_by_user_id": DEVELOPMENT_USER_ID,
                        "visibility": ArtifactVisibility.PROJECT,
                    }
                )
                artifact = _persist_uploaded_artifact(
                    session,
                    record=ArtifactFileRecord.model_validate(artifact_record),
                )
                persisted = persist_molop_calculation_artifact(
                    session,
                    artifact=artifact,
                    chem_file=parsed.chem_file,
                    records=list(parsed.frame_records),
                    started_at=datetime.now(UTC),
                    completed_at=datetime.now(UTC),
                )
                frame = session.get(
                    CalculationFrame,
                    persisted.frames_by_file_index[0].id,
                )
                assert frame is not None
                frames.append(frame)

            gaussian_frame, orca_frame = frames
            assert gaussian_frame.id != orca_frame.id
            assert gaussian_frame.geometry_id == orca_frame.geometry_id
            assert {
                gaussian_frame.geometry_assignment_kind,
                orca_frame.geometry_assignment_kind,
            }.issubset(
                {
                    GeometryAssignmentKind.PARSED_EXACT,
                    GeometryAssignmentKind.MATCHED_EXISTING_GEOMETRY,
                }
            )
            protocols = [gaussian_frame.segment.protocol, orca_frame.segment.protocol]
            assert all(protocol is not None for protocol in protocols)
            assert {protocol.qm_software for protocol in protocols if protocol is not None} == {
                QMSoftware.GAUSSIAN,
                QMSoftware.ORCA,
            }
            assert {frame.id for frame in gaussian_frame.geometry.calculation_frames}.issuperset(
                {gaussian_frame.id, orca_frame.id}
            )

    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()
