import asyncio
import gzip
import threading
import time
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from molgr import interface as molgr_interface
from molop import molopconfig
from rdkit.Chem import rdChemReactions

from tricycle_reaction_db.application.dtos import ArtifactUploadResult
from tricycle_reaction_db.application.services import artifact_uploads as upload_module
from tricycle_reaction_db.application.services.artifact_uploads import (
    ArtifactUploadError,
    ArtifactUploadLimitError,
    ArtifactUploadPayload,
    ArtifactUploadService,
    _FailedInference,
    _parse_calculation_output,
    _parse_calculation_outputs_batch,
    _parser_payload,
    _require_batch_upload_budget,
    _run_molop_parser,
    _run_molop_parser_with_progress,
    _SuccessfulInference,
)
from tricycle_reaction_db.application.services.authorization import AuthorizationService
from tricycle_reaction_db.core.config import Settings
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactKind,
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


def test_reconstruction_failure_persists_suspicious_topology(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    molopconfig.show_progress_bar = False

    def fail_reconstruction(*_: object, **__: object) -> object:
        raise RuntimeError("forced reconstruction failure")

    monkeypatch.setattr(
        molgr_interface.core.pipeline.reconstruct_with_metals,
        "xyz2omol",
        fail_reconstruction,
    )
    parsed = _parse_calculation_output(NON_TS_FIXTURE.read_bytes(), NON_TS_FIXTURE.name)

    assert parsed.source_frame_count == len(parsed.frame_records) > 0
    assert all(
        frame.topology_reconstruction_status == "suspicious_fallback" for frame in parsed.chem_file
    )
    assert all(
        record.frame.parse_presence["topology"] == "parse_failed" for record in parsed.frame_records
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


def test_suspicious_ts_topology_is_not_used_for_reaction_inference(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    molopconfig.show_progress_bar = False

    def fail_reconstruction(*_: object, **__: object) -> object:
        raise RuntimeError("forced reconstruction failure")

    monkeypatch.setattr(
        molgr_interface.core.pipeline.reconstruct_with_metals,
        "xyz2omol",
        fail_reconstruction,
    )
    parsed = _parse_calculation_output(TS_FIXTURE.read_bytes(), TS_FIXTURE.name)

    assert len(parsed.inferences) == 1
    inference = parsed.inferences[0]
    assert isinstance(inference, _FailedInference)
    assert inference.error_code == "ts_topology_untrusted"
    assert all(
        record.molecule.topology_derivation.reconstruction_metadata["molgr_status"]
        == "suspicious_fallback"
        for record in parsed.frame_records
    )


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


def test_molop_parse_semaphore_enforces_process_level_slot_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, molop_parse_slots=1)
    lock = threading.Lock()
    active = 0
    peak = 0

    def blocking_parser(value: int) -> int:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return value

    async def run() -> list[int]:
        return await asyncio.gather(
            *(_run_molop_parser(blocking_parser, index) for index in range(4))
        )

    monkeypatch.setattr(upload_module, "get_settings", lambda: settings)
    assert asyncio.run(run()) == [0, 1, 2, 3]
    assert peak == 1


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
