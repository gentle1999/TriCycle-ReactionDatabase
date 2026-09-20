import asyncio
import json
import tarfile
from collections.abc import Iterator
from gzip import open as open_gzip
from hashlib import sha256
from pathlib import Path
from time import monotonic

import pytest
from rdkit import Chem
from rdkit.Chem import rdChemReactions

from tricycle_reaction_db.application.services import artifact_uploads, rdkit_graph_matching
from tricycle_reaction_db.application.services.artifact_upload_types import (
    _FailedInference,
    _SuccessfulInference,
)
from tricycle_reaction_db.application.services.rdkit_graph_matching import (
    MolecularGraphMatchTimeoutError,
    get_substruct_matches,
)
from tricycle_reaction_db.core.chemistry_config import INVERSION_LABILE_RULES
from tricycle_reaction_db.core.config import Settings
from tricycle_reaction_db.ingestion.normalization import (
    clear_inversion_labile_atom_chirality,
    serialize_molecule_smiles,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures/real_world_extremes"
BATCH_FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures/real_world_batch_256"
DAG_SOURCE = FIXTURE_ROOT / "1-s2.0-S2451929422005617-mmc2__24.log.gz"
DAG_ENDPOINTS = FIXTURE_ROOT / "1-s2.0-S2451929422005617-mmc2__24.endpoints.sdf"
LARGE_SOURCE = FIXTURE_ROOT / "1-s2.0-S2451929422005617-mmc2__26.log.gz"
LARGE_ENDPOINTS = FIXTURE_ROOT / "1-s2.0-S2451929422005617-mmc2__26.endpoints.sdf"
FIXTURE_MANIFEST = FIXTURE_ROOT / "manifest.json"
BATCH_FIXTURE_MANIFEST = BATCH_FIXTURE_ROOT / "manifest.json"


def _endpoint_molecules(path: Path) -> tuple[Chem.Mol, ...]:
    molecules = tuple(molecule for molecule in Chem.SDMolSupplier(str(path), removeHs=False))
    assert molecules and all(molecule is not None for molecule in molecules)
    return tuple(molecule for molecule in molecules if molecule is not None)


def _uncompressed_sha256(path: Path) -> str:
    digest = sha256()
    with open_gzip(path, "rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def test_real_world_batch_corpus_has_256_distinct_balanced_source_files() -> None:
    manifest = json.loads(BATCH_FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    samples = manifest["samples"]
    assert manifest["expected_file_count"] == 256
    assert manifest["reaction_pair_count"] == 128
    assert len(samples) == 256
    assert len({sample["file"] for sample in samples}) == 256
    assert len({sample["pair_id"] for sample in samples}) == 128
    assert {
        category: sum(sample["category"] == category for sample in samples)
        for category in ("prod", "ts")
    } == {
        "prod": 128,
        "ts": 128,
    }
    assert manifest["storage_format"] == "sharded tar.gz archives containing raw .log members"
    archive_paths = sorted(BATCH_FIXTURE_ROOT.glob("corpus-*.tar.gz"))
    assert len(archive_paths) == 8
    archived_files: set[str] = set()
    for archive_path in archive_paths:
        assert archive_path.stat().st_size < 50 * 1024**2
        with tarfile.open(archive_path, "r:gz") as archive:
            members = {member.name: member for member in archive.getmembers()}
            log_members = {
                name: member for name, member in members.items() if name.endswith(".log")
            }
            expected_in_shard = {
                sample["file"] for sample in samples if sample["archive_file"] == archive_path.name
            }
            assert set(log_members) == expected_in_shard
            assert all(member.isfile() for member in log_members.values())
            assert "manifest.json" in members
            embedded_manifest = archive.extractfile("manifest.json")
            assert embedded_manifest is not None
            assert json.loads(embedded_manifest.read()) == manifest
            first_log = archive.extractfile(next(iter(log_members)))
            assert first_log is not None
            assert not first_log.read(2).startswith(b"\x1f\x8b")
            archived_files.update(log_members)
    assert archived_files == {sample["file"] for sample in samples}
    assert not tuple(BATCH_FIXTURE_ROOT.glob("*.log.gz"))


@pytest.fixture(autouse=True)
def close_shared_molop_pool_after_test() -> Iterator[None]:
    yield
    asyncio.run(artifact_uploads.close_molop_process_pool())


@pytest.mark.asyncio
async def test_real_segmented_and_large_gaussian_files_run_through_shared_frame_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        molop_batch_n_jobs=2,
        molop_file_parse_timeout_seconds=60.0,
        molop_file_parse_timeout_size_multiplier=1.5,
    )
    monkeypatch.setattr(artifact_uploads, "get_settings", lambda: settings)

    assert _uncompressed_sha256(DAG_SOURCE) == (
        "f2c30cf0e1e69ae5b533ac82a7e0032ba736b8e45a90e6205a14961680c43c97"
    )
    assert _uncompressed_sha256(LARGE_SOURCE) == (
        "6fbab2f871041d6cd27508995718c61261bd6e3f20f6487807062752c9c26401"
    )
    assert sha256(DAG_ENDPOINTS.read_bytes()).hexdigest() == (
        "653887dae41050609a76cf6566f5b433600bab93f733327b768de240132aa61e"
    )
    assert sha256(LARGE_ENDPOINTS.read_bytes()).hexdigest() == (
        "c677085456198d0b792c3618a6387e849f43b2cc1452ef260ae71bb07d7308ca"
    )

    dag_source = await artifact_uploads._run_molop_source_parser(
        DAG_SOURCE,
        DAG_SOURCE.name.removesuffix(".gz"),
    )
    assert dag_source.source_format == "g16log"
    assert dag_source.source_frame_count == 77
    assert [
        (segment.segment_index, segment.frame_count)
        for segment in dag_source.chem_file.source_segments
    ] == [
        (0, 76),
        (1, 1),
    ]

    # This exact 19.6 MiB file previously timed out while each frame ran
    # whole-molecule SMARTS matching. Exercise the same shared parser and frame
    # worker path used by uploads, including the size-derived parse deadline.
    assert artifact_uploads._source_size_bytes(LARGE_SOURCE) == 19_620_917
    assert artifact_uploads._molop_file_parse_timeout_seconds(LARGE_SOURCE) == pytest.approx(
        138.4,
        abs=0.1,
    )
    parsed = await artifact_uploads._run_molop_file_pipeline(
        LARGE_SOURCE,
        LARGE_SOURCE.name.removesuffix(".gz"),
        submission_slots=asyncio.Semaphore(2),
        file_slots=asyncio.Semaphore(1),
    )

    assert parsed.source_format == "g16log"
    assert parsed.source_frame_count == len(parsed.frame_records) == 219
    assert [
        (segment.segment_index, segment.frame_count) for segment in parsed.chem_file.source_segments
    ] == [
        (0, 218),
        (1, 1),
    ]
    assert [record.frame.file_frame_index for record in parsed.frame_records] == list(range(219))
    assert max(record.molecule.topology.atom_count for record in parsed.frame_records) >= 100
    assert parsed.parse_diagnostics == ()
    assert len(parsed.inferences) == 1
    inference = parsed.inferences[0]
    assert isinstance(inference, _SuccessfulInference)
    assert inference.file_frame_index == 218
    reaction = rdChemReactions.ReactionFromSmarts(inference.reaction_smiles, useSmiles=True)
    assert reaction is not None
    assert reaction.GetNumReactantTemplates() >= 2
    assert reaction.GetNumProductTemplates() >= 1


@pytest.mark.asyncio
async def test_additional_real_world_sources_keep_frames_segments_and_ts_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        molop_batch_n_jobs=2,
        molop_file_parse_timeout_seconds=60.0,
        molop_file_parse_timeout_size_multiplier=1.5,
    )
    monkeypatch.setattr(artifact_uploads, "get_settings", lambda: settings)
    manifest = json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    existing_sources = {DAG_SOURCE.name, LARGE_SOURCE.name}
    samples = tuple(
        sample for sample in manifest["samples"] if sample["file"] not in existing_sources
    )
    assert len(samples) == 5
    file_slots = asyncio.Semaphore(2)
    frame_slots = asyncio.Semaphore(4)

    async def parse_sample(sample: dict[str, object]) -> None:
        source_path = FIXTURE_ROOT / str(sample["file"])
        assert _uncompressed_sha256(source_path) == sample["source_sha256"]
        parsed = await artifact_uploads._run_molop_file_pipeline(
            source_path,
            source_path.name.removesuffix(".gz"),
            submission_slots=frame_slots,
            file_slots=file_slots,
        )
        expected_frames = int(sample["source_frame_count"])
        assert parsed.source_format == "g16log"
        assert parsed.source_frame_count == len(parsed.frame_records) == expected_frames
        assert [segment.frame_count for segment in parsed.chem_file.source_segments] == sample[
            "segment_frame_counts"
        ]
        assert len(parsed.inferences) == sample["inference_count"]
        assert not any(isinstance(item, _FailedInference) for item in parsed.inferences)
        assert all(isinstance(item, _SuccessfulInference) for item in parsed.inferences)
        assert parsed.parse_diagnostics == ()

    await asyncio.gather(*(parse_sample(sample) for sample in samples))


@pytest.mark.parametrize(
    ("fixture", "atom_count"),
    [(DAG_ENDPOINTS, 78), (LARGE_ENDPOINTS, 102)],
)
def test_real_signed_endpoints_have_stable_lossless_smiles_round_trips(
    fixture: Path,
    atom_count: int,
) -> None:
    for molecule in _endpoint_molecules(fixture):
        assert molecule.GetNumAtoms() == atom_count
        serialized = serialize_molecule_smiles(molecule)
        parser = Chem.SmilesParserParams()
        parser.removeHs = False
        restored = Chem.MolFromSmiles(serialized, parser)
        assert restored is not None
        assert restored.GetNumAtoms() == atom_count
        assert serialize_molecule_smiles(restored) == serialized


def test_large_real_endpoint_uses_atom_local_inversion_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    molecule = _endpoint_molecules(LARGE_ENDPOINTS)[0]

    def fail_if_whole_graph_matching_is_used(*_: object, **__: object) -> object:
        raise AssertionError("single-atom SMARTS must not invoke whole-molecule matching")

    monkeypatch.setattr(
        rdkit_graph_matching,
        "get_substruct_matches",
        fail_if_whole_graph_matching_is_used,
    )
    cleaned = clear_inversion_labile_atom_chirality(molecule)

    assert cleaned.GetNumAtoms() == 102
    assert tuple(bond.GetStereo() for bond in cleaned.GetBonds()) == tuple(
        bond.GetStereo() for bond in molecule.GetBonds()
    )
    for rule in INVERSION_LABILE_RULES:
        query = Chem.MolFromSmarts(rule.atom_smarts)
        assert query is not None
        query_atom = query.GetAtomWithIdx(0)
        local_matches = {atom.GetIdx() for atom in molecule.GetAtoms() if query_atom.Match(atom)}
        reference_matches = {
            match[0]
            for match in molecule.GetSubstructMatches(query, uniquify=True, maxMatches=1_000)
        }
        assert local_matches == reference_matches
        for atom in molecule.GetAtoms():
            if query_atom.Match(atom):
                assert (
                    cleaned.GetAtomWithIdx(atom.GetIdx()).GetChiralTag()
                    is Chem.ChiralType.CHI_UNSPECIFIED
                )


def test_large_real_endpoint_full_graph_match_has_a_hard_timeout() -> None:
    molecule = _endpoint_molecules(LARGE_ENDPOINTS)[0]
    started_at = monotonic()

    with pytest.raises(MolecularGraphMatchTimeoutError) as error:
        get_substruct_matches(
            molecule,
            molecule,
            hard_timeout_for_large_molecules=True,
            timeout_seconds=0.000001,
        )

    assert error.value.target_atom_count == 102
    assert error.value.query_atom_count == 102
    assert monotonic() - started_at < 10.0
