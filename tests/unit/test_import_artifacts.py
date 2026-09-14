import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from tricycle_reaction_db.application.dtos import UploadBatchItemView, UploadBatchView
from tricycle_reaction_db.application.services.artifact_upload_types import ArtifactUploadPayload
from tricycle_reaction_db.application.services.upload_batches import (
    StagedUploadSubmission,
    UploadBatchService,
)
from tricycle_reaction_db.core.config import Settings
from tricycle_reaction_db.dev.import_artifacts import (
    ImportCandidate,
    ImportFingerprint,
    ImportMetrics,
    ImportState,
    discover_files,
    file_fingerprint,
    import_files,
    is_retryable_import_error,
    iter_batches,
)
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactKind,
    ImportMaterializationStatus,
    ImportParseStatus,
    UploadBatchItemStatus,
    UploadBatchStatus,
)


def test_discover_files_recurses_deduplicates_and_ignores_symlinks(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "a.log").write_text("a", encoding="utf-8")
    (second / "b.log").write_text("b", encoding="utf-8")
    (second / "nested").mkdir()
    (second / "nested" / "c.log").write_text("c", encoding="utf-8")
    (second / "link.log").symlink_to(first / "a.log")

    candidates = discover_files([first, second, first / "a.log"])

    assert [candidate.path.name for candidate in candidates] == ["a.log", "b.log", "c.log"]


def test_discover_files_excludes_calculation_sidecars_but_keeps_vendor_outputs(
    tmp_path: Path,
) -> None:
    (tmp_path / "source.log").write_text("log", encoding="utf-8")
    (tmp_path / "metadata.json").write_text("{}", encoding="utf-8")
    (tmp_path / "compressed-metadata.json.gz").write_bytes(b"metadata")
    (tmp_path / "index.csv").write_text("id\n", encoding="utf-8")
    (tmp_path / "vendor.output").write_text("vendor output", encoding="utf-8")

    candidates = discover_files([tmp_path], artifact_kind=ArtifactKind.CALCULATION_OUTPUT)

    assert [candidate.path.name for candidate in candidates] == ["source.log", "vendor.output"]


def test_discover_files_can_exclude_calculation_basename_globs(tmp_path: Path) -> None:
    (tmp_path / "r0_conf0_opt_xtb.out").write_text("xtb", encoding="utf-8")
    (tmp_path / "r0_sp_orca.out").write_text("orca", encoding="utf-8")
    (tmp_path / "g16.log").write_text("g16", encoding="utf-8")

    candidates = discover_files(
        [tmp_path],
        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
        exclude_name_globs=["*_XTB.OUT"],
    )

    assert [candidate.path.name for candidate in candidates] == ["g16.log", "r0_sp_orca.out"]


def test_transient_import_error_classifier_distinguishes_parser_timeout() -> None:
    assert is_retryable_import_error("psycopg.errors.OutOfMemory: max_locks_per_transaction")
    assert is_retryable_import_error("deadlock detected")
    assert not is_retryable_import_error("[molop_parse_timeout] parser exceeded its file budget")
    assert not is_retryable_import_error("NO_VALID_ORGANIC_CANDIDATE")


def test_iter_batches_obeys_file_and_byte_limits() -> None:
    candidates = [
        ImportCandidate(Path(f"file-{index}"), size, 0) for index, size in enumerate((4, 4, 7))
    ]

    batches = list(iter_batches(candidates, max_files=2, max_bytes=8))

    assert [[candidate.size_bytes for candidate in batch] for batch in batches] == [[4, 4], [7]]


def test_import_state_only_skips_unchanged_success(tmp_path: Path) -> None:
    source = tmp_path / "source.log"
    source.write_text("payload", encoding="utf-8")
    state = ImportState(tmp_path / "state.jsonl")
    candidate = discover_files([source])[0]
    fingerprint = file_fingerprint(source)
    project_id = UUID("00000000-0000-7000-8000-000000000201")

    state.append(
        {
            "source": str(source.resolve()),
            "status": "succeeded",
            "project_id": str(project_id),
            "artifact_kind": "input",
            "size_bytes": fingerprint.size_bytes,
            "mtime_ns": fingerprint.mtime_ns,
            "sha256": fingerprint.sha256,
        }
    )

    assert state.succeeded(
        candidate.path,
        project_id=project_id,
        artifact_kind=ArtifactKind.INPUT,
        fingerprint=fingerprint,
    )
    assert not state.succeeded(
        candidate.path,
        project_id=project_id,
        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
        fingerprint=fingerprint,
    )


