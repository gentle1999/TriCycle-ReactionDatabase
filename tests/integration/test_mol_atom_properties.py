import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor

import pytest
from molgr.utils.converter import METAL_UNPAIRED_ELECTRONS_PROP as METAL_SPIN
from rdkit import Chem
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Session

from tricycle_reaction_db.application.services._persistence import (
    _bulk_insert_pending_entities,
    _copy_compatible,
)
from tricycle_reaction_db.application.services.artifact_uploads import (
    _initialize_molop_process_worker,
)
from tricycle_reaction_db.application.services.molecular_geometry import (
    GeometryPersistenceContext,
    persist_molecular_topology,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import MolecularTopology
from tricycle_reaction_db.db.types.annotated_mol import AnnotatedRdkitMol
from tricycle_reaction_db.domain.identity import SYSTEM_PROJECT_ID
from tricycle_reaction_db.domain.mol_properties import mol_atom_properties
from tricycle_reaction_db.ingestion.molop import configure_molecular_graph_reconstruction
from tricycle_reaction_db.ingestion.normalization import normalize_topology


def _normalize_in_child(molecule):
    return normalize_topology(
        molecule,
        add_hydrogens=False,
        reconstruction_method="molgr/cpp",
        reconstruction_version="test",
    )


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1", reason="database tests disabled"
    ),
]


def test_metal_spin_survives_cartridge_storage_and_aliased_scalar_reads() -> None:
    table = Table(
        "_mol_spin_roundtrip_probe",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("mol", AnnotatedRdkitMol(return_type="mol")),
        Column("mol_atom_properties", JSONB),
        prefixes=["TEMPORARY"],
    )
    molecule = Chem.MolFromSmiles("[Fe]<-N/C=C/F")
    molecule.GetAtomWithIdx(0).SetIntProp(METAL_SPIN, 4)
    molecule.GetAtomWithIdx(0).SetNumRadicalElectrons(0)
    # Production bulk ingestion must retain mol_from_pkl(), not COPY raw
    # pickle bytes into the cartridge's text SMILES input function.
    assert not _copy_compatible(tuple(table.columns))
    engine = create_engine(get_settings().database_url)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                table.create(connection)
                connection.execute(
                    table.insert(),
                    {
                        "id": 1,
                        "mol": molecule,
                        "mol_atom_properties": mol_atom_properties(molecule),
                    },
                )
                for selected in (table, table.alias("aliased_spin")):
                    restored = connection.execute(select(selected.c.mol)).scalar_one()
                    assert restored.GetAtomWithIdx(0).GetIntProp(METAL_SPIN) == 4
                    assert restored.GetAtomWithIdx(0).GetNumRadicalElectrons() == 0
                    assert Chem.MolToSmiles(restored) == Chem.MolToSmiles(molecule)
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def test_fast_ingestion_retains_metal_spin() -> None:
    molecule = Chem.AddHs(Chem.MolFromSmiles("[Fe]<-N/C=C/F"))
    molecule.GetAtomWithIdx(0).SetIntProp(METAL_SPIN, 4)
    configure_molecular_graph_reconstruction()
    with ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_initialize_molop_process_worker,
    ) as pool:
        record = pool.submit(_normalize_in_child, molecule).result(timeout=30)
    assert any(not k.startswith("radical:") for k in record.topology.mol_atom_properties)
    engine = create_engine(get_settings().database_url)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                    session.info["tricycle_fast_insert"] = True
                    persisted = persist_molecular_topology(
                        session,
                        record,
                        context=GeometryPersistenceContext(project_id=SYSTEM_PROJECT_ID),
                    )
                    _bulk_insert_pending_entities(session)
                    restored = session.execute(
                        select(MolecularTopology.mol).where(
                            MolecularTopology.id == persisted.topology.id
                        )
                    ).scalar_one()
                    metal = next(a for a in restored.GetAtoms() if a.GetSymbol() == "Fe")
                    assert metal.GetIntProp(METAL_SPIN) == 4
                    assert metal.GetNumRadicalElectrons() == 0
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
