import asyncio
from pathlib import Path
from typing import cast
from uuid import UUID

from tricycle_reaction_db.application.dtos import (
    ArtifactBatchUploadItem,
    ArtifactBatchUploadResult,
    ArtifactUploadResult,
)
from tricycle_reaction_db.application.services.transition_state_uploads import (
    ArtifactUploadPayload,
    ArtifactUploadService,
)
from tricycle_reaction_db.core.config import Settings
from tricycle_reaction_db.dev.import_artifacts import (
    ImportCandidate,
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
    summary = asyncio.run(
        import_files(
            discover_files([source]),
            project_id=UUID("00000000-0000-7000-8000-000000000201"),
            user_id=UUID("00000000-0000-7000-8000-000000000002"),
            artifact_kind=ArtifactKind.INPUT,
            state=ImportState(None),
            dry_run=False,
        )
    )

    assert summary.succeeded == 1
    assert len(calls) == 1
    assert calls[0][0].spool_path == source.resolve()
