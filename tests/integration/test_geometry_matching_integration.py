"""Database-backed tests for software-neutral Geometry reuse."""

import os
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import numpy as np
import pytest
from rdkit import Chem
from sqlalchemy import create_engine, text
from sqlmodel import Session

from tricycle_reaction_db.application.services.artifact_uploads import (
    _mark_ingestion_failed,
)
from tricycle_reaction_db.application.services.molecular_geometry import (
    GEOMETRY_MATCH_POLICY_VERSION,
    GeometryAssignmentAmbiguityError,
    persist_molecular_geometry,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import ArtifactFile, ArtifactIngestion, Geometry
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactKind,
    ArtifactVisibility,
    GeometryAssignmentKind,
    StorageStatus,
)
from tricycle_reaction_db.domain.identity import DEVELOPMENT_USER_ID, SYSTEM_PROJECT_ID
from tricycle_reaction_db.ingestion import normalize_molecule

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


def _database_internal_coordinate_match(
    candidate: np.ndarray,
    observed: np.ndarray,
    *,
    candidate_decimal_places: int | None = 8,
    observed_decimal_places: int | None = 8,
) -> bool:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            return bool(
                connection.execute(
                    text(
                        """
                        SELECT geometry_internal_coordinates_equivalent(
                            CAST(:candidate_distances AS double precision[]),
                            CAST(:candidate_angles AS double precision[]),
                            CAST(:candidate_dihedrals AS double precision[]),
                            CAST(:candidate_decimal_places AS smallint),
                            CAST(:observed_distances AS double precision[]),
                            CAST(:observed_angles AS double precision[]),
                            CAST(:observed_dihedrals AS double precision[]),
                            CAST(:observed_decimal_places AS smallint)
                        )
                        """
                    ),
                    {
                        "candidate_distances": candidate[:, 0].tolist(),
                        "candidate_angles": candidate[:, 1].tolist(),
                        "candidate_dihedrals": candidate[:, 2].tolist(),
                        "candidate_decimal_places": candidate_decimal_places,
                        "observed_distances": observed[:, 0].tolist(),
                        "observed_angles": observed[:, 1].tolist(),
                        "observed_dihedrals": observed[:, 2].tolist(),
                        "observed_decimal_places": observed_decimal_places,
                    },
                ).scalar_one()
            )
    finally:
        engine.dispose()


def test_database_internal_coordinate_match_preserves_periodicity_and_linear_exemption() -> None:
    reference = np.asarray(
        [[0.0, 0.0, 0.0], [1.2, 0.0, 0.0], [1.1, 109.5, 0.0], [1.0, 111.0, 179.9]],
        dtype=np.float64,
    )
    periodic_observation = reference.copy()
    periodic_observation[1, 0] += 2e-9
    periodic_observation[3, 2] = -180.1
    assert _database_internal_coordinate_match(reference, periodic_observation)

    linear_observation = reference.copy()
    linear_observation[3, 1] = 180.0
    linear_observation[3, 2] = -60.0
    linear_reference = reference.copy()
    linear_reference[3, 1] = 180.0
    linear_reference[3, 2] = 60.0
    assert _database_internal_coordinate_match(linear_reference, linear_observation)

    different_conformer = reference.copy()
    different_conformer[3, 2] = 60.0
    assert not _database_internal_coordinate_match(reference, different_conformer)


