import asyncio
import gzip
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from molgr import interface as molgr_interface
from molop import molopconfig
from rdkit.Chem import rdChemReactions

from tricycle_reaction_db.application.dtos import ArtifactUploadResult
from tricycle_reaction_db.application.services import transition_state_uploads as upload_module
from tricycle_reaction_db.application.services.authorization import AuthorizationService
from tricycle_reaction_db.application.services.transition_state_uploads import (
    ArtifactUploadError,
    ArtifactUploadLimitError,
    ArtifactUploadPayload,
    ArtifactUploadService,
    _FailedInference,
    _parse_calculation_output,
    _parse_calculation_outputs_batch,
    _parser_payload,
    _run_molop_parser,
    _SuccessfulInference,
)
from tricycle_reaction_db.core.config import Settings
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactKind,
    StorageStatus,
)
from tricycle_reaction_db.domain.identity import DEVELOPMENT_USER_ID, SYSTEM_PROJECT_ID

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "da_bench_minimal"
TS_FIXTURE = (
    FIXTURE_ROOT / "complete_set/000000000000_000000403256/00/ts/"
    "000000000000_000000403256_00_conf_01_ts.43b3faa8fcc9.log.gz"
)
NON_TS_FIXTURE = (
    FIXTURE_ROOT / "complete_set/000000000000_000000403256/00/prod/"
    "000000000000_000000403256_00_00.prod.log.gz"
)


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


def test_batch_parser_calls_molop_once_and_restores_input_order(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_auto_parser(paths: list[str], **options: object) -> object:
        calls.append((paths, options))
        assert [Path(path).read_bytes() for path in paths] == [b"first", b"second"]
        return SimpleNamespace(
            outcomes=(
                SimpleNamespace(
                    input_index=1,
                    succeeded=True,
                    value="parsed-second",
                    failure=None,
                    status="ok",
                ),
                SimpleNamespace(
                    input_index=0,
                    succeeded=True,
                    value="parsed-first",
                    failure=None,
                    status="ok",
                ),
            )
        )

    monkeypatch.setattr(
        "tricycle_reaction_db.application.services.transition_state_uploads.AutoParser",
        fake_auto_parser,
    )
    monkeypatch.setattr(
        "tricycle_reaction_db.application.services.transition_state_uploads."
        "_parsed_artifact_from_chem_file",
        lambda chem_file, *, source_compression: (chem_file, source_compression),
    )

    parsed = _parse_calculation_outputs_batch(
        [(gzip.compress(b"first"), "first.log.gz"), (b"second", "second.out")],
        n_jobs=4,
    )

    assert parsed == {0: ("parsed-first", "gzip"), 1: ("parsed-second", None)}
    assert len(calls) == 1
    paths, options = calls[0]
    assert len(paths) == 2
    assert options["n_jobs"] == 4
    assert options["return_report"] is True
    assert options["capture_source_evidence"] is True
    assert options["release_file_content"] is True


def test_batch_parser_isolates_invalid_gzip_before_molop(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_auto_parser(paths: list[str], **_: object) -> object:
        assert len(paths) == 1
        assert Path(paths[0]).read_bytes() == b"valid"
        return SimpleNamespace(
            outcomes=(
                SimpleNamespace(
                    input_index=0,
                    succeeded=False,
                    value=None,
                    failure=SimpleNamespace(message="isolated MolOP failure"),
                    status="error",
                ),
            )
        )

    monkeypatch.setattr(
        "tricycle_reaction_db.application.services.transition_state_uploads.AutoParser",
        fake_auto_parser,
    )

    parsed = _parse_calculation_outputs_batch(
        [(b"not-gzip", "broken.log.gz"), (b"valid", "valid.log")],
        n_jobs=2,
    )

    assert isinstance(parsed[0], ArtifactUploadError)
    assert str(parsed[0]) == "uploaded gzip artifact is invalid"
    assert isinstance(parsed[1], ArtifactUploadError)
    assert str(parsed[1]) == "isolated MolOP failure"


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