def test_import_state_keeps_partial_ingestion_retryable(tmp_path: Path) -> None:
    source = tmp_path / "partial.log"
    source.write_text("payload", encoding="utf-8")
    state = ImportState(tmp_path / "state.jsonl")
    candidate = discover_files([source])[0]
    fingerprint = file_fingerprint(source)
    project_id = UUID("00000000-0000-0000-0000-000000000201")

    state.append(
        {
            "source": str(source.resolve()),
            "status": "succeeded",
            "ingestion_status": "partial",
            "project_id": str(project_id),
            "artifact_kind": ArtifactKind.CALCULATION_OUTPUT.value,
            "size_bytes": fingerprint.size_bytes,
            "mtime_ns": fingerprint.mtime_ns,
            "sha256": fingerprint.sha256,
        }
    )

    assert not state.terminal(
        candidate.path,
        project_id=project_id,
        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
        fingerprint=fingerprint,
    )


def test_import_state_ignores_only_an_unterminated_tail(tmp_path: Path) -> None:
    source = tmp_path / "source.log"
    source.write_text("payload", encoding="utf-8")
    fingerprint = file_fingerprint(source)
    state_path = tmp_path / "state.jsonl"
    valid_record = {
        "source": str(source.resolve()),
        "status": "succeeded",
        "project_id": "00000000-0000-7000-8000-000000000201",
        "artifact_kind": ArtifactKind.INPUT.value,
        "size_bytes": fingerprint.size_bytes,
        "mtime_ns": fingerprint.mtime_ns,
        "sha256": fingerprint.sha256,
    }
    state_path.write_text(
        json.dumps(valid_record, sort_keys=True) + '\n{"source": "truncated', encoding="utf-8"
    )

    state = ImportState(state_path)

    assert state_path.read_bytes().endswith(b"\n")
    assert state.terminal(
        source.resolve(),
        project_id=UUID("00000000-0000-7000-8000-000000000201"),
        artifact_kind=ArtifactKind.INPUT,
        fingerprint=fingerprint,
    )


_BATCH_ID = UUID("00000000-0000-7000-8000-000000000301")
_USER_ID = UUID("00000000-0000-7000-8000-000000000002")
_PROJECT_ID = UUID("00000000-0000-7000-8000-000000000201")
_BATCH_NOW = datetime(2026, 8, 20, tzinfo=UTC)


def _payload_size(payload: ArtifactUploadPayload) -> int:
    if payload.payload is not None:
        return len(payload.payload)
    if payload.spool_path is not None:
        return payload.spool_path.stat().st_size
    return 0


def _staged_submission(
    payloads: list[ArtifactUploadPayload],
    *,
    failed_names: set[str] | None = None,
    filtered_names: set[str] | None = None,
) -> StagedUploadSubmission:
    failed_names = failed_names or set()
    filtered_names = filtered_names or set()
    items: list[UploadBatchItemView] = []
    staged_count = 0
    failed_count = 0
    for position, payload in enumerate(payloads):
        filename = payload.filename
        artifact_id = UUID(int=0x400 + position) if filename not in failed_names else None
        if filename in filtered_names:
            status = UploadBatchItemStatus.FAILED
            parse_status = ImportParseStatus.FILTERED
            materialization_status = ImportMaterializationStatus.SUCCEEDED
            ingestion_status = ArtifactIngestionStatus.FILTERED
            error_code = "no_calculation_frames"
            error_message = "source contains no QM calculation frames; artifact was filtered"
            failed_count += 1
        elif filename in failed_names:
            status = UploadBatchItemStatus.FAILED
            parse_status = ImportParseStatus.FAILED
            materialization_status = ImportMaterializationStatus.FAILED
            ingestion_status = ArtifactIngestionStatus.FAILED
            error_code = "artifact_stage_failed"
            error_message = "RustFS staging failed"
            failed_count += 1
        else:
            status = UploadBatchItemStatus.STAGED
            parse_status = ImportParseStatus.PENDING
            materialization_status = ImportMaterializationStatus.SUCCEEDED
            ingestion_status = ArtifactIngestionStatus.PENDING
            error_code = None
            error_message = None
            staged_count += 1
        items.append(
            UploadBatchItemView(
                id=UUID(int=0x500 + position),
                batch_id=_BATCH_ID,
                created_at=_BATCH_NOW,
                updated_at=_BATCH_NOW,
                client_file_id=UUID(int=0x600 + position),
                position=position,
                original_filename=filename,
                relative_path=payload.relative_path or filename,
                size_bytes=_payload_size(payload),
                media_type=payload.media_type,
                status=status,
                attempt_count=1,
                content_sha256=None,
                parse_status=parse_status,
                materialization_status=materialization_status,
                artifact_file_id=artifact_id,
                ingestion_status=ingestion_status,
                error_code=error_code,
                error_message=error_message,
                metadata={},
            )
        )
    batch = UploadBatchView(
        id=_BATCH_ID,
        created_at=_BATCH_NOW,
        updated_at=_BATCH_NOW,
        project_id=_PROJECT_ID,
        created_by_user_id=_USER_ID,
        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
        status=UploadBatchStatus.ACTIVE,
        shared_metadata={},
        total_count=len(items),
        total_bytes=sum(item.size_bytes for item in items),
        succeeded_count=0,
        failed_count=failed_count,
        cancelled_count=0,
        uploading_count=0,
        staged_count=staged_count,
        processing_count=0,
    )
    return StagedUploadSubmission(batch=batch, items=tuple(items))