def test_printing_precision_observation_reuses_one_geometry() -> None:
    base = Chem.MolFromSmiles("[13CH3][15NH2]")
    assert base is not None
    molecule = Chem.AddHs(base)
    atom_indices = np.arange(molecule.GetNumAtoms(), dtype=np.float64)
    coordinates = np.column_stack(
        (
            atom_indices * 0.7,
            np.mod(np.square(atom_indices), 5.0) * 0.3,
            np.mod(np.power(atom_indices, 3), 7.0) * 0.2,
        )
    )
    transformed = coordinates.copy()
    transformed[2, 1] += 2e-9
    source = normalize_molecule(
        molecule,
        coordinates,
        charge=0,
        multiplicity=1,
        reconstruction_method="geometry-match-test",
        reconstruction_version="v1",
    )
    observation = normalize_molecule(
        molecule,
        transformed,
        charge=0,
        multiplicity=1,
        reconstruction_method="geometry-match-test",
        reconstruction_version="v1",
    )
    angle = np.deg2rad(37.0)
    rotation = np.asarray(
        [
            [np.cos(angle), -np.sin(angle), 0.0],
            [np.sin(angle), np.cos(angle), 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    rotated = normalize_molecule(
        molecule,
        coordinates @ rotation + np.asarray([4.5, -2.0, 7.25]),
        charge=0,
        multiplicity=1,
        reconstruction_method="geometry-match-test",
        reconstruction_version="v1",
    )

    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            authority = persist_molecular_geometry(
                session,
                source,
                coordinate_decimal_places=8,
            )
            matched = persist_molecular_geometry(
                session,
                observation,
                coordinate_decimal_places=8,
            )
            equivariant_match = persist_molecular_geometry(
                session,
                rotated,
                coordinate_decimal_places=8,
            )

            assert matched.geometry.id == authority.geometry.id
            assert (
                matched.geometry_assignment_kind is GeometryAssignmentKind.MATCHED_EXISTING_GEOMETRY
            )
            assert (
                matched.observed_to_geometry_atom_indices
                == observation.observed_to_geometry_atom_indices
            )
            assert matched.coordinate_rmsd_angstrom is not None
            assert matched.coordinate_max_abs_angstrom is not None
            assert 0 < matched.coordinate_rmsd_angstrom < 1e-9
            assert matched.coordinate_rmsd_angstrom <= matched.coordinate_max_abs_angstrom < 2e-9
            assert equivariant_match.geometry.id == authority.geometry.id
            assert equivariant_match.geometry_assignment_kind is GeometryAssignmentKind.PARSED_EXACT
            assert equivariant_match.coordinate_rmsd_angstrom < 1e-12
            assert equivariant_match.coordinate_max_abs_angstrom < 1e-12
            assert len(equivariant_match.observed_to_geometry_transform) == 16
            assert np.array_equal(
                source.geometry.internal_coordinates,
                rotated.geometry.internal_coordinates,
            )
            assert GEOMETRY_MATCH_POLICY_VERSION == "geometry-internal-coordinate-match-v3"
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_ambiguous_geometry_match_is_rejected_with_persisted_versioned_qc_evidence() -> None:
    molecule = Chem.AddHs(Chem.MolFromSmiles("CO"))
    atom_indices = np.arange(molecule.GetNumAtoms(), dtype=np.float64)
    coordinates = np.column_stack(
        (
            atom_indices * 0.9,
            np.mod(np.square(atom_indices), 5.0) * 0.2,
            np.mod(np.power(atom_indices, 3), 7.0) * 0.15,
        )
    )
    source = normalize_molecule(
        molecule,
        coordinates,
        charge=0,
        multiplicity=1,
        reconstruction_method="geometry-ambiguity-test",
        reconstruction_version="v1",
    )
    shifted_coordinates = coordinates.copy()
    shifted_coordinates[2, 1] += 2e-9
    observation = normalize_molecule(
        molecule,
        shifted_coordinates,
        charge=0,
        multiplicity=1,
        reconstruction_method="geometry-ambiguity-test",
        reconstruction_version="v1",
    )

    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            authority = persist_molecular_geometry(
                session,
                source,
                coordinate_decimal_places=8,
            )
            assert authority.topology.id is not None
            internal = np.array(source.geometry.internal_coordinates, copy=True)
            alternate = Geometry(
                topology_id=authority.topology.id,
                topology=authority.topology,
                mol=Chem.Mol(authority.geometry.mol),
                internal_coordinates=internal,
                internal_coordinate_distances_angstrom=internal[:, 0].tolist(),
                internal_coordinate_angles_degrees=internal[:, 1].tolist(),
                internal_coordinate_dihedrals_degrees=internal[:, 2].tolist(),
                internal_coordinate_hash=source.geometry.internal_coordinate_hash,
                geometry_hash=sha256(f"ambiguous-{uuid4()}".encode()).hexdigest(),
                canonicalization_version=source.geometry.canonicalization_version,
            )
            session.add(alternate)
            session.flush()

            with pytest.raises(GeometryAssignmentAmbiguityError) as caught:
                persist_molecular_geometry(
                    session,
                    observation,
                    coordinate_decimal_places=8,
                )

            error = caught.value
            assert set(error.candidate_ids) == {authority.geometry.id, alternate.id}
            evidence = error.evidence()
            assert evidence["rule_id"] == "geometry.unique-coordinate-match"
            assert evidence["policy_version"] == GEOMETRY_MATCH_POLICY_VERSION
            assert evidence["outcome"] == "fail"
            assert evidence["candidate_count"] == 2

            digest = sha256(f"geometry-ambiguity-ingestion-{uuid4()}".encode()).hexdigest()
            artifact = ArtifactFile(
                project_id=SYSTEM_PROJECT_ID,
                created_by_user_id=DEVELOPMENT_USER_ID,
                visibility=ArtifactVisibility.PROJECT,
                bucket="integration-test",
                object_key=f"integration/geometry-ambiguity/{digest}",
                content_sha256=digest,
                size_bytes=1,
                original_filename="ambiguous.log",
                media_type="text/plain",
                artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                storage_status=StorageStatus.AVAILABLE,
            )
            session.add(artifact)
            session.flush()
            assert artifact.id is not None
            ingestion = ArtifactIngestion(
                artifact_file_id=artifact.id,
                artifact_file=artifact,
                parser_version="test",
                started_at=datetime.now(UTC),
            )
            session.add(ingestion)
            session.flush()
            assert ingestion.id is not None
            _mark_ingestion_failed(
                session,
                ingestion_id=ingestion.id,
                error=error,
                error_code="calculation_persistence_failed",
                completed_at=datetime.now(UTC),
            )
            session.flush()

            assert ingestion.status is ArtifactIngestionStatus.FAILED
            assert ingestion.error_code == "geometry_assignment_ambiguous"
            assert ingestion.parser_metadata["qc_rejection"] == evidence
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()
