import asyncio
import gzip
import threading
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from molgr.config import CONFIG as molgr_config
from molop import molopconfig
from molop.io.base_models import Molecule as molop_molecule_module
from rdkit import Chem
from rdkit.Chem import AllChem, rdChemReactions, rdDepictor

from tricycle_reaction_db.application.dtos import ArtifactUploadResult
from tricycle_reaction_db.application.services import artifact_uploads as upload_module
from tricycle_reaction_db.application.services.artifact_uploads import (
    ArtifactUploadError,
    ArtifactUploadLimitError,
    ArtifactUploadPayload,
    ArtifactUploadService,
    MolOPFileParseTimeoutError,
    _await_cancellation_safe,
    _FailedInference,
    _fast_molop_ingestion_enabled,
    _materialize_parsed_artifacts,
    _molop_file_parse_timeout_seconds,
    _parse_calculation_output,
    _parse_calculation_outputs_batch,
    _parser_payload,
    _pipeline_task_lifecycle,
    _prepare_calculation_parser_path,
    _require_batch_upload_budget,
    _run_molop_file_parser,
    _run_molop_file_pipeline,
    _run_molop_parser_with_progress,
    _SuccessfulInference,
)
from tricycle_reaction_db.application.services.authorization import AuthorizationService
from tricycle_reaction_db.core.config import Settings
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactKind,
    FrameRole,
    StorageStatus,
)
from tricycle_reaction_db.domain.identity import DEVELOPMENT_USER_ID, SYSTEM_PROJECT_ID
from tricycle_reaction_db.storage.rustfs import RustFSSettings

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "da_bench_minimal"
TS_FIXTURE = (
    FIXTURE_ROOT / "complete_set/000000000000_000000403256/00/ts/"
    "000000000000_000000403256_00_conf_01_ts.43b3faa8fcc9.log.gz"
)
NON_TS_FIXTURE = (
    FIXTURE_ROOT / "complete_set/000000000000_000000403256/00/prod/"
    "000000000000_000000403256_00_00.prod.log.gz"
)


@pytest.fixture(autouse=True)
def close_parser_pool_after_test() -> None:
    """Keep MolOP's native child-process guard isolated between test cases."""

    yield
    asyncio.run(upload_module.close_molop_process_pool())


def test_molop_infers_one_reaction_for_each_detected_ts_frame() -> None:
    molopconfig.show_progress_bar = False
    parsed = _parse_calculation_output(TS_FIXTURE.read_bytes(), TS_FIXTURE.name)

    assert (
        parsed.chem_file.parser_provenance.effective_config["molgr"]["interface"][
            "reconstruction_failure_policy"
        ]
        == "return_suspicious"
    )
    assert molgr_config.cpp_backend.max_threads == 1
    assert molgr_config.cpp_backend.enable_target_bucket_parallelism is False
    assert molgr_config.cpp_backend.enable_candidate_scoring_parallelism is False
    assert parsed.source_frame_count == 23
    assert parsed.source_format == "g16log"
    assert len(parsed.inferences) == 1
    inferred = parsed.inferences[0]
    assert isinstance(inferred, _SuccessfulInference)
    assert not isinstance(inferred, _FailedInference)
    assert inferred.file_frame_index == 22
    assert inferred.imaginary_mode_index == 0
    assert inferred.imaginary_frequency_cm1 == -550.7677

    reaction = rdChemReactions.ReactionFromSmarts(inferred.reaction_smiles, useSmiles=True)
    assert reaction is not None
    assert reaction.GetNumReactantTemplates() == 2
    assert reaction.GetNumProductTemplates() == 1
    reactant_maps = {
        atom.GetAtomMapNum() for template in reaction.GetReactants() for atom in template.GetAtoms()
    }
    product_maps = {
        atom.GetAtomMapNum() for template in reaction.GetProducts() for atom in template.GetAtoms()
    }
    assert reactant_maps == product_maps == set(range(1, 22))


def test_non_ts_calculation_frames_do_not_infer_reactions() -> None:
    molopconfig.show_progress_bar = False
    parsed = _parse_calculation_output(
        gzip.compress(gzip.decompress(NON_TS_FIXTURE.read_bytes())),
        NON_TS_FIXTURE.name,
    )

    assert parsed.source_frame_count > 0
    assert parsed.inferences == ()


