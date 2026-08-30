import asyncio
import json
import time
from pathlib import Path
from typing import cast
from uuid import UUID

from tricycle_reaction_db.application.dtos import (
    ArtifactBatchUploadItem,
    ArtifactBatchUploadResult,
    ArtifactUploadResult,
)
from tricycle_reaction_db.application.services.artifact_uploads import (
    ArtifactUploadPayload,
    ArtifactUploadService,
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
    iter_batches,
)
from tricycle_reaction_db.domain.enums import ArtifactIngestionStatus, ArtifactKind, StorageStatus


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


def test_import_files_passes_local_paths_to_upload_service(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "archive" / "nested" / "input.dat"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"payload")
    calls: list[list[ArtifactUploadPayload]] = []

    async def fake_upload_batch(**kwargs: object) -> ArtifactBatchUploadResult:
        payloads = cast(list[ArtifactUploadPayload], kwargs["files"])
        assert kwargs["reparse_failed_ingestions"] is True
        calls.append(payloads)
        return ArtifactBatchUploadResult(
            total_count=1,
            succeeded_count=1,
            failed_count=0,
            source_frame_count=0,
            transition_state_frame_count=0,
            inferred_reaction_count=0,
            items=[
                ArtifactBatchUploadItem(
                    filename="input.dat",
                    succeeded=True,
                    result=ArtifactUploadResult(
                        artifact_id=UUID("00000000-0000-7000-8000-000000000301"),
                        artifact_kind=ArtifactKind.INPUT,
                        storage_status=StorageStatus.AVAILABLE,
                        ingestion_status=ArtifactIngestionStatus.SUCCEEDED,
                        inferred_reaction_count=0,
                        inferences=[],
                    ),
                )
            ],
        )

    monkeypatch.setattr(ArtifactUploadService, "upload_batch", fake_upload_batch)
    monkeypatch.setattr(
        "tricycle_reaction_db.dev.import_artifacts.get_settings",
        lambda: Settings.model_validate({"max_batch_files": 10, "max_batch_bytes": 1024}),
    )
    metrics = ImportMetrics()
    summary = asyncio.run(
        import_files(
            discover_files([source]),
            project_id=UUID("00000000-0000-7000-8000-000000000201"),
            user_id=UUID("00000000-0000-7000-8000-000000000002"),
            artifact_kind=ArtifactKind.INPUT,
            state=ImportState(None),
            dry_run=False,
            metrics=metrics,
        )
    )

    assert summary.succeeded == 1
    assert len(calls) == 1
    assert calls[0][0].spool_path == source.resolve()
    assert metrics.steps[0]["batch_size"] == 1
    assert metrics.step_timings_ms["fingerprint"] >= 0
    assert metrics.step_timings_ms["upload_batches"] >= 0
    assert metrics.sql_statement_count >= 0