def test_import_files_stages_local_paths_in_the_durable_queue(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "archive" / "nested" / "input.dat"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"payload")
    calls: list[list[ArtifactUploadPayload]] = []

    async def create_and_stage(**kwargs: object) -> StagedUploadSubmission:
        payloads = cast(list[ArtifactUploadPayload], kwargs["files"])
        assert kwargs["artifact_kind"] is ArtifactKind.INPUT
        assert kwargs["project_id"] == _PROJECT_ID
        assert kwargs["user_id"] == _USER_ID
        calls.append(payloads)
        return _staged_submission(payloads)

    monkeypatch.setattr(UploadBatchService, "create_and_stage", staticmethod(create_and_stage))
    monkeypatch.setattr(
        "tricycle_reaction_db.dev.import_artifacts.get_settings",
        lambda: Settings.model_validate({"max_batch_files": 10, "max_batch_bytes": 1024}),
    )
    metrics = ImportMetrics()
    summary = asyncio.run(
        import_files(
            discover_files([source]),
            project_id=_PROJECT_ID,
            user_id=_USER_ID,
            artifact_kind=ArtifactKind.INPUT,
            state=ImportState(None),
            dry_run=False,
            metrics=metrics,
        )
    )

    assert summary.staged == 1
    assert summary.failed == 0
    assert len(calls) == 1
    assert calls[0][0].payload is None
    assert calls[0][0].spool_path == source.resolve()
    assert metrics.steps[0]["batch_size"] == 1
    assert metrics.step_timings_ms["fingerprint"] >= 0
    assert metrics.step_timings_ms["upload_batches"] >= 0
    assert metrics.sql_statement_count >= 0


