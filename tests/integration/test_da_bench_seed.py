import gzip
import json
import os
import shutil
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from rdkit.Chem import rdChemReactions
from sqlalchemy import create_engine
from sqlmodel import Session, col, select

from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    CalculationFrame,
    MappedReaction,
    ParseRevision,
    TransitionStateEndpoint,
)
from tricycle_reaction_db.dev.seed_da_bench import seed_da_bench_fixture
from tricycle_reaction_db.domain.enums import TransitionStateEndpointDirection
from tricycle_reaction_db.storage.rustfs import RustFSObjectStore, RustFSSettings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.rustfs,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1"
        or os.getenv("TRICYCLE_RUN_RUSTFS_TESTS") != "1",
        reason="set database and RustFS integration flags to run DA seed integration tests",
    ),
]


def _isolated_fixture(source_root: Path, target_root: Path) -> None:
    """Copy the fixture with isolated artifact and mapped-reaction identities."""

    shutil.copytree(source_root, target_root)
    manifest_path = target_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    marker = uuid4().hex.encode("ascii")

    for entry in manifest["logs"]:
        compressed_path = target_root / entry["relative_path"]
        payload = gzip.decompress(compressed_path.read_bytes())
        # The suffix leaves the completed Gaussian calculation untouched while
        # making RustFS/Artifact identity independent from other test runs.
        payload += b"\nTriCycle DA seed integration marker " + marker + b"\n"
        compressed = gzip.compress(payload, compresslevel=9, mtime=0)
        compressed_path.write_bytes(compressed)
        entry["source_size_bytes"] = len(payload)
        entry["source_sha256"] = sha256(payload).hexdigest()
        entry["gzip_sha256"] = sha256(compressed).hexdigest()

    workflow = manifest["workflow"]
    definition = rdChemReactions.ReactionFromSmarts(
        workflow["mapped_reaction_smiles"][0],
        useSmiles=True,
    )
    assert definition is not None
    map_offset = 1_000_000 + uuid4().int % 1_000_000
    for templates in (definition.GetReactants(), definition.GetProducts()):
        for template in templates:
            for atom in template.GetAtoms():
                atom.SetAtomMapNum(atom.GetAtomMapNum() + map_offset)
    workflow["mapped_reaction_smiles"] = [rdChemReactions.ReactionToSmiles(definition, True)]
    for participant in workflow["participants"]:
        participant["source_atom_map_numbers"] = [
            atom_map + map_offset for atom_map in participant["source_atom_map_numbers"]
        ]
    for node in workflow["nodes"]:
        for component in node["components"]:
            component["source_atom_map_numbers"] = [
                atom_map + map_offset for atom_map in component["source_atom_map_numbers"]
            ]
    workflow["manifest_key"] = f"integration-seed:{marker.decode('ascii')}"
    workflow["reaction_key"] = f"integration-seed:{marker.decode('ascii')}"
    workflow["path_key"] = f"integration-seed:{map_offset}"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_da_bench_seed_is_idempotent_across_postgres_and_rustfs(tmp_path: Path) -> None:
    fixture_root = tmp_path / "fixture"
    _isolated_fixture(Path("tests/fixtures/da_bench_minimal"), fixture_root)

    bucket = f"tricycle-da-seed-{uuid4().hex}"
    store_settings = RustFSSettings().model_copy(update={"bucket": bucket})
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    written_keys: set[str] = set()
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with RustFSObjectStore(store_settings) as store:
            store.ensure_bucket()
            with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                first = seed_da_bench_fixture(session, store, fixture_root)
                session.commit()
                written_keys = {item.key for item in store.iter_objects(prefix="raw")}
                assert len(written_keys) == 4

                second = seed_da_bench_fixture(session, store, fixture_root)
                session.commit()
                assert asdict(second) == asdict(first)
                assert {item.key for item in store.iter_objects(prefix="raw")} == written_keys

                assert first.frame_counts == {
                    "ene": 5,
                    "diene": 7,
                    "transition_state": 23,
                    "product": 10,
                }
                assert sum(first.array_counts.values()) == 227
                assert set(first.node_ids) == {"reactants", "transition-state", "product"}
                mapped_reaction = session.get(MappedReaction, first.mapped_reaction_id)
                assert mapped_reaction is not None
                assert mapped_reaction.minimum_activation_gibbs_free_energy_kcal_mol is not None
                assert mapped_reaction.minimum_reaction_gibbs_free_energy_kcal_mol is not None
                endpoints = session.exec(
                    select(TransitionStateEndpoint)
                    .join(
                        CalculationFrame,
                        col(TransitionStateEndpoint.calculation_frame_id)
                        == col(CalculationFrame.id),
                    )
                    .join(
                        ParseRevision,
                        col(CalculationFrame.parse_revision_id) == col(ParseRevision.id),
                    )
                    .where(col(ParseRevision.artifact_file_id).in_(first.artifact_ids.values()))
                ).all()
                assert {endpoint.direction for endpoint in endpoints} == {
                    TransitionStateEndpointDirection.NEGATIVE,
                    TransitionStateEndpointDirection.POSITIVE,
                }
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()
        with RustFSObjectStore(store_settings) as store:
            # The first seed can fail before ``written_keys`` is populated, so
            # enumerate this test's uniquely named bucket during cleanup.
            for item in store.iter_objects(prefix="raw"):
                if store.exists(item.key):
                    store.delete(item.key)
