import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from molop import AutoParser, molopconfig
from rdkit import Chem
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import undefer
from sqlmodel import Session, select

from tricycle_reaction_db.application.dtos import NormalizedMoleculeRecord
from tricycle_reaction_db.application.services import (
    persist_artifact_file,
    persist_calculation_protocol,
    persist_molecular_geometry,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import ArtifactFile, CalculationProtocol, Geometry
from tricycle_reaction_db.domain.enums import ArtifactKind, QMSoftware
from tricycle_reaction_db.ingestion import (
    artifact_record_from_path,
    calculation_protocol_record,
    normalize_molop_frame,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def parsed_da_bench_records(
    da_bench_manifest: dict[str, Any],
    da_bench_log_paths: dict[str, Path],
) -> dict[str, NormalizedMoleculeRecord]:
    molopconfig.show_progress_bar = False
    expected = {entry["role"]: entry for entry in da_bench_manifest["logs"]}
    records: dict[str, NormalizedMoleculeRecord] = {}

    for role, path in da_bench_log_paths.items():
        parsed_file = AutoParser(str(path), n_jobs=1)[0]
        fixture = expected[role]
        assert len(parsed_file) == fixture["frame_count"]
        assert (
            path.read_bytes().count(b"Normal termination of Gaussian")
            == fixture["normal_termination_count"]
        )
        assert all(len(frame.atoms) == fixture["atom_count"] for frame in parsed_file)

        frame_records = [normalize_molop_frame(frame) for frame in parsed_file]
        assert {record.topology.canonical_isomeric_smiles for record in frame_records} == {
            fixture["final_topology_smiles"]
        }
        final_record = frame_records[-1]
        assert final_record.formula.hill_formula == fixture["final_formula"]
        assert (
            parsed_file[-1].vibrations.num_imaginary == fixture["final_imaginary_frequency_count"]
        )
        records[role] = final_record

    return records


def test_real_da_fixture_preserves_distinct_ts_and_product_topologies(
    parsed_da_bench_records: dict[str, NormalizedMoleculeRecord],
) -> None:
    transition_state = parsed_da_bench_records["transition_state"]
    product = parsed_da_bench_records["product"]

    assert transition_state.formula.composition_hash == product.formula.composition_hash
    assert transition_state.formula.hill_formula == product.formula.hill_formula == "C8H10O2S"
    assert transition_state.topology.graph_hash != product.topology.graph_hash
    assert transition_state.topology.fragment_count == 2
    assert product.topology.fragment_count == 1


@pytest.mark.skipif(
    os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
    reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
)
def test_real_da_fixture_round_trips_through_core_business_models(
    parsed_da_bench_records: dict[str, NormalizedMoleculeRecord],
    da_bench_log_paths: dict[str, Path],
) -> None:
    database_engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = database_engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            persisted = {
                role: persist_molecular_geometry(session, record)
                for role, record in parsed_da_bench_records.items()
            }
            duplicate = persist_molecular_geometry(
                session, parsed_da_bench_records["transition_state"]
            )
            artifacts = {
                role: persist_artifact_file(
                    session,
                    artifact_record_from_path(
                        path,
                        bucket="tricycle-raw",
                        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                    ),
                )
                for role, path in da_bench_log_paths.items()
            }
            duplicate_artifact = persist_artifact_file(
                session,
                artifact_record_from_path(
                    da_bench_log_paths["transition_state"],
                    bucket="tricycle-raw",
                    artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                ),
            )
            protocol = persist_calculation_protocol(
                session,
                calculation_protocol_record(
                    qm_software=QMSoftware.GAUSSIAN,
                    qm_software_version="ES64L-G16RevA.03",
                    method_family="DFT",
                    method="DFT",
                    functional="RB3LYP",
                    basis_set="def2SVP",
                    task_requests=["freq", "opt", "freq"],
                    normalized_spec={"fixture": "da-bench-minimal"},
                ),
            )
            session.commit()

            assert duplicate.geometry.id == persisted["transition_state"].geometry.id
            assert duplicate_artifact.id == artifacts["transition_state"].id
            assert all(item.formula.id is not None for item in persisted.values())
            assert all(item.formula.id.version == 7 for item in persisted.values())
            assert len({item.formula.id for item in persisted.values()}) == 3
            assert len({item.topology.id for item in persisted.values()}) == 4
            assert len({item.geometry.id for item in persisted.values()}) == 4
            assert len({artifact.id for artifact in artifacts.values()}) == 4
            assert protocol.id is not None and protocol.id.version == 7
            assert protocol.task_requests == ["freq", "opt"]
            assert artifacts["transition_state"].object_key.endswith(
                "de24aad074d036c6ecedee3b47ece00a56591f8050afd573ad609f00fbcf858b"
            )

            product = persisted["product"]
            assert product.topology.formula is product.formula
            assert product.geometry.topology is product.topology
            assert isinstance(product.topology.mol, Chem.Mol)

        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            artifact = session.exec(
                select(ArtifactFile).where(ArtifactFile.id == artifacts["transition_state"].id)
            ).one()
            loaded_protocol = session.exec(
                select(CalculationProtocol).where(CalculationProtocol.id == protocol.id)
            ).one()
            assert artifact.artifact_kind is ArtifactKind.CALCULATION_OUTPUT
            assert loaded_protocol.qm_software is QMSoftware.GAUSSIAN

            unloaded = session.exec(select(Geometry)).first()
            assert unloaded is not None
            assert "internal_coordinates" in inspect(unloaded).unloaded
            with pytest.raises(InvalidRequestError, match="raiseload"):
                _ = unloaded.internal_coordinates

            loaded = session.exec(
                select(Geometry).options(undefer(Geometry.internal_coordinates))
            ).first()
            assert loaded is not None
            assert isinstance(loaded.internal_coordinates, np.ndarray)
            assert loaded.internal_coordinates.shape == (loaded.atom_count, 3)
            assert not loaded.internal_coordinates.flags.writeable
            assert loaded.topology.mol.GetNumConformers() == 0
            assert all(atom.GetAtomMapNum() == 0 for atom in loaded.topology.mol.GetAtoms())
            assert loaded.mol.GetNumConformers() == 1
            assert loaded.mol.GetConformer().Is3D()
            assert [atom.GetAtomicNum() for atom in loaded.mol.GetAtoms()] == [
                atom.GetAtomicNum() for atom in loaded.topology.mol.GetAtoms()
            ]
    finally:
        transaction.rollback()
        connection.close()
        database_engine.dispose()