def test_frame_conversion_failure_keeps_other_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed frame is diagnosed without discarding valid frames."""

    class FakeFrame:
        def __init__(self, index: int) -> None:
            self.file_frame_index = index

    class FakeChemFile:
        schema_version = "test-schema"
        source_format = "other"
        source_segments: tuple[object, ...] = ()

        def __init__(self) -> None:
            self.frames = [FakeFrame(index) for index in range(3)]

        def __iter__(self):
            return iter(self.frames)

        def __len__(self) -> int:
            return len(self.frames)

    def convert(frame: FakeFrame, **_: object) -> object:
        if frame.file_frame_index == 1:
            raise ValueError("malformed frame")
        return SimpleNamespace(
            segment_index=0,
            frame=SimpleNamespace(file_frame_index=frame.file_frame_index),
        )

    monkeypatch.setattr(upload_module, "frame_records_from_molop", convert)
    parsed = upload_module._parsed_artifact_from_chem_file(
        FakeChemFile(),
        source_compression=None,
    )

    assert len(parsed.frame_records) == 2
    assert parsed.source_frame_count == 3
    assert parsed.parse_diagnostics == (
        {
            "code": "frame_parse_failed",
            "stage": "conversion",
            "segment_index": 0,
            "file_frame_index": 1,
            "error_type": "ValueError",
            "message": "malformed frame",
        },
    )


def test_source_evidence_does_not_disable_fast_ingestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, molop_capture_source_evidence=True)
    monkeypatch.setattr(upload_module, "get_settings", lambda: settings)
    assert settings.molop_capture_source_evidence
    assert _fast_molop_ingestion_enabled()


def test_storage_pool_does_not_recycle_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid Python 3.12's max_tasks_per_child rollover deadlock for uploads."""

    created: list[dict[str, object]] = []

    class FakePool:
        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

        def shutdown(self, **_: object) -> None:
            return None

    monkeypatch.setattr(upload_module, "ProcessPoolExecutor", FakePool)
    monkeypatch.setattr(upload_module, "_storage_process_pool", None)
    monkeypatch.setattr(upload_module, "_storage_process_pool_workers", None)
    monkeypatch.setattr(upload_module, "_storage_process_pool_pid", None)

    upload_module._get_storage_process_pool(2)

    assert len(created) == 1
    assert "max_tasks_per_child" not in created[0]


@pytest.mark.asyncio
async def test_file_pipeline_timeout_isolated_to_one_file(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, molop_file_parse_timeout_seconds=0.01)
    monkeypatch.setattr(upload_module, "get_settings", lambda: settings)

    async def slow_file(*_: object, **__: object) -> object:
        await asyncio.sleep(1)
        return None

    monkeypatch.setattr(upload_module, "_run_isolated_molop_file", slow_file)
    with pytest.raises(MolOPFileParseTimeoutError, match="exceeded 0.01s"):
        await _run_molop_file_pipeline(b"source", "slow.log")


@pytest.mark.parametrize(
    ("size_bytes", "expected_seconds"),
    [
        (5 * 1024 * 1024, 60.0),
        (10 * 1024 * 1024, 60.0),
        (20 * 1024 * 1024, 120.0),
    ],
)
def test_file_parse_timeout_scales_with_source_size(
    monkeypatch: pytest.MonkeyPatch,
    size_bytes: int,
    expected_seconds: float,
) -> None:
    settings = Settings(_env_file=None, molop_file_parse_timeout_seconds=60.0)
    monkeypatch.setattr(upload_module, "get_settings", lambda: settings)

    assert _molop_file_parse_timeout_seconds(b"0" * size_bytes) == expected_seconds


def test_file_parse_timeout_scales_path_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = Settings(_env_file=None, molop_file_parse_timeout_seconds=60.0)
    monkeypatch.setattr(upload_module, "get_settings", lambda: settings)
    source = tmp_path / "large.log"
    source.write_bytes(b"0" * (30 * 1024 * 1024))

    assert _molop_file_parse_timeout_seconds(source) == 180.0


@pytest.mark.asyncio
async def test_file_pipeline_timeout_releases_slot_for_next_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, molop_file_parse_timeout_seconds=0.01)
    monkeypatch.setattr(upload_module, "get_settings", lambda: settings)
    calls: list[str] = []

    async def fake_file(_: object, filename: str, **__: object) -> object:
        calls.append(filename)
        if filename == "slow.log":
            await asyncio.sleep(1)
        return f"parsed:{filename}"

    monkeypatch.setattr(upload_module, "_run_isolated_molop_file", fake_file)
    file_slots = asyncio.Semaphore(1)
    slow_task = asyncio.create_task(
        _run_molop_file_pipeline(b"slow", "slow.log", file_slots=file_slots)
    )
    await asyncio.sleep(0)
    fast_task = asyncio.create_task(
        _run_molop_file_pipeline(b"fast", "next.log", file_slots=file_slots)
    )
    slow_result, fast_result = await asyncio.gather(slow_task, fast_task, return_exceptions=True)

    assert isinstance(slow_result, MolOPFileParseTimeoutError)
    assert fast_result == "parsed:next.log"
    assert calls == ["slow.log", "next.log"]


