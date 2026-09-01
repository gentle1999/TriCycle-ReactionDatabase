"""Database-backed tests for software-neutral Geometry reuse."""

import os

import numpy as np
import pytest
from rdkit import Chem
from sqlalchemy import create_engine, text
from sqlmodel import Session

from tricycle_reaction_db.application.services.molecular_geometry import (
    GEOMETRY_MATCH_POLICY_VERSION,
    GeometryPersistenceContext,
    persist_molecular_geometry,
    preload_molecular_geometry_context,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import Geometry
from tricycle_reaction_db.domain.enums import GeometryAssignmentKind
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
            assert GEOMETRY_MATCH_POLICY_VERSION == "geometry-internal-coordinate-match-v4"
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_preloaded_batch_reuses_pending_equivalent_geometry() -> None:
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
    shifted = coordinates.copy()
    shifted[2, 1] += 2e-9
    source = normalize_molecule(
        molecule,
        coordinates,
        charge=0,
        multiplicity=1,
        reconstruction_method="geometry-batch-dedup-test",
        reconstruction_version="v1",
    )
    observation = normalize_molecule(
        molecule,
        shifted,
        charge=0,
        multiplicity=1,
        reconstruction_method="geometry-batch-dedup-test",
        reconstruction_version="v1",
    )
    assert source.geometry.geometry_hash != observation.geometry.geometry_hash

    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            session.info["tricycle_fast_insert"] = True
            context = GeometryPersistenceContext()
            preload_molecular_geometry_context(
                session,
                [(source, 8), (observation, 8)],
                context=context,
            )
            first = persist_molecular_geometry(
                session,
                source,
                coordinate_decimal_places=8,
                context=context,
            )
            second = persist_molecular_geometry(
                session,
                observation,
                coordinate_decimal_places=8,
                context=context,
            )

            assert second.geometry.id == first.geometry.id
            assert (
                second.geometry_assignment_kind is GeometryAssignmentKind.MATCHED_EXISTING_GEOMETRY
            )
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_same_coordinates_with_different_electronic_state_are_distinct_geometries() -> None:
    molecule = Chem.AddHs(Chem.MolFromSmiles("CO"))
    assert molecule is not None
    coordinates = np.column_stack(
        (
            np.arange(molecule.GetNumAtoms(), dtype=np.float64),
            np.zeros(molecule.GetNumAtoms()),
            np.zeros(molecule.GetNumAtoms()),
        )
    )
    singlet = normalize_molecule(
        molecule,
        coordinates,
        charge=0,
        multiplicity=1,
        reconstruction_method="geometry-state-test",
        reconstruction_version="v1",
    )
    triplet = normalize_molecule(
        molecule,
        coordinates,
        charge=0,
        multiplicity=3,
        reconstruction_method="geometry-state-test",
        reconstruction_version="v1",
    )
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            first = persist_molecular_geometry(session, singlet)
            second = persist_molecular_geometry(session, triplet)
            assert first.geometry.id != second.geometry.id
            assert (first.geometry.charge, first.geometry.multiplicity) == (0, 1)
            assert (second.geometry.charge, second.geometry.multiplicity) == (0, 3)
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_ambiguous_geometry_match_selects_nearest_candidate() -> None:
    molecule = Chem.AddHs(Chem.MolFromSmiles("CO"))
    assert molecule is not None
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
    alternate_coordinates = coordinates.copy()
    alternate_coordinates[2, 1] += 4e-7
    alternate_record = normalize_molecule(
        molecule,
        alternate_coordinates,
        charge=0,
        multiplicity=1,
        reconstruction_method="geometry-ambiguity-test",
        reconstruction_version="v1",
    )
    observed_coordinates = coordinates.copy()
    observed_coordinates[2, 1] += 3e-7
    observation = normalize_molecule(
        molecule,
        observed_coordinates,
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
            assert source.geometry.geometry_hash != alternate_record.geometry.geometry_hash
            assert source.geometry.geometry_hash != observation.geometry.geometry_hash
            assert alternate_record.geometry.geometry_hash != observation.geometry.geometry_hash
            internal = np.array(alternate_record.geometry.internal_coordinates, copy=True)
            alternate = Geometry(
                topology_id=authority.topology.id,
                topology=authority.topology,
                mol=Chem.Mol(alternate_record.geometry.mol),
                internal_coordinates=internal,
                internal_coordinate_distances_angstrom=internal[:, 0].tolist(),
                internal_coordinate_angles_degrees=internal[:, 1].tolist(),
                internal_coordinate_dihedrals_degrees=internal[:, 2].tolist(),
                minimum_coordinate_decimal_places=6,
                internal_coordinate_hash=alternate_record.geometry.internal_coordinate_hash,
                geometry_hash=alternate_record.geometry.geometry_hash,
                charge=alternate_record.geometry.charge,
                multiplicity=alternate_record.geometry.multiplicity,
                canonicalization_version=alternate_record.geometry.canonicalization_version,
            )
            session.add(alternate)
            session.flush()

            assert _database_internal_coordinate_match(
                source.geometry.internal_coordinates,
                observation.geometry.internal_coordinates,
                candidate_decimal_places=6,
                observed_decimal_places=6,
            )
            assert _database_internal_coordinate_match(
                alternate_record.geometry.internal_coordinates,
                observation.geometry.internal_coordinates,
                candidate_decimal_places=6,
                observed_decimal_places=6,
            )

            matched = persist_molecular_geometry(
                session,
                observation,
                coordinate_decimal_places=6,
            )
            assert matched.geometry.id == alternate.id
            assert (
                matched.geometry_assignment_kind is GeometryAssignmentKind.MATCHED_EXISTING_GEOMETRY
            )
            assert matched.coordinate_rmsd_angstrom >= 0
            assert matched.coordinate_max_abs_angstrom >= matched.coordinate_rmsd_angstrom
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()
