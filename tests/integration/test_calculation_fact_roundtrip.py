import os
from datetime import UTC, datetime
from hashlib import sha256
from queue import Queue
from secrets import token_hex
from threading import Thread
from time import monotonic, sleep
from uuid import UUID

import numpy as np
import pytest
from rdkit import Chem
from sqlalchemy import create_engine, insert, inspect, text
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.orm import undefer
from sqlalchemy.sql.dml import Insert
from sqlmodel import Session, select

from tricycle_reaction_db.application.dtos import (
    AtomicPopulationSeriesRecord,
    CalculationFrameRecord,
    CalculationSegmentRecord,
    ChargeSpinPopulationResultRecord,
    NormalizedMoleculeRecord,
    ParseRevisionCompletionRecord,
    ParseRevisionRecord,
    ScientificArrayAssignmentRecord,
    ScientificArrayRecord,
    ThermochemistryResultRecord,
)
from tricycle_reaction_db.application.services import (
    finalize_parse_revision,
    persist_artifact_file,
    persist_atomic_population_series,
    persist_calculation_frame,
    persist_calculation_protocol,
    persist_calculation_segment,
    persist_charge_spin_population_result,
    persist_molecular_geometry,
    persist_parse_revision,
    persist_scientific_array,
    persist_scientific_array_assignment,
    persist_thermochemistry_result,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    CalculationFrame,
    CalculationSegment,
    ParseRevision,
    ScientificArray,
)
from tricycle_reaction_db.db.types import summarize_numpy_array
from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    FrameRole,
    GeometryAssignmentKind,
    ParseStatus,
    QMSoftware,
    SCFStatus,
    ScientificArrayKind,
    ScientificArrayOwnerKind,
    SelectedEnergyKind,
    SourceFormat,
    StorageStatus,
    TerminationStatus,
)
from tricycle_reaction_db.ingestion import (
    artifact_record_from_path,
    calculation_protocol_record,
    normalize_molecule,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


def _exact_geometry_assignment(molecule: NormalizedMoleculeRecord) -> dict[str, object]:
    return {
        "geometry_assignment_kind": GeometryAssignmentKind.PARSED_EXACT,
        "observed_coordinates": molecule.observed_coordinates,
        "observed_coordinate_hash": molecule.observed_coordinate_hash,
        "observed_to_geometry_atom_indices": molecule.observed_to_geometry_atom_indices,
        "observed_to_geometry_transform": molecule.observed_to_geometry_transform,
        "geometry_assignment_rmsd_angstrom": molecule.geometry_assignment_rmsd_angstrom,
        "geometry_assignment_max_abs_angstrom": molecule.geometry_assignment_max_abs_angstrom,
        "geometry_assignment_policy_version": "geometry-internal-coordinate-match-v4",
    }


def _assert_check_rejected(
    session: Session,
    statement: Insert,
    constraint_name: str,
) -> None:
    with pytest.raises(IntegrityError) as caught, session.begin_nested():
        session.execute(statement)
    diagnostic = getattr(caught.value.orig, "diag", None)
    assert getattr(diagnostic, "constraint_name", None) == constraint_name


def test_calculation_facts_round_trip_through_relationships(tmp_path) -> None:
    source_path = tmp_path / "relationship-probe.log"
    source_payload = b"Gaussian relationship probe\nframe data\n"
    source_path.write_bytes(source_payload)
    source_hash = sha256(source_payload).hexdigest()

    base_mol = Chem.MolFromSmiles("C=C")
    assert base_mol is not None
    mol = Chem.AddHs(base_mol)
    coordinates = np.arange(mol.GetNumAtoms() * 3, dtype=np.float64).reshape(-1, 3) / 10
    molecule_record = normalize_molecule(
        mol,
        coordinates,
        charge=0,
        multiplicity=1,
        reconstruction_method="fixture",
        reconstruction_version="v1",
    )

    database_engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = database_engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            artifact = persist_artifact_file(
                session,
                artifact_record_from_path(
                    source_path,
                    bucket="tricycle-test",
                    artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                ),
            )
            protocol = persist_calculation_protocol(
                session,
                calculation_protocol_record(
                    qm_software=QMSoftware.GAUSSIAN,
                    qm_software_version="G16RevA.03",
                    method_family="DFT",
                    functional="B3LYP",
                    basis_set="def2SVP",
                    task_requests=["opt", "freq"],
                    normalized_spec={"fixture": "relationship-probe"},
                ),
            )
            persisted_molecule = persist_molecular_geometry(session, molecule_record)
            geometry = persisted_molecule.geometry
            topology_derivation = persisted_molecule.topology_derivation
            alternate_derivation = persist_molecular_geometry(
                session,
                normalize_molecule(
                    mol,
                    coordinates,
                    charge=0,
                    multiplicity=1,
                    reconstruction_method="fixture",
                    reconstruction_version="v2",
                    reconstruction_metadata={"source": "alternate"},
                ),
            )
            assert alternate_derivation.topology.id == persisted_molecule.topology.id
            assert alternate_derivation.geometry.id == geometry.id
            assert alternate_derivation.topology_derivation.id != topology_derivation.id
            now = datetime.now(UTC)
            revision_record = ParseRevisionRecord(
                export_schema_version="molop-calculation-v1",
                parser_id="fixture.parser",
                parser_version="0.2.1",
                molop_version="0.2.1",
                parser_commit="8d9573169478f795ce828a5cec75fd3a28bbc066",
                rdkit_version="2025.09.6",
                parser_provenance={
                    "parser_id": "fixture.parser",
                    "parser_version": "0.2.1",
                    "molop_version": "0.2.1",
                    "rdkit_version": "2025.09.6",
                    "effective_config": {},
                    "effective_config_sha256": "2" * 64,
                },
                parser_provenance_hash="1" * 64,
                parser_config_hash="2" * 64,
                reconstruction_config_hash="3" * 64,
                source_format=SourceFormat.GAUSSIAN_LOG,
                source_encoding="ascii",
                status=ParseStatus.PENDING,
                started_at=now,
            )
            revision = persist_parse_revision(session, artifact, revision_record)
            completion_record = ParseRevisionCompletionRecord(
                record_sha256="4" * 64,
                completed_at=now,
            )
            with pytest.raises(ValueError, match="at least one CalculationSegment"):
                finalize_parse_revision(session, revision, completion_record)
            segment_record = CalculationSegmentRecord(
                segment_index=0,
                source_start_byte=0,
                source_end_byte=len(source_payload),
                source_start_char=0,
                source_end_char=len(source_payload),
                source_start_line=1,
                source_end_line=3,
                source_block_sha256=source_hash,
                termination_status=TerminationStatus.NORMAL,
                scf_status=SCFStatus.CONVERGED,
            )
            with pytest.raises(ValueError, match="exceeds its parsed source byte size"):
                persist_calculation_segment(
                    session,
                    revision,
                    protocol,
                    segment_record.model_copy(update={"source_end_byte": len(source_payload) + 1}),
                )
            segment = persist_calculation_segment(
                session,
                revision,
                protocol,
                segment_record,
            )
            frame_record = CalculationFrameRecord(
                frame_index=0,
                file_frame_index=0,
                frame_role=FrameRole.TERMINAL,
                source_start_byte=0,
                source_end_byte=len(source_payload),
                source_start_char=0,
                source_end_char=len(source_payload),
                source_start_line=1,
                source_end_line=3,
                source_block_sha256=source_hash,
                charge=0,
                multiplicity=1,
                coordinate_decimal_places=8,
                reference_total_energy_hartree=-78.5,
                frequency_count=1,
                negative_frequency_count=0,
                lowest_frequency_cm1=812.0,
                **_exact_geometry_assignment(molecule_record),
            )
            with pytest.raises(ValueError, match="byte span must be contained"):
                persist_calculation_frame(
                    session,
                    segment,
                    geometry,
                    topology_derivation,
                    frame_record.model_copy(
                        update={
                            "source_start_byte": len(source_payload),
                            "source_end_byte": len(source_payload) + 1,
                        }
                    ),
                )
            frame = persist_calculation_frame(
                session,
                segment,
                geometry,
                topology_derivation,
                frame_record,
            )
            assert frame.observed_coordinates == pytest.approx(molecule_record.observed_coordinates)
            assert frame.observed_coordinate_hash == molecule_record.observed_coordinate_hash

            forces = np.zeros((geometry.atom_count, 3), dtype=np.float64)
            summary = summarize_numpy_array(forces)
            array_record = ScientificArrayRecord(
                kind=ScientificArrayKind.FORCES,
                ordinal=0,
                unit="hartree/bohr",
                dtype=summary.dtype,
                shape=list(summary.shape),
                array_nbytes=summary.nbytes,
                payload_sha256=summary.sha256,
                data=forces,
            )
            bad_forces = np.zeros((geometry.atom_count,), dtype=np.float64)
            bad_summary = summarize_numpy_array(bad_forces)
            with pytest.raises(ValueError, match="forces shape must be"):
                persist_scientific_array(
                    session,
                    frame,
                    array_record.model_copy(
                        update={
                            "data": bad_forces,
                            "shape": list(bad_summary.shape),
                            "array_nbytes": bad_summary.nbytes,
                            "payload_sha256": bad_summary.sha256,
                        }
                    ),
                )
            scientific_array = persist_scientific_array(session, frame, array_record)
            population_result_record = ChargeSpinPopulationResultRecord(
                series_count=1,
                source_schema_version="molop-calculation-v1",
            )
            population_result = persist_charge_spin_population_result(
                session,
                frame,
                population_result_record,
            )
            population_series_record = AtomicPopulationSeriesRecord(
                series_key="mulliken_charges",
                scheme="mulliken",
                quantity="charge",
                value_count=geometry.atom_count,
                source_label="Mulliken charges",
            )
            population_series = persist_atomic_population_series(
                session,
                population_result,
                population_series_record,
            )
            population_values = np.linspace(
                -0.25,
                0.25,
                geometry.atom_count,
                dtype=np.float64,
            )
            population_summary = summarize_numpy_array(population_values)
            population_array_record = ScientificArrayRecord(
                kind=ScientificArrayKind.ATOMIC_POPULATION,
                ordinal=0,
                unit="dimensionless",
                dtype=population_summary.dtype,
                shape=list(population_summary.shape),
                array_nbytes=population_summary.nbytes,
                payload_sha256=population_summary.sha256,
                data=population_values,
            )
            population_array = persist_scientific_array(
                session,
                frame,
                population_array_record,
            )
            assignment_record = ScientificArrayAssignmentRecord(
                array_kind=ScientificArrayKind.ATOMIC_POPULATION,
                array_ordinal=0,
                owner_kind=ScientificArrayOwnerKind.ATOMIC_POPULATION_SERIES,
                owner_key="mulliken_charges",
                slot="values",
            )
            assignment = persist_scientific_array_assignment(
                session,
                population_array,
                population_series,
                assignment_record,
            )
            thermochemistry_record = ThermochemistryResultRecord(
                temperature_kelvin=298.15,
                pressure_atm=1.0,
                thermal_gibbs_correction_hartree=0.0123,
                source_schema_version="molop-calculation-v1",
            )
            thermochemistry = persist_thermochemistry_result(
                session,
                frame,
                thermochemistry_record,
            )
            finalize_parse_revision(session, revision, completion_record)

            assert persist_parse_revision(session, artifact, revision_record).id == revision.id
            assert (
                persist_calculation_segment(session, revision, protocol, segment_record).id
                == segment.id
            )
            assert (
                persist_calculation_frame(
                    session,
                    segment,
                    geometry,
                    topology_derivation,
                    frame_record,
                ).id
                == frame.id
            )
            assert persist_scientific_array(session, frame, array_record).id == scientific_array.id
            assert (
                persist_charge_spin_population_result(session, frame, population_result_record).id
                == population_result.id
            )
            assert (
                persist_atomic_population_series(
                    session, population_result, population_series_record
                ).id
                == population_series.id
            )
            assert (
                persist_scientific_array_assignment(
                    session,
                    population_array,
                    population_series,
                    assignment_record,
                ).id
                == assignment.id
            )
            assert (
                persist_thermochemistry_result(session, frame, thermochemistry_record).id
                == thermochemistry.id
            )
            assert finalize_parse_revision(session, revision, completion_record).id == revision.id
            session.commit()

            assert revision.id is not None and revision.id.version == 7
            assert segment.id is not None and segment.id.version == 7
            assert frame.id is not None and frame.id.version == 7
            assert segment.parse_revision_id == revision.id
            assert frame.segment_id == segment.id
            assert frame.parse_revision_id == revision.id
            assert frame.geometry_id == geometry.id
            assert frame.topology_derivation_id == topology_derivation.id
            assert revision.status is ParseStatus.SUCCEEDED
            artifact_id = artifact.id
            protocol_id = protocol.id
            geometry_id = geometry.id
            revision_id = revision.id
            scientific_array_id = scientific_array.id

        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            loaded_revision = session.exec(
                select(ParseRevision).where(ParseRevision.id == revision_id)
            ).one()
            loaded_segment = loaded_revision.segments[0]
            loaded_frame = loaded_segment.frames[0]
            assert loaded_revision.artifact_file.id == artifact_id
            assert loaded_segment.protocol.id == protocol_id
            assert loaded_frame.geometry.id == geometry_id
            assert loaded_frame.coordinate_decimal_places == 8
            assert loaded_frame.topology_derivation.provenance_hash == (
                molecule_record.topology_derivation.provenance_hash
            )
            assert loaded_frame.thermochemistry_result is not None
            assert loaded_frame.thermochemistry_result.gibbs_free_energy_hartree is None

            unloaded_array = loaded_frame.scientific_arrays[0]
            assert "data" in inspect(unloaded_array).unloaded
            with pytest.raises(InvalidRequestError, match="raiseload"):
                _ = unloaded_array.data

            loaded_array = session.exec(
                select(ScientificArray)
                .where(ScientificArray.id == scientific_array_id)
                .options(undefer(ScientificArray.data))
            ).one()
            np.testing.assert_array_equal(loaded_array.data, forces)
            assert not loaded_array.data.flags.writeable
    finally:
        transaction.rollback()
        connection.close()
        database_engine.dispose()


def test_postgresql_checks_reject_invalid_calculation_facts(tmp_path) -> None:
    source_path = tmp_path / "database-check-probe.log"
    source_payload = b"database constraint probe\n" * 4
    source_path.write_bytes(source_payload)
    source_hash = sha256(source_payload).hexdigest()

    base_mol = Chem.MolFromSmiles("C=C")
    assert base_mol is not None
    mol = Chem.AddHs(base_mol)
    coordinates = np.arange(mol.GetNumAtoms() * 3, dtype=np.float64).reshape(-1, 3) / 10
    molecule_record = normalize_molecule(
        mol,
        coordinates,
        charge=0,
        multiplicity=1,
        reconstruction_method="constraint-probe",
        reconstruction_version="v1",
    )

    database_engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = database_engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            artifact = persist_artifact_file(
                session,
                artifact_record_from_path(
                    source_path,
                    bucket="tricycle-check-test",
                    artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                ),
            )
            protocol = persist_calculation_protocol(
                session,
                calculation_protocol_record(
                    qm_software=QMSoftware.GAUSSIAN,
                    qm_software_version="G16-check-probe",
                    normalized_spec={"fixture": "database-check-probe"},
                    task_requests=["opt"],
                ),
            )
            persisted_molecule = persist_molecular_geometry(session, molecule_record)
            geometry = persisted_molecule.geometry
            topology_derivation = persisted_molecule.topology_derivation
            revision = persist_parse_revision(
                session,
                artifact,
                ParseRevisionRecord(
                    export_schema_version="database-check-v1",
                    parser_id="fixture.parser",
                    parser_version="v1",
                    molop_version="v1",
                    parser_commit="a" * 40,
                    rdkit_version="2025.09.6",
                    parser_provenance={
                        "parser_id": "fixture.parser",
                        "parser_version": "v1",
                        "molop_version": "v1",
                        "rdkit_version": "2025.09.6",
                        "effective_config": {},
                        "effective_config_sha256": "2" * 64,
                    },
                    parser_provenance_hash="1" * 64,
                    parser_config_hash="2" * 64,
                    reconstruction_config_hash="3" * 64,
                    source_format=SourceFormat.GAUSSIAN_LOG,
                    source_encoding="ascii",
                    started_at=datetime.now(UTC),
                ),
            )
            segment = persist_calculation_segment(
                session,
                revision,
                protocol,
                CalculationSegmentRecord(
                    segment_index=0,
                    source_start_byte=0,
                    source_end_byte=len(source_payload),
                    source_start_line=1,
                    source_end_line=5,
                    source_block_sha256=source_hash,
                ),
            )
            frame = persist_calculation_frame(
                session,
                segment,
                geometry,
                topology_derivation,
                CalculationFrameRecord(
                    frame_index=0,
                    file_frame_index=0,
                    frame_role=FrameRole.INITIAL,
                    source_start_byte=0,
                    source_end_byte=len(source_payload),
                    source_start_line=1,
                    source_end_line=5,
                    source_block_sha256=source_hash,
                    charge=0,
                    multiplicity=1,
                    **_exact_geometry_assignment(molecule_record),
                ),
            )

            assert revision.id is not None
            assert protocol.id is not None
            assert segment.id is not None
            assert geometry.id is not None
            assert frame.id is not None

            _assert_check_rejected(
                session,
                insert(CalculationSegment).values(
                    parse_revision_id=revision.id,
                    protocol_id=protocol.id,
                    segment_index=1,
                    source_start_byte=0,
                    source_end_byte=1,
                    source_start_char=0,
                    source_end_char=None,
                    source_start_line=1,
                    source_end_line=2,
                    source_block_sha256="4" * 64,
                    program_metadata={},
                ),
                "ck_calculation_segment_char_span",
            )

            frame_values = {
                "parse_revision_id": revision.id,
                "segment_id": segment.id,
                "frame_role": FrameRole.INTERMEDIATE,
                "source_start_byte": 0,
                "source_end_byte": 1,
                "source_start_line": 1,
                "source_end_line": 2,
                "source_block_sha256": "5" * 64,
                "geometry_id": geometry.id,
                "topology_derivation_id": topology_derivation.id,
                "charge": 0,
                "multiplicity": 1,
                "geometry_assignment_kind": GeometryAssignmentKind.PARSED_EXACT,
                "observed_coordinates": molecule_record.observed_coordinates,
                "observed_coordinate_hash": molecule_record.observed_coordinate_hash,
                "observed_to_geometry_atom_indices": (
                    molecule_record.observed_to_geometry_atom_indices
                ),
                "observed_to_geometry_transform": (molecule_record.observed_to_geometry_transform),
                "geometry_assignment_rmsd_angstrom": (
                    molecule_record.geometry_assignment_rmsd_angstrom
                ),
                "geometry_assignment_max_abs_angstrom": (
                    molecule_record.geometry_assignment_max_abs_angstrom
                ),
                "geometry_assignment_policy_version": ("geometry-internal-coordinate-match-v4"),
                "program_metadata": {},
            }
            _assert_check_rejected(
                session,
                insert(CalculationFrame).values(
                    **frame_values,
                    frame_index=1,
                    file_frame_index=1,
                    source_start_char=0,
                    source_end_char=None,
                ),
                "ck_calculation_frame_char_span",
            )
            _assert_check_rejected(
                session,
                insert(CalculationFrame).values(
                    **frame_values,
                    frame_index=2,
                    file_frame_index=2,
                    reference_total_energy_hartree=-10.0,
                    selected_energy_hartree=-9.0,
                    selected_energy_kind=SelectedEnergyKind.REFERENCE_TOTAL,
                    energy_selection_policy_version="policy-v1",
                ),
                "ck_calculation_frame_selected_energy_matches_source",
            )
            _assert_check_rejected(
                session,
                insert(CalculationFrame).values(
                    **frame_values,
                    frame_index=3,
                    file_frame_index=3,
                    coordinate_decimal_places=19,
                ),
                "ck_calculation_frame_coordinate_decimal_places",
            )
            for index, negative_frequency_count in enumerate((None, 1), start=4):
                _assert_check_rejected(
                    session,
                    insert(CalculationFrame).values(
                        **frame_values,
                        frame_index=index,
                        file_frame_index=index,
                        frequency_count=0,
                        negative_frequency_count=negative_frequency_count,
                        lowest_frequency_cm1=None,
                    ),
                    "ck_calculation_frame_frequency_summary_complete",
                )

            array_data = np.zeros((geometry.atom_count, 3), dtype=np.float64)
            array_summary = summarize_numpy_array(array_data)
            _assert_check_rejected(
                session,
                insert(ScientificArray).values(
                    frame_id=frame.id,
                    kind=ScientificArrayKind.FORCES,
                    ordinal=0,
                    unit="hartree/bohr",
                    dtype=array_summary.dtype,
                    shape=[geometry.atom_count, None],
                    array_nbytes=array_summary.nbytes,
                    payload_sha256=array_summary.sha256,
                    data=array_data,
                ),
                "ck_scientific_array_shape",
            )
    finally:
        transaction.rollback()
        connection.close()
        database_engine.dispose()


def test_concurrent_parse_revision_persistence_reuses_identity_after_advisory_lock() -> None:
    database_engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    digest = token_hex(32)
    artifact_id: UUID | None = None
    first_session: Session | None = None
    worker: Thread | None = None
    backend_pids: Queue[int] = Queue()
    worker_results: Queue[tuple[UUID | None, Exception | None]] = Queue()

    try:
        with Session(database_engine) as setup_session:
            artifact = ArtifactFile(
                bucket="tricycle-concurrency-test",
                object_key=f"raw/sha256/{digest[:2]}/{digest}",
                content_sha256=digest,
                size_bytes=1,
                original_filename="concurrent.log",
                media_type="text/plain",
                artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                storage_status=StorageStatus.PENDING,
            )
            setup_session.add(artifact)
            setup_session.commit()
            setup_session.refresh(artifact)
            artifact_id = artifact.id
        assert artifact_id is not None

        revision_record = ParseRevisionRecord(
            export_schema_version="advisory-lock-v1",
            parser_id="fixture.parser",
            parser_version="v1",
            molop_version="v1",
            parser_commit="b" * 40,
            rdkit_version="2025.09.6",
            parser_provenance={
                "parser_id": "fixture.parser",
                "parser_version": "v1",
                "molop_version": "v1",
                "rdkit_version": "2025.09.6",
                "effective_config": {},
                "effective_config_sha256": "7" * 64,
            },
            parser_provenance_hash="6" * 64,
            parser_config_hash="7" * 64,
            reconstruction_config_hash="8" * 64,
            source_format=SourceFormat.GAUSSIAN_LOG,
            source_encoding="ascii",
            started_at=datetime.now(UTC),
        )

        first_session = Session(database_engine)
        first_artifact = first_session.get(ArtifactFile, artifact_id)
        assert first_artifact is not None
        first_revision = persist_parse_revision(first_session, first_artifact, revision_record)
        assert first_revision.id is not None
        first_revision_id = first_revision.id

        def persist_in_second_session() -> None:
            try:
                with Session(database_engine) as second_session:
                    second_artifact = second_session.get(ArtifactFile, artifact_id)
                    assert second_artifact is not None
                    backend_pid = second_session.execute(text("SELECT pg_backend_pid()"))
                    backend_pids.put(int(backend_pid.scalar_one()))
                    second_revision = persist_parse_revision(
                        second_session,
                        second_artifact,
                        revision_record,
                    )
                    second_session.commit()
                    worker_results.put((second_revision.id, None))
            except Exception as error:
                worker_results.put((None, error))

        worker = Thread(target=persist_in_second_session, daemon=True)
        worker.start()
        second_backend_pid = backend_pids.get(timeout=5)

        deadline = monotonic() + 5
        waiting_for_advisory_lock = False
        with database_engine.connect() as observer:
            while monotonic() < deadline:
                waiting_for_advisory_lock = bool(
                    observer.execute(
                        text(
                            "SELECT EXISTS ("
                            "SELECT 1 FROM pg_locks "
                            "WHERE pid = :pid AND locktype = 'advisory' AND NOT granted)"
                        ),
                        {"pid": second_backend_pid},
                    ).scalar_one()
                )
                if waiting_for_advisory_lock:
                    break
                sleep(0.01)
        assert waiting_for_advisory_lock

        first_session.commit()
        worker.join(timeout=5)
        assert not worker.is_alive()
        second_revision_id, worker_error = worker_results.get(timeout=1)
        assert worker_error is None
        assert second_revision_id == first_revision_id

        with Session(database_engine) as verification_session:
            revisions = verification_session.exec(
                select(ParseRevision).where(ParseRevision.artifact_file_id == artifact_id)
            ).all()
            assert [revision.id for revision in revisions] == [first_revision_id]
    finally:
        if first_session is not None:
            first_session.rollback()
            first_session.close()
        if worker is not None and worker.is_alive():
            worker.join(timeout=5)
        if artifact_id is not None:
            with Session(database_engine) as cleanup_session:
                revisions = cleanup_session.exec(
                    select(ParseRevision).where(ParseRevision.artifact_file_id == artifact_id)
                ).all()
                for revision in revisions:
                    cleanup_session.delete(revision)
                cleanup_session.flush()
                artifact = cleanup_session.get(ArtifactFile, artifact_id)
                if artifact is not None:
                    cleanup_session.delete(artifact)
                cleanup_session.commit()
        database_engine.dispose()
