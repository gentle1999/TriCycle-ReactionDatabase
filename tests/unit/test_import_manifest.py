import os
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from tricycle_reaction_db.dev.import_artifacts import (
    ImportFingerprint,
    ImportState,
    candidates_from_manifest,
)
from tricycle_reaction_db.domain.enums import ArtifactKind
from tricycle_reaction_db.ingestion.manifest import (
    ArtifactManifest,
    ArtifactManifestEntry,
    ManifestError,
    validate_manifest_staging,
)

PROJECT_ID = UUID("00000000-0000-7000-8000-000000000201")
ARCHIVE_SHA256 = "a" * 64


def _entry(
    root: Path,
    relative_path: str,
    payload: bytes,
    *,
    selection_status: str = "selected",
) -> ArtifactManifestEntry:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return ArtifactManifestEntry(
        archive_sha256=ARCHIVE_SHA256,
        relative_path=relative_path,
        staged_file_path=str(path),
        file_sha256=sha256(payload).hexdigest(),
        size_bytes=len(payload),
        media_type="text/plain",
        is_gaussian_log=relative_path.endswith(".log"),
        selection_status=selection_status,  # type: ignore[arg-type]
    )


def test_manifest_imports_only_selected_entries_and_preserves_paths(tmp_path: Path) -> None:
    selected = _entry(tmp_path, "run-01/ts/result.log", b"selected")
    filtered = _entry(
        tmp_path,
        "run-01/notes.txt",
        b"filtered",
        selection_status="filtered",
    )
    # This file is deliberately absent from the manifest. It must not become
    # an implicit import merely because it is below the staging root.
    (tmp_path / "run-01" / "new.log").write_text("not selected", encoding="utf-8")
    manifest = ArtifactManifest(
        archive_sha256=ARCHIVE_SHA256,
        entries=[selected, filtered],
    )

    candidates = candidates_from_manifest(manifest, staging_root=tmp_path)

    assert [candidate.relative_path for candidate in candidates] == ["run-01/ts/result.log"]
    assert candidates[0].path == (tmp_path / "run-01/ts/result.log").resolve()
    assert candidates[0].archive_sha256 == ARCHIVE_SHA256
    assert candidates[0].file_sha256 == selected.file_sha256


def test_manifest_rehash_rejects_changed_file(tmp_path: Path) -> None:
    entry = _entry(tmp_path, "run/result.log", b"original")
    manifest = ArtifactManifest(archive_sha256=ARCHIVE_SHA256, entries=[entry])
    (tmp_path / "run/result.log").write_bytes(b"changed")

    with pytest.raises(ManifestError, match="identity mismatch"):
        validate_manifest_staging(manifest, staging_root=tmp_path)