@pytest.mark.asyncio
async def test_file_timeout_does_not_shutdown_shared_molop_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, molop_file_parse_timeout_seconds=0.01)
    monkeypatch.setattr(upload_module, "get_settings", lambda: settings)
    shared_pool = object()
    shutdown_calls: list[object] = []

    async def slow_file(*_: object, **__: object) -> object:
        await asyncio.sleep(1)
        return None

    monkeypatch.setattr(upload_module, "_run_isolated_molop_file", slow_file)
    monkeypatch.setattr(
        upload_module,
        "_shutdown_molop_process_pool_sync",
        lambda: shutdown_calls.append(shared_pool),
    )

    with pytest.raises(MolOPFileParseTimeoutError):
        await _run_molop_file_pipeline(b"source", "slow.log")

    assert shutdown_calls == []


@pytest.mark.asyncio
async def test_batch_startup_failure_recovers_committed_reservations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, max_batch_files=1, max_batch_bytes=1024)
    monkeypatch.setattr(upload_module, "get_settings", lambda: settings)

    async def allow_upload(*_: object) -> None:
        return None

    monkeypatch.setattr(AuthorizationService, "require_project_permission", allow_upload)

    async def fake_prepare_batch(
        cls: object,
        **_: object,
    ) -> tuple[dict[int, object], dict[int, object]]:
        return {0: object()}, {}

    monkeypatch.setattr(
        ArtifactUploadService,
        "_prepare_upload_batch",
        classmethod(fake_prepare_batch),
    )

    async def recover(**values: object) -> None:
        recovered.append(values["error"])

    recovered: list[BaseException] = []
    monkeypatch.setattr(upload_module, "_recover_aborted_batch", recover)

    def fail_storage_pool(*_: object, **__: object) -> object:
        raise RuntimeError("storage pool startup failed")

    monkeypatch.setattr(upload_module, "_get_storage_process_pool", fail_storage_pool)
    with pytest.raises(RuntimeError, match="storage pool startup failed"):
        await ArtifactUploadService.upload_batch(
            files=[ArtifactUploadPayload("one.log", "text/plain", b"one")],
            artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
            project_id=SYSTEM_PROJECT_ID,
            user_id=DEVELOPMENT_USER_ID,
        )

    assert len(recovered) == 1
    assert str(recovered[0]) == "storage pool startup failed"


@pytest.mark.asyncio
async def test_pipeline_task_lifecycle_cancels_files_when_batch_fails() -> None:
    started = asyncio.Event()

    async def pending_file() -> None:
        started.set()
        await asyncio.Future()

    task = asyncio.create_task(pending_file())
    await started.wait()
    with pytest.raises(RuntimeError, match="persistence failed"):
        async with _pipeline_task_lifecycle([task]):
            raise RuntimeError("persistence failed")
    assert task.cancelled()


@pytest.mark.asyncio
async def test_pipeline_task_lifecycle_runs_abort_recovery_before_reraising() -> None:
    recovered: list[BaseException] = []

    async def recover(error: BaseException) -> None:
        recovered.append(error)

    with pytest.raises(RuntimeError, match="persistence failed"):
        async with _pipeline_task_lifecycle([], on_abort=recover):
            raise RuntimeError("persistence failed")

    assert len(recovered) == 1
    assert isinstance(recovered[0], RuntimeError)


@pytest.mark.asyncio
async def test_pipeline_task_lifecycle_recovers_before_propagating_cancellation() -> None:
    recovered: list[BaseException] = []

    async def recover(error: BaseException) -> None:
        await asyncio.sleep(0)
        recovered.append(error)

    with pytest.raises(asyncio.CancelledError):
        async with _pipeline_task_lifecycle([], on_abort=recover):
            raise asyncio.CancelledError

    assert len(recovered) == 1
    assert isinstance(recovered[0], asyncio.CancelledError)