def test_import_files_records_zero_frame_outputs_as_filtered(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "not-a-qm-output.log"
    source.write_text("no calculation frames", encoding="utf-8")
    state_path = tmp_path / "state.jsonl"

    async def fake_upload_batch(**_: object) -> ArtifactBatchUploadResult:
        return ArtifactBatchUploadResult(
            total_count=1,
            succeeded_count=0,
            failed_count=1,
            source_frame_count=0,
            transition_state_frame_count=0,
            inferred_reaction_count=0,
            items=[
                ArtifactBatchUploadItem(
                    filename=source.name,
                    succeeded=False,
                    error_code="no_calculation_frames",
                    error_message="source contains no QM calculation frames; artifact was filtered",
                    result=ArtifactUploadResult(
                        artifact_id=UUID("00000000-0000-7000-0000-000000000301"),
                        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                        storage_status=StorageStatus.AVAILABLE,
                        ingestion_status=ArtifactIngestionStatus.FILTERED,
                        source_frame_count=0,
                        transition_state_frame_count=0,
                        inferred_reaction_count=0,
                        inferences=[],
                    ),
                )
            ],
        )

    monkeypatch.setattr(ArtifactUploadService, "upload_batch", fake_upload_batch)
    monkeypatch.setattr(
        "tricycle_reaction_db.dev.import_artifacts.get_settings",
        lambda: Settings.model_validate({"max_batch_files": 10, "max_batch_bytes": 1024}),
    )

    summary = asyncio.run(
        import_files(
            discover_files([source]),
            project_id=UUID("00000000-0000-7000-8000-000000000201"),
            user_id=UUID("00000000-0000-7000-8000-000000000002"),
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
        project_id=UUID("00000000-0000-7000-8000-000000000201"),
        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
        fingerprint=file_fingerprint(source),
    )


def test_import_files_isolates_deterministic_batch_failures(monkeypatch, tmp_path: Path) -> None:
    sources = [tmp_path / name for name in ("bad.log", "good-a.log", "good-b.log")]
    for source in sources:
        source.write_text(source.name, encoding="utf-8")
    calls: list[list[str]] = []

    async def fake_upload_batch(**kwargs: object) -> ArtifactBatchUploadResult:
        payloads = cast(list[ArtifactUploadPayload], kwargs["files"])
        filenames = [payload.filename for payload in payloads]
        calls.append(filenames)
        if "bad.log" in filenames:
            raise ValueError("inconsistent topology")
        return ArtifactBatchUploadResult(
            total_count=len(payloads),
            succeeded_count=len(payloads),
            failed_count=0,
            source_frame_count=0,
            transition_state_frame_count=0,
            inferred_reaction_count=0,
            items=[
                ArtifactBatchUploadItem(
                    filename=payload.filename,
                    succeeded=True,
                    result=ArtifactUploadResult(
                        artifact_id=UUID("00000000-0000-7000-8000-000000000301"),
                        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                        storage_status=StorageStatus.AVAILABLE,
                        ingestion_status=ArtifactIngestionStatus.SUCCEEDED,
                        inferred_reaction_count=0,
                        inferences=[],
                    ),
                )
                for payload in payloads
            ],
        )

    monkeypatch.setattr(ArtifactUploadService, "upload_batch", fake_upload_batch)
    monkeypatch.setattr(
        "tricycle_reaction_db.dev.import_artifacts.get_settings",
        lambda: Settings(_env_file=None, max_batch_files=10, max_batch_bytes=1024),
    )
    state_path = tmp_path / "state.jsonl"

    summary = asyncio.run(
        import_files(
            discover_files(
                [tmp_path / "bad.log", tmp_path / "good-a.log", tmp_path / "good-b.log"]
            ),
            project_id=UUID("00000000-0000-7000-8000-000000000201"),
            user_id=UUID("00000000-0000-7000-8000-000000000002"),
            artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
            state=ImportState(state_path),
            dry_run=False,
        )
    )

    records = [json.loads(line) for line in state_path.read_text(encoding="utf-8").splitlines()]
    assert summary.attempted == 3
    assert summary.succeeded == 2
    assert summary.failed == 1
    assert calls == [
        ["bad.log", "good-a.log", "good-b.log"],
        ["bad.log"],
        ["good-a.log", "good-b.log"],
    ]
    assert {record["filename"]: record["status"] for record in records} == {
        "bad.log": "failed",
        "good-a.log": "succeeded",
        "good-b.log": "succeeded",
    }


def test_import_files_keeps_pipeline_window_independent_from_persistence_batch(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    sources = []
    for index in range(17):
        source = tmp_path / f"file-{index:02d}.log"
        source.write_bytes(source.name.encode())
        sources.append(source)
    state_path = tmp_path / "state.jsonl"
    calls: list[list[str]] = []
    persistence_batch_sizes: list[int] = []
    state_lines_seen_before_call: list[int] = []

    async def fake_upload_batch(**kwargs: object) -> ArtifactBatchUploadResult:
        payloads = cast(list[ArtifactUploadPayload], kwargs["files"])
        assert kwargs["streaming"] is True
        assert kwargs["enforce_batch_file_limit"] is False
        persistence_batch_sizes.append(cast(int, kwargs["persistence_batch_files"]))
        calls.append([payload.filename for payload in payloads])
        state_lines_seen_before_call.append(
            len(state_path.read_text(encoding="utf-8").splitlines()) if state_path.exists() else 0
        )
        return ArtifactBatchUploadResult(
            total_count=len(payloads),
            succeeded_count=len(payloads),
            failed_count=0,
            source_frame_count=0,
            transition_state_frame_count=0,
            inferred_reaction_count=0,
            items=[
                ArtifactBatchUploadItem(
                    filename=payload.filename,
                    succeeded=True,
                    result=ArtifactUploadResult(
                        artifact_id=UUID("00000000-0000-7000-0000-000000000301"),
                        artifact_kind=ArtifactKind.INPUT,
                        storage_status=StorageStatus.AVAILABLE,
                        ingestion_status=ArtifactIngestionStatus.SUCCEEDED,
                        inferred_reaction_count=0,
                        inferences=[],
                    ),
                )
                for payload in payloads
            ],
        )

    monkeypatch.setattr(ArtifactUploadService, "upload_batch", fake_upload_batch)
    monkeypatch.setattr(
        "tricycle_reaction_db.dev.import_artifacts.get_settings",
        lambda: Settings(_env_file=None, max_batch_files=128, max_batch_bytes=1024),
    )

    summary = asyncio.run(
        import_files(
            discover_files(sources),
            project_id=UUID("00000000-0000-7000-8000-000000000201"),
            user_id=UUID("00000000-0000-7000-8000-000000000002"),
            artifact_kind=ArtifactKind.INPUT,
            state=ImportState(state_path),
            dry_run=False,
            commit_batch_files=2,
            pipeline_window_files=8,
            stream_queue_size=2,
        )
    )

    assert summary.succeeded == 17
    assert [len(call) for call in calls] == [8, 8, 1]
    assert persistence_batch_sizes == [2, 2, 2]
    # The second service call observes the first microbatch's durable state.
    assert state_lines_seen_before_call == [0, 8, 16]


def test_import_files_does_not_split_streaming_microbatches_by_aggregate_bytes(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    sources = []
    for index in range(3):
        source = tmp_path / f"file-{index}.log"
        source.write_bytes(b"1" * 400)
        sources.append(source)
    calls: list[list[str]] = []

    async def fake_upload_batch(**kwargs: object) -> ArtifactBatchUploadResult:
        payloads = cast(list[ArtifactUploadPayload], kwargs["files"])
        assert kwargs["streaming"] is True
        calls.append([payload.filename for payload in payloads])
        return ArtifactBatchUploadResult(
            total_count=len(payloads),
            succeeded_count=len(payloads),
            failed_count=0,
            source_frame_count=0,
            transition_state_frame_count=0,
            inferred_reaction_count=0,
            items=[
                ArtifactBatchUploadItem(
                    filename=payload.filename,
                    succeeded=True,
                    result=ArtifactUploadResult(
                        artifact_id=UUID("00000000-0000-7000-0000-000000000301"),
                        artifact_kind=ArtifactKind.INPUT,
                        storage_status=StorageStatus.AVAILABLE,
                        ingestion_status=ArtifactIngestionStatus.SUCCEEDED,
                        inferred_reaction_count=0,
                        inferences=[],
                    ),
                )
                for payload in payloads
            ],
        )

    monkeypatch.setattr(ArtifactUploadService, "upload_batch", fake_upload_batch)
    monkeypatch.setattr(
        "tricycle_reaction_db.dev.import_artifacts.get_settings",
        lambda: Settings(_env_file=None, max_batch_files=128, max_batch_bytes=1024),
    )

    summary = asyncio.run(
        import_files(
            discover_files(sources),
            project_id=UUID("00000000-0000-7000-0000-000000000201"),
            user_id=UUID("00000000-0000-7000-8000-000000000002"),
            artifact_kind=ArtifactKind.INPUT,
            state=ImportState(None),
            dry_run=False,
            commit_batch_files=16,
        )
    )

    assert summary.succeeded == 3
    assert [len(call) for call in calls] == [3]


def test_import_files_starts_upload_before_fingerprinting_all_candidates(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    sources = []
    for index in range(40):
        source = tmp_path / f"file-{index:02d}.log"
        source.write_bytes(source.name.encode())
        sources.append(source)
    fingerprint_calls: list[str] = []
    first_upload_fingerprint_count: list[int] = []

    def fake_fingerprint(path: Path) -> ImportFingerprint:
        fingerprint_calls.append(path.name)
        time.sleep(0.01)
        return file_fingerprint(path)

    async def fake_upload_batch(**kwargs: object) -> ArtifactBatchUploadResult:
        payloads = cast(list[ArtifactUploadPayload], kwargs["files"])
        first_upload_fingerprint_count.append(len(fingerprint_calls))
        return ArtifactBatchUploadResult(
            total_count=len(payloads),
            succeeded_count=len(payloads),
            failed_count=0,
            source_frame_count=0,
            transition_state_frame_count=0,
            inferred_reaction_count=0,
            items=[
                ArtifactBatchUploadItem(
                    filename=payload.filename,
                    succeeded=True,
                    result=ArtifactUploadResult(
                        artifact_id=UUID("00000000-0000-7000-0000-000000000301"),
                        artifact_kind=ArtifactKind.INPUT,
                        storage_status=StorageStatus.AVAILABLE,
                        ingestion_status=ArtifactIngestionStatus.SUCCEEDED,
                        inferred_reaction_count=0,
                        inferences=[],
                    ),
                )
                for payload in payloads
            ],
        )

    monkeypatch.setattr(
        "tricycle_reaction_db.dev.import_artifacts.file_fingerprint",
        fake_fingerprint,
    )
    monkeypatch.setattr(ArtifactUploadService, "upload_batch", fake_upload_batch)
    monkeypatch.setattr(
        "tricycle_reaction_db.dev.import_artifacts.get_settings",
        lambda: Settings(_env_file=None, max_batch_files=128, max_batch_bytes=1024),
    )

    summary = asyncio.run(
        import_files(
            discover_files(sources),
            project_id=UUID("00000000-0000-7000-8000-000000000201"),
            user_id=UUID("00000000-0000-7000-8000-000000000002"),
            artifact_kind=ArtifactKind.INPUT,
            state=ImportState(None),
            dry_run=False,
            fingerprint_workers=2,
            commit_batch_files=16,
            pipeline_window_files=16,
            stream_queue_size=2,
        )
    )

    assert summary.succeeded == 40
    assert first_upload_fingerprint_count
    assert first_upload_fingerprint_count[0] < len(sources)