def test_manifest_identity_does_not_depend_on_staging_location(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first = _entry(first_root, "run/result.log", b"same bytes")
    second = _entry(second_root, "run/result.log", b"same bytes")

    first_manifest = ArtifactManifest(archive_sha256=ARCHIVE_SHA256, entries=[first])
    second_manifest = ArtifactManifest(archive_sha256=ARCHIVE_SHA256, entries=[second])

    assert first_manifest.content_sha256() == second_manifest.content_sha256()


def test_manifest_identity_does_not_depend_on_entry_order(tmp_path: Path) -> None:
    first = _entry(tmp_path, "run-a/result.log", b"first")
    second = _entry(tmp_path, "run-b/result.log", b"second")

    first_manifest = ArtifactManifest(
        archive_sha256=ARCHIVE_SHA256,
        entries=[first, second],
    )
    second_manifest = ArtifactManifest(
        archive_sha256=ARCHIVE_SHA256,
        entries=[second, first],
    )

    assert first_manifest.content_sha256() == second_manifest.content_sha256()


def test_manifest_rejects_paths_outside_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.log"
    outside.write_bytes(b"outside")
    entry = ArtifactManifestEntry(
        archive_sha256=ARCHIVE_SHA256,
        relative_path="outside.log",
        staged_file_path=str(outside),
        file_sha256=sha256(b"outside").hexdigest(),
        size_bytes=7,
        media_type="text/plain",
        is_gaussian_log=True,
    )
    manifest = ArtifactManifest(archive_sha256=ARCHIVE_SHA256, entries=[entry])

    with pytest.raises(ManifestError, match="outside"):
        validate_manifest_staging(manifest, staging_root=tmp_path)


def test_manifest_rejects_symlink_and_hardlink_entries(tmp_path: Path) -> None:
    target = tmp_path / "target.log"
    target.write_bytes(b"target")
    symlink = tmp_path / "symlink.log"
    symlink.symlink_to(target)
    symlink_entry = ArtifactManifestEntry(
        archive_sha256=ARCHIVE_SHA256,
        relative_path="symlink.log",
        staged_file_path=str(symlink),
        file_sha256=sha256(b"target").hexdigest(),
        size_bytes=6,
        media_type="text/plain",
        is_gaussian_log=True,
    )
    with pytest.raises(ManifestError, match="symbolic link"):
        validate_manifest_staging(
            ArtifactManifest(archive_sha256=ARCHIVE_SHA256, entries=[symlink_entry]),
            staging_root=tmp_path,
        )

    if os.name == "nt":
        pytest.skip("hard-link security test requires POSIX link semantics")
    hardlink = tmp_path / "hardlink.log"
    hardlink.hardlink_to(target)
    hardlink_entry = ArtifactManifestEntry(
        archive_sha256=ARCHIVE_SHA256,
        relative_path="hardlink.log",
        staged_file_path=str(hardlink),
        file_sha256=sha256(b"target").hexdigest(),
        size_bytes=6,
        media_type="text/plain",
        is_gaussian_log=True,
    )
    with pytest.raises(ManifestError, match="hard link"):
        validate_manifest_staging(
            ArtifactManifest(archive_sha256=ARCHIVE_SHA256, entries=[hardlink_entry]),
            staging_root=tmp_path,
        )


@pytest.mark.parametrize("relative_path", ["../escape.log", "/absolute.log", "./dot.log", ""])
def test_manifest_rejects_unsafe_relative_paths(tmp_path: Path, relative_path: str) -> None:
    with pytest.raises(ValidationError, match="relative_path"):
        ArtifactManifestEntry(
            archive_sha256=ARCHIVE_SHA256,
            relative_path=relative_path,
            staged_file_path=str(tmp_path / "placeholder.log"),
            file_sha256=sha256(b"payload").hexdigest(),
            size_bytes=7,
            media_type="text/plain",
            is_gaussian_log=True,
        )


def test_import_state_identity_separates_same_basename_paths(tmp_path: Path) -> None:
    first = tmp_path / "run-a" / "result.log"
    second = tmp_path / "run-b" / "result.log"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    state = ImportState(tmp_path / "import-state.jsonl")
    archive = ARCHIVE_SHA256
    for path, relative in ((first, "run-a/result.log"), (second, "run-b/result.log")):
        fingerprint = ImportFingerprint(
            size_bytes=path.stat().st_size,
            mtime_ns=path.stat().st_mtime_ns,
            sha256=sha256(path.read_bytes()).hexdigest(),
        )
        state.append(
            {
                "source": str(path),
                "identity": ImportState._identity(
                    project_id=PROJECT_ID,
                    artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                    archive_sha256=archive,
                    relative_path=relative,
                    file_sha256=fingerprint.sha256,
                ),
                "status": "succeeded",
                "project_id": str(PROJECT_ID),
                "artifact_kind": ArtifactKind.CALCULATION_OUTPUT.value,
                "size_bytes": fingerprint.size_bytes,
                "mtime_ns": fingerprint.mtime_ns,
                "sha256": fingerprint.sha256,
            }
        )

    assert all(
        state.terminal(
            path,
            project_id=PROJECT_ID,
            artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
            fingerprint=ImportFingerprint(
                size_bytes=path.stat().st_size,
                mtime_ns=path.stat().st_mtime_ns,
                sha256=sha256(path.read_bytes()).hexdigest(),
            ),
            archive_sha256=archive,
            relative_path=relative,
            file_sha256=sha256(path.read_bytes()).hexdigest(),
        )
        for path, relative in ((first, "run-a/result.log"), (second, "run-b/result.log"))
    )