@pytest.mark.asyncio
async def test_cancellation_safe_wait_drains_external_operation() -> None:
    finished = asyncio.Event()

    async def external_operation() -> str:
        await asyncio.sleep(0.01)
        finished.set()
        return "done"

    task = asyncio.create_task(_await_cancellation_safe(external_operation()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_file_pipeline_runs_parse_and_conversion_in_file_worker() -> None:
    parsed = await upload_module._run_isolated_molop_file(
        TS_FIXTURE.read_bytes(),
        TS_FIXTURE.name,
    )
    assert parsed.source_frame_count == 23
    assert len(parsed.frame_records) == 23
    assert len(parsed.inferences) == 1


def test_fast_molop_parse_defers_topology_reconstruction_until_materialization() -> None:
    molopconfig.show_progress_bar = False
    parsed = asyncio.run(_run_molop_file_parser(TS_FIXTURE.read_bytes(), TS_FIXTURE.name))

    assert parsed.source_frame_count == 23
    assert parsed.frame_records == ()
    assert parsed.inferences == ()
    assert parsed.chem_file.source_segments
    assert parsed.chem_file[0].frame_role == FrameRole.INITIAL.value
    assert parsed.chem_file[9].frame_role == FrameRole.TERMINAL.value
    assert all(frame.topology_reconstruction_status is None for frame in parsed.chem_file)

    materialized = _materialize_parsed_artifacts([parsed])[0]
    assert len(materialized.frame_records) == 23
    assert len(materialized.inferences) == 1
    assert materialized.frame_records[0].frame.frame_role is FrameRole.INITIAL
    assert materialized.frame_records[9].frame.frame_role is FrameRole.TERMINAL
    assert materialized.frame_records[22].frame.frame_role is FrameRole.TERMINAL
    assert all(
        frame.topology_reconstruction_status in {"succeeded", "suspicious_fallback"}
        for frame in materialized.chem_file
    )


def test_reconstruction_failure_persists_suspicious_topology(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    molopconfig.show_progress_bar = False
    reconstruct = molop_molecule_module.xyz_to_rdmol

    def suspicious_reconstruction(*args: object, **kwargs: object) -> object:
        molecule = reconstruct(*args, **kwargs)
        assert molecule is not None
        molecule.SetProp("_MolGRReconstructionStatus", "suspicious_fallback")
        molecule.SetProp(
            "_MolGRReconstructionDiagnostics",
            '{"message":"forced suspicious fallback"}',
        )
        return molecule

    monkeypatch.setattr(
        molop_molecule_module,
        "xyz_to_rdmol",
        suspicious_reconstruction,
    )
    parsed = _parse_calculation_output(NON_TS_FIXTURE.read_bytes(), NON_TS_FIXTURE.name)

    assert parsed.source_frame_count == len(parsed.frame_records) > 0
    assert all(
        frame.topology_reconstruction_status == "suspicious_fallback" for frame in parsed.chem_file
    )
    assert all(
        record.molecule.topology_derivation.reconstruction_metadata["molgr_status"]
        == "suspicious_fallback"
        for record in parsed.frame_records
    )
    assert all(
        record.molecule.formula.atom_count
        == record.molecule.topology.atom_count
        == record.molecule.geometry.atom_count
        for record in parsed.frame_records
    )


def test_suspicious_ts_frame_still_attempts_reaction_inference(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    molopconfig.show_progress_bar = False
    parsed = _parse_calculation_output(TS_FIXTURE.read_bytes(), TS_FIXTURE.name)
    frame = parsed.chem_file[-1]
    frame.topology_reconstruction_status = "suspicious_fallback"
    signed_endpoints = upload_module._signed_ts_endpoints

    def trusted_endpoints(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        negative, positive, negative_ratio, positive_ratio = signed_endpoints(*args, **kwargs)
        for endpoint in (negative, positive):
            if endpoint.HasProp("_MolGRReconstructionStatus"):
                endpoint.ClearProp("_MolGRReconstructionStatus")
        return negative, positive, negative_ratio, positive_ratio

    monkeypatch.setattr(upload_module, "_signed_ts_endpoints", trusted_endpoints)
    inference = upload_module._infer_ts_frame(frame, fallback_index=22)

    assert isinstance(inference, _SuccessfulInference)


def test_suspicious_ts_endpoint_is_not_used_for_reaction_inference(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    molopconfig.show_progress_bar = False
    parsed = _parse_calculation_output(TS_FIXTURE.read_bytes(), TS_FIXTURE.name)
    frame = parsed.chem_file[-1]
    signed_endpoints = upload_module._signed_ts_endpoints

    def suspicious_endpoints(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        negative, positive, negative_ratio, positive_ratio = signed_endpoints(*args, **kwargs)
        negative.SetProp("_MolGRReconstructionStatus", "suspicious_fallback")
        return negative, positive, negative_ratio, positive_ratio

    monkeypatch.setattr(upload_module, "_signed_ts_endpoints", suspicious_endpoints)
    inference = upload_module._infer_ts_frame(frame, fallback_index=22)

    assert isinstance(inference, _FailedInference)
    assert inference.error_code == "ts_topology_untrusted"


def test_inferred_endpoint_repairs_stereo_before_fragment_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assigned = Chem.AddHs(Chem.MolFromSmiles("F/C=C/F.[Na+]"))
    rdDepictor.Compute2DCoords(assigned)
    endpoint = Chem.MolFromMolBlock(
        Chem.MolToMolBlock(assigned),
        sanitize=False,
        removeHs=False,
        strictParsing=True,
    )
    assert endpoint is not None
    for assigned_bond, endpoint_bond in zip(
        assigned.GetBonds(), endpoint.GetBonds(), strict=True
    ):
        endpoint_bond.SetStereo(assigned_bond.GetStereo())
        stereo_atoms = list(assigned_bond.GetStereoAtoms())
        if len(stereo_atoms) == 2:
            endpoint_bond.SetStereoAtoms(*stereo_atoms)
        endpoint_bond.SetBondDir(Chem.BondDir.NONE)

    incomplete_smiles = Chem.MolToSmiles(
        endpoint,
        canonical=True,
        isomericSmiles=True,
        allHsExplicit=True,
    )
    assert "/" not in incomplete_smiles and "\\" not in incomplete_smiles
    assert endpoint.GetNumConformers() == 1

    repair_inputs: list[tuple[int, int]] = []
    repair_stereo = upload_module.ensure_serializable_double_bond_stereochemistry

    def record_repair_input(molecule: Chem.Mol, **kwargs: object) -> Chem.Mol:
        repair_inputs.append((len(Chem.GetMolFrags(molecule)), molecule.GetNumConformers()))
        return repair_stereo(molecule, **kwargs)

    monkeypatch.setattr(
        upload_module,
        "ensure_serializable_double_bond_stereochemistry",
        record_repair_input,
    )

    inferred = _SuccessfulInference(
        file_frame_index=0,
        imaginary_mode_index=0,
        imaginary_frequency_cm1=-100.0,
        reaction_smiles="test",
        negative_endpoint=endpoint,
        positive_endpoint=Chem.Mol(endpoint),
        negative_displacement_ratio=1.0,
        positive_displacement_ratio=1.0,
        charge=1,
        multiplicity=1,
    )
    records = upload_module._inference_topology_records(inferred)

    assert repair_inputs[:2] == [(2, 1), (2, 1)]
    assert repair_inputs[2:] == [(1, 0)] * 4
    endpoint_smiles = [record.topology.canonical_isomeric_smiles for record in records[:2]]
    participant_smiles = [
        record.topology.canonical_isomeric_smiles
        for record in records[2:]
        if record.topology.atom_count > 1
    ]
    assert all(smiles is not None for smiles in endpoint_smiles + participant_smiles)
    assert all(
        "/" in smiles or "\\" in smiles
        for smiles in endpoint_smiles + participant_smiles
        if smiles is not None
    )


def test_mapped_endpoint_reaction_repairs_ez_after_fragment_extraction() -> None:
    endpoint = Chem.AddHs(Chem.MolFromSmiles("C/C=C/C.[Na+]"))
    for bond in endpoint.GetBonds():
        bond.SetBondDir(Chem.BondDir.NONE)

    reaction_smiles = upload_module._mapped_reaction_smiles(endpoint, Chem.Mol(endpoint))
    reactants, products = reaction_smiles.split(">>")

    assert "/" in reactants or "\\" in reactants
    assert "/" in products or "\\" in products
    for side in (reactants, products):
        parsed = Chem.MolFromSmiles(side)
        assert parsed is not None
        double_bond = next(
            bond for bond in parsed.GetBonds() if bond.GetBondType() == Chem.BondType.DOUBLE
        )
        assert double_bond.GetStereo() == Chem.BondStereo.STEREOE


def test_ts_endpoint_stereo_is_inferred_from_endpoint_3d_coordinates() -> None:
    endpoint = Chem.AddHs(Chem.MolFromSmiles("FC=CF"))
    assert AllChem.EmbedMolecule(endpoint, randomSeed=7) == 0
    for bond in endpoint.GetBonds():
        bond.SetStereo(Chem.BondStereo.STEREONONE)
        bond.SetBondDir(Chem.BondDir.NONE)

    inferred = upload_module._infer_endpoint_stereochemistry_from_3d(endpoint)
    double_bond = next(
        bond for bond in inferred.GetBonds() if bond.GetBondType() == Chem.BondType.DOUBLE
    )
    serialized = Chem.MolToSmiles(
        inferred,
        canonical=True,
        isomericSmiles=True,
        allHsExplicit=True,
    )

    assert double_bond.GetStereo() in {
        Chem.BondStereo.STEREOE,
        Chem.BondStereo.STEREOZ,
    }
    assert "/" in serialized or "\\" in serialized


def test_gzip_parser_payload_keeps_logical_source_identity_separate() -> None:
    source = b"logical QM source\n"
    compressed = gzip.compress(source)

    assert _parser_payload(source, "calculation.log") == (source, None)
    assert _parser_payload(compressed, "calculation.log.gz") == (source, "gzip")


def test_parser_payload_enforces_compressed_and_decompressed_limits() -> None:
    with pytest.raises(ArtifactUploadError, match="exceeds"):
        _parser_payload(b"0123456789", "calculation.log", max_decompressed_bytes=4)

    compressed = gzip.compress(b"x" * 1024)
    assert len(compressed) < 128
    with pytest.raises(ArtifactUploadError, match="decompressed artifact exceeds"):
        _parser_payload(compressed, "calculation.log.gz", max_decompressed_bytes=128)


def test_serial_batch_parser_dispatches_files_and_restores_input_order(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[list[bytes], list[str | None], int]] = []

    def fake_parallel_parser(
        paths: list[str],
        compressions: list[str | None],
        *,
        n_jobs: int,
    ) -> list[tuple[object | None, str | None]]:
        calls.append(([Path(path).read_bytes() for path in paths], compressions, n_jobs))
        assert [Path(path).read_bytes() for path in paths] == [b"first", b"second"]
        return [("parsed-first", None), ("parsed-second", None)]

    monkeypatch.setattr(upload_module, "_parse_calculation_paths_parallel", fake_parallel_parser)
    parsed = _parse_calculation_outputs_batch(
        [(gzip.compress(b"first"), "first.log.gz"), (b"second", "second.out")],
        n_jobs=1,
    )

    assert parsed == {0: "parsed-first", 1: "parsed-second"}
    assert calls == [([b"first", b"second"], ["gzip", None], 1)]


def test_parallel_batch_parser_dispatches_each_file_with_source_evidence(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[list[bytes], list[str | None], int]] = []

    def fake_parallel_parser(
        paths: list[str],
        compressions: list[str | None],
        *,
        n_jobs: int,
    ) -> list[tuple[object | None, str | None]]:
        calls.append(([Path(path).read_bytes() for path in paths], compressions, n_jobs))
        return [("parsed-first", None), (None, "isolated MolOP failure")]

    monkeypatch.setattr(upload_module, "_parse_calculation_paths_parallel", fake_parallel_parser)

    parsed = _parse_calculation_outputs_batch(
        [(gzip.compress(b"first"), "first.log.gz"), (b"second", "second.out")],
        n_jobs=4,
    )

    assert parsed[0] == "parsed-first"
    assert isinstance(parsed[1], ArtifactUploadError)
    assert str(parsed[1]) == "isolated MolOP failure"
    assert calls == [([b"first", b"second"], ["gzip", None], 4)]


def test_batch_parser_isolates_invalid_gzip_before_molop(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_parallel_parser(
        paths: list[str],
        compressions: list[str | None],
        *,
        n_jobs: int,
    ) -> list[tuple[object | None, str | None]]:
        assert len(paths) == 1
        assert Path(paths[0]).read_bytes() == b"valid"
        assert compressions == [None]
        assert n_jobs == 2
        return [(None, "isolated MolOP failure")]

    monkeypatch.setattr(upload_module, "_parse_calculation_paths_parallel", fake_parallel_parser)

    parsed = _parse_calculation_outputs_batch(
        [(b"not-gzip", "broken.log.gz"), (b"valid", "valid.log")],
        n_jobs=2,
    )

    assert isinstance(parsed[0], ArtifactUploadError)
    assert str(parsed[0]) == "uploaded gzip artifact is invalid"
    assert isinstance(parsed[1], ArtifactUploadError)
    assert str(parsed[1]) == "isolated MolOP failure"


def test_spooled_batch_budget_keeps_source_on_disk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "calculation.out"
    payload = b"spooled calculation output\n" * 1024
    source.write_bytes(payload)

    def unexpected_read_bytes(_: Path) -> bytes:
        raise AssertionError("spooled batch budget must not materialize the source")

    monkeypatch.setattr(Path, "read_bytes", unexpected_read_bytes)
    inspected = _require_batch_upload_budget(
        [ArtifactUploadPayload(source.name, "text/plain", None, spool_path=source)]
    )

    assert inspected[0].source == source
    assert inspected[0].size_bytes == len(payload)
    assert inspected[0].content_sha256 == sha256(payload).hexdigest()
    assert inspected[0].media_probe == payload[: 64 * 1024]


def test_spooled_batch_budget_rejects_gzip_bomb(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "calculation.log.gz"
    source.write_bytes(gzip.compress(b"x" * 1025))
    monkeypatch.setattr(
        upload_module,
        "get_settings",
        lambda: Settings(_env_file=None, max_upload_bytes=1024, max_batch_bytes=2048),
    )

    with pytest.raises(ArtifactUploadLimitError, match="decompressed artifact"):
        _require_batch_upload_budget(
            [ArtifactUploadPayload(source.name, "application/gzip", None, spool_path=source)]
        )


def test_streaming_spooled_budget_uses_per_file_limit_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.log"
    second = tmp_path / "second.log"
    first.write_bytes(b"a" * 700)
    second.write_bytes(b"b" * 700)
    monkeypatch.setattr(
        upload_module,
        "get_settings",
        lambda: Settings(_env_file=None, max_upload_bytes=1024, max_batch_bytes=1024),
    )

    inspected = _require_batch_upload_budget(
        [
            ArtifactUploadPayload(first.name, "text/plain", None, spool_path=first),
            ArtifactUploadPayload(second.name, "text/plain", None, spool_path=second),
        ],
        enforce_batch_bytes=False,
    )

    assert sorted(inspected) == [0, 1]
    with pytest.raises(ArtifactUploadLimitError, match="upload batch exceeds"):
        _require_batch_upload_budget(
            [
                ArtifactUploadPayload(first.name, "text/plain", None, spool_path=first),
                ArtifactUploadPayload(second.name, "text/plain", None, spool_path=second),
            ]
        )


def test_batch_parser_uses_uncompressed_spooled_source_directly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "calculation.out"
    source.write_bytes(b"direct spooled source")

    def fake_parallel_parser(
        paths: list[str],
        compressions: list[str | None],
        *,
        n_jobs: int,
    ) -> list[tuple[object | None, str | None]]:
        assert paths == [str(source)]
        assert compressions == [None]
        assert n_jobs == 2
        return [("parsed", None)]

    monkeypatch.setattr(upload_module, "_parse_calculation_paths_parallel", fake_parallel_parser)
    parsed = _parse_calculation_outputs_batch([(source, source.name)], n_jobs=2)

    assert parsed == {0: "parsed"}


def test_parser_path_restores_filename_suffix_for_opaque_spool_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "00000000.upload"
    source.write_bytes(b"spooled calculation output")
    temporary_dir = tmp_path / "prepared"
    temporary_dir.mkdir()

    parser_path, compression = _prepare_calculation_parser_path(
        source,
        "gaussian.log",
        temporary_dir=temporary_dir,
        input_index=0,
    )

    assert Path(parser_path).suffix == ".log"
    assert Path(parser_path) != source
    assert Path(parser_path).read_bytes() == source.read_bytes()
    assert compression is None


def test_parser_path_decompresses_opaque_gzip_spool_file_with_filename_suffix(
    tmp_path: Path,
) -> None:
    source = tmp_path / "00000000.upload"
    source.write_bytes(gzip.compress(b"compressed calculation output"))
    temporary_dir = tmp_path / "prepared"
    temporary_dir.mkdir()

    parser_path, compression = _prepare_calculation_parser_path(
        source,
        "gaussian.log.gz",
        temporary_dir=temporary_dir,
        input_index=0,
    )

    assert Path(parser_path).suffix == ".log"
    assert Path(parser_path).read_bytes() == b"compressed calculation output"
    assert compression == "gzip"


def test_batch_parser_decompresses_spooled_gzip_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "calculation.log.gz"
    source.write_bytes(gzip.compress(b"decoded spooled source"))

    def fake_parallel_parser(
        paths: list[str],
        compressions: list[str | None],
        *,
        n_jobs: int,
    ) -> list[tuple[object | None, str | None]]:
        assert [Path(path).read_bytes() for path in paths] == [b"decoded spooled source"]
        assert compressions == ["gzip"]
        assert n_jobs == 2
        return [("parsed", None)]

    monkeypatch.setattr(upload_module, "_parse_calculation_paths_parallel", fake_parallel_parser)
    parsed = _parse_calculation_outputs_batch([(source, source.name)], n_jobs=2)

    assert parsed == {0: "parsed"}


def test_validate_probes_calculation_without_persistence(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def allow_upload(*_: object) -> None:
        return None

    monkeypatch.setattr(AuthorizationService, "require_project_permission", allow_upload)
    result = asyncio.run(
        ArtifactUploadService.validate(
            payload=TS_FIXTURE.read_bytes(),
            filename=TS_FIXTURE.name,
            project_id=SYSTEM_PROJECT_ID,
            user_id=DEVELOPMENT_USER_ID,
        )
    )

    assert result.source_format == "g16log"
    assert result.source_compression == "gzip"
    assert result.source_frame_count == 23
    assert result.transition_state_frame_count == result.successful_inference_count == 1
    assert result.failed_inference_count == 0
    assert result.inferences[0].reaction_smiles is not None


def test_batch_upload_isolates_each_file_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def allow_upload(*_: object) -> None:
        return None

    async def upload(**values: object) -> ArtifactUploadResult:
        filename = str(values["filename"])
        if filename == "invalid.log":
            raise RuntimeError("isolated parse failure")
        return ArtifactUploadResult(
            artifact_id=SYSTEM_PROJECT_ID,
            artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
            storage_status=StorageStatus.AVAILABLE,
            ingestion_status=ArtifactIngestionStatus.SUCCEEDED,
            source_frame_count=1,
            transition_state_frame_count=0,
            inferred_reaction_count=0,
            inferences=[],
        )

    monkeypatch.setattr(AuthorizationService, "require_project_permission", allow_upload)
    monkeypatch.setattr(ArtifactUploadService, "_prepare_upload", upload)
    result = asyncio.run(
        ArtifactUploadService.upload_batch(
            files=[
                ArtifactUploadPayload("gaussian.log", "text/plain", b"gaussian"),
                ArtifactUploadPayload("invalid.log", "text/plain", b"invalid"),
                ArtifactUploadPayload("orca.orcaout", "text/plain", b"orca"),
            ],
            artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
            project_id=SYSTEM_PROJECT_ID,
            user_id=DEVELOPMENT_USER_ID,
        )
    )

    assert (result.total_count, result.succeeded_count, result.failed_count) == (3, 2, 1)
    assert [item.succeeded for item in result.items] == [True, False, True]
    assert result.items[1].error_code == "artifact_upload_failed"
    assert result.items[1].error_message == "isolated parse failure"
    assert result.source_frame_count == 2


def test_new_object_upload_skips_rustfs_existence_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    uploaded = SimpleNamespace(key="new-object")

    class Store:
        def __init__(self, _settings: RustFSSettings) -> None:
            pass

        def __enter__(self) -> object:
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def ensure_bucket(self) -> None:
            calls.append("ensure_bucket")

        def exists(self, _key: str) -> bool:
            raise AssertionError("new object upload performed a redundant HEAD")

        def put_bytes(self, **_: object) -> object:
            calls.append("put_bytes")
            return uploaded

    monkeypatch.setattr(upload_module, "RustFSObjectStore", Store)

    result = ArtifactUploadService._store_payload(
        RustFSSettings(_env_file=None),
        "uploads/new-object",
        b"new payload",
        "text/plain",
        check_existing_object=False,
    )

    assert result is uploaded
    assert calls == ["ensure_bucket", "put_bytes"]


def test_retry_upload_keeps_rustfs_existence_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    existing = SimpleNamespace(key="existing-object")

    class Store:
        def __init__(self, _settings: RustFSSettings) -> None:
            pass

        def __enter__(self) -> object:
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def ensure_bucket(self) -> None:
            calls.append("ensure_bucket")

        def exists(self, _key: str) -> bool:
            calls.append("exists")
            return True

        def head(self, _key: str) -> object:
            calls.append("head")
            return existing

        def put_bytes(self, **_: object) -> object:
            raise AssertionError("retry overwrote an existing RustFS object")

    monkeypatch.setattr(upload_module, "RustFSObjectStore", Store)

    result = ArtifactUploadService._store_payload(
        RustFSSettings(_env_file=None),
        "uploads/existing-object",
        b"existing payload",
        "text/plain",
        check_existing_object=True,
    )

    assert result is existing
    assert calls == ["ensure_bucket", "exists", "head"]


@pytest.mark.parametrize(
    ("files", "settings", "message"),
    [
        (
            [
                ArtifactUploadPayload("one.log", "text/plain", b"a"),
                ArtifactUploadPayload("two.log", "text/plain", b"b"),
            ],
            Settings(_env_file=None, max_batch_files=1),
            "file limit",
        ),
        (
            [ArtifactUploadPayload("one.log", "text/plain", b"a" * 1025)],
            Settings(_env_file=None, max_upload_bytes=1024),
            "byte limit",
        ),
        (
            [
                ArtifactUploadPayload("one.log", "text/plain", b"a" * 700),
                ArtifactUploadPayload("two.log", "text/plain", b"b" * 700),
            ],
            Settings(_env_file=None, max_upload_bytes=2048, max_batch_bytes=1024),
            "byte limit",
        ),
        (
            [
                ArtifactUploadPayload(
                    "compressed.log.gz",
                    "application/gzip",
                    gzip.compress(b"expanded" * 512),
                )
            ],
            Settings(_env_file=None, max_upload_bytes=1024),
            "decompressed artifact",
        ),
    ],
)
def test_batch_service_rejects_resource_limits_before_authorization_or_storage(
    monkeypatch: pytest.MonkeyPatch,
    files: list[ArtifactUploadPayload],
    settings: Settings,
    message: str,
) -> None:
    async def unexpected_authorization(*_: object) -> None:
        raise AssertionError("resource-rejected batch reached authorization")

    async def unexpected_storage(**_: object) -> ArtifactUploadResult:
        raise AssertionError("resource-rejected batch reached storage")

    monkeypatch.setattr(upload_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        AuthorizationService,
        "require_project_permission",
        unexpected_authorization,
    )
    monkeypatch.setattr(ArtifactUploadService, "_prepare_upload", unexpected_storage)

    with pytest.raises(ArtifactUploadLimitError, match=message):
        asyncio.run(
            ArtifactUploadService.upload_batch(
                files=files,
                artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                project_id=SYSTEM_PROJECT_ID,
                user_id=DEVELOPMENT_USER_ID,
            )
        )


def test_local_streaming_pipeline_can_exceed_http_file_count_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        upload_module,
        "get_settings",
        lambda: Settings(_env_file=None, max_batch_files=1),
    )

    inspected = _require_batch_upload_budget(
        [
            ArtifactUploadPayload("one.log", "text/plain", b"one"),
            ArtifactUploadPayload("two.log", "text/plain", b"two"),
        ],
        enforce_batch_files=False,
        enforce_batch_bytes=False,
    )

    assert set(inspected) == {0, 1}


def test_molop_progress_callback_runs_before_parser_batch_finishes() -> None:
    callback_completed = threading.Event()
    events: list[str] = []

    def parser(*, progress_queue: object) -> dict[int, str]:
        progress_queue.put((0, "parsed"))  # type: ignore[attr-defined]
        assert callback_completed.wait(timeout=1)
        events.append("parser_finished")
        return {0: "parsed"}

    async def progress_callback(index: int, result: object) -> None:
        assert (index, result) == (0, "parsed")
        events.append("persisted")
        callback_completed.set()

    result = asyncio.run(
        _run_molop_parser_with_progress(
            parser,
            progress_callback=progress_callback,
        )
    )

    assert result == {0: "parsed"}
    assert events == ["persisted", "parser_finished"]