def test_import_files_records_worker_filter_results_as_filtered(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "not-a-qm-output.log"
    source.write_text("no calculation frames", encoding="utf-8")
    state_path = tmp_path / "state.jsonl"

    async def create_and_stage(**kwargs: object) -> StagedUploadSubmission:
        payloads = cast(list[ArtifactUploadPayload], kwargs["files"])
        return _staged_submission(payloads, filtered_names={source.name})

    monkeypatch.setattr(UploadBatchService, "create_and_stage", staticmethod(create_and_stage))
    monkeypatch.setattr(
        "tricycle_reaction_db.dev.import_artifacts.get_settings",
        lambda: Settings.model_validate({"max_batch_files": 10, "max_batch_bytes": 1024}),
    )

    summary = asyncio.run(
        import_files(
            discover_files([source]),
            project_id=_PROJECT_ID,
            user_id=_USER_ID,
            artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
            state=ImportState(state_path),
            dry_run=False,
        )
    )

    record = json.loads(state_path.read_text(encoding="utf-8"))
    assert summary.filtered == 1
    assert summary.failed == 0
    assert record["status"] == "filtered"
    assert ImportState(state_path).terminal(
        source.resolve(),
        project_id=_PROJECT_ID,
        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
        fingerprint=file_fingerprint(source),
    )


def test_import_files_preserves_per_file_queue_failures(monkeypatch, tmp_path: Path) -> None:
    sources = [tmp_path / name for name in ("bad.log", "good-a.log", "good-b.log")]
    for source in sources:
        source.write_text(source.name, encoding="utf-8")
    calls: list[list[str]] = []

    async def create_and_stage(**kwargs: object) -> StagedUploadSubmission:
        payloads = cast(list[ArtifactUploadPayload], kwargs["files"])
        calls.append([payload.filename for payload in payloads])
        return _staged_submission(payloads, failed_names={"bad.log"})

    monkeypatch.setattr(UploadBatchService, "create_and_stage", staticmethod(create_and_stage))
    monkeypatch.setattr(
        "tricycle_reaction_db.dev.import_artifacts.get_settings",
        lambda: Settings(_env_file=None, max_batch_files=10, max_batch_bytes=1024),
    )
    state_path = tmp_path / "state.jsonl"

    summary = asyncio.run(
        import_files(
            discover_files(sources),
            project_id=_PROJECT_ID,
            user_id=_USER_ID,
            artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
            state=ImportState(state_path),
            dry_run=False,
        )
    )

    records = [json.loads(line) for line in state_path.read_text(encoding="utf-8").splitlines()]
    assert summary.attempted == 3
    assert summary.staged == 2
    assert summary.failed == 1
    assert calls == [["bad.log", "good-a.log", "good-b.log"]]
    assert {record["filename"]: record["status"] for record in records} == {
        "bad.log": "failed",
        "good-a.log": "staged",
        "good-b.log": "staged",
    }


def test_import_files_uses_queue_windows_and_not_parser_persistence_options(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    sources = []
    for index in range(17):
        source = tmp_path / f"file-{index:02d}.log"
        source.write_bytes(source.name.encode())
        sources.append(source)
    calls: list[list[str]] = []

    async def create_and_stage(**kwargs: object) -> StagedUploadSubmission:
        payloads = cast(list[ArtifactUploadPayload], kwargs["files"])
        calls.append([payload.filename for payload in payloads])
        return _staged_submission(payloads)

    monkeypatch.setattr(UploadBatchService, "create_and_stage", staticmethod(create_and_stage))
    monkeypatch.setattr(
        "tricycle_reaction_db.dev.import_artifacts.get_settings",
        lambda: Settings(_env_file=None, max_batch_files=128, max_batch_bytes=1024),
    )

    summary = asyncio.run(
        import_files(
            discover_files(sources),
            project_id=_PROJECT_ID,
            user_id=_USER_ID,
            artifact_kind=ArtifactKind.INPUT,
            state=ImportState(tmp_path / "state.jsonl"),
            dry_run=False,
            commit_batch_files=2,
            pipeline_window_files=8,
            stream_queue_size=2,
        )
    )

    assert summary.staged == 17
    assert [len(call) for call in calls] == [8, 8, 1]


def test_import_files_honors_queue_batch_byte_budget(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    sources = []
    for index in range(3):
        source = tmp_path / f"file-{index}.log"
        source.write_bytes(b"1" * 400)
        sources.append(source)
    calls: list[list[str]] = []

    async def create_and_stage(**kwargs: object) -> StagedUploadSubmission:
        payloads = cast(list[ArtifactUploadPayload], kwargs["files"])
        calls.append([payload.filename for payload in payloads])
        return _staged_submission(payloads)

    monkeypatch.setattr(UploadBatchService, "create_and_stage", staticmethod(create_and_stage))
    monkeypatch.setattr(
        "tricycle_reaction_db.dev.import_artifacts.get_settings",
        lambda: Settings(_env_file=None, max_batch_files=128, max_batch_bytes=1024),
    )

    summary = asyncio.run(
        import_files(
            discover_files(sources),
            project_id=_PROJECT_ID,
            user_id=_USER_ID,
            artifact_kind=ArtifactKind.INPUT,
            state=ImportState(None),
            dry_run=False,
        )
    )

    assert summary.staged == 3
    assert [len(call) for call in calls] == [2, 1]


def test_import_files_stages_before_fingerprinting_all_candidates(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    sources = []
    for index in range(40):
        source = tmp_path / f"file-{index:02d}.log"
        source.write_bytes(source.name.encode())
        sources.append(source)
    fingerprint_calls: list[str] = []
    first_stage_fingerprint_count: list[int] = []

    def fake_fingerprint(path: Path) -> ImportFingerprint:
        fingerprint_calls.append(path.name)
        time.sleep(0.01)
        return file_fingerprint(path)

    async def create_and_stage(**kwargs: object) -> StagedUploadSubmission:
        payloads = cast(list[ArtifactUploadPayload], kwargs["files"])
        first_stage_fingerprint_count.append(len(fingerprint_calls))
        return _staged_submission(payloads)

    monkeypatch.setattr(
        "tricycle_reaction_db.dev.import_artifacts.file_fingerprint",
        fake_fingerprint,
    )
    monkeypatch.setattr(UploadBatchService, "create_and_stage", staticmethod(create_and_stage))
    monkeypatch.setattr(
        "tricycle_reaction_db.dev.import_artifacts.get_settings",
        lambda: Settings(_env_file=None, max_batch_files=128, max_batch_bytes=1024),
    )

    summary = asyncio.run(
        import_files(
            discover_files(sources),
            project_id=_PROJECT_ID,
            user_id=_USER_ID,
            artifact_kind=ArtifactKind.INPUT,
            state=ImportState(None),
            dry_run=False,
            fingerprint_workers=2,
            pipeline_window_files=16,
            stream_queue_size=2,
        )
    )

    assert summary.staged == 40
    assert first_stage_fingerprint_count
    assert first_stage_fingerprint_count[0] < len(sources)
