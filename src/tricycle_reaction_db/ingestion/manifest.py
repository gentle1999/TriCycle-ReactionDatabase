"""Validated manifests for extracted artifact imports.

The manifest is the boundary between an archive extractor and the database
ingestion pipeline.  It deliberately contains both the archive-relative name
and the server-side staging path.  The latter is never trusted by itself: the
path is checked against the configured staging root and the bytes are hashed
again immediately before they are handed to the upload service.
"""

from __future__ import annotations

import json
import mimetypes
import os
import stat
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MANIFEST_SCHEMA_VERSION = "artifact-manifest-v1"
SHA256_PATTERN = r"^[0-9a-fA-F]{64}$"
ManifestSelectionStatus = Literal["selected", "filtered", "rejected"]


class ManifestError(ValueError):
    """Raised when a manifest or its staged files cannot be trusted."""


def _normalize_sha256(value: str, *, field_name: str) -> str:
    normalized = value.strip().casefold()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError(f"{field_name} must be a SHA-256 hex digest")
    return normalized


def normalize_relative_path(value: str) -> str:
    """Return one canonical archive-relative POSIX path.

    Empty components and ``..`` are rejected instead of normalized away.  A
    manifest should describe exactly what the extractor produced, so silently
    rewriting an unsafe name would make the audit record ambiguous.
    """

    normalized = value.replace("\\", "/")
    raw_parts = normalized.split("/") if normalized else []
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in raw_parts)
        or "\x00" in normalized
        or path == PurePosixPath(".")
    ):
        raise ValueError("relative_path must be a non-empty safe POSIX path")
    return str(path)


class ArtifactManifestEntry(BaseModel):
    """One explicitly selected or explicitly rejected staged file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    archive_sha256: str
    relative_path: str = Field(min_length=1, max_length=4096)
    staged_file_path: str = Field(min_length=1, max_length=16_384)
    file_sha256: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=255)
    is_gaussian_log: bool
    selection_status: ManifestSelectionStatus = "selected"

    @field_validator("archive_sha256", "file_sha256")
    @classmethod
    def validate_hash(cls, value: str, info: Any) -> str:
        return _normalize_sha256(value, field_name=info.field_name)

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        return normalize_relative_path(value)

    @field_validator("staged_file_path")
    @classmethod
    def validate_staged_path(cls, value: str) -> str:
        normalized = value.strip()
        path = Path(normalized)
        if not normalized or not path.is_absolute() or "\x00" in normalized:
            raise ValueError("staged_file_path must be an absolute local path")
        return normalized


class ArtifactManifest(BaseModel):
    """An immutable description of one extracted archive import."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = MANIFEST_SCHEMA_VERSION
    archive_sha256: str
    entries: list[ArtifactManifestEntry] = Field(min_length=1, max_length=100_000)
    # Optional so a producer can publish a detached manifest digest.  The
    # canonical digest is always recomputed by ``content_sha256`` below.
    manifest_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)

    @field_validator("archive_sha256", "manifest_sha256")
    @classmethod
    def validate_manifest_hash(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _normalize_sha256(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_entries(self) -> ArtifactManifest:
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported manifest schema version: {self.schema_version}")
        paths = [entry.relative_path for entry in self.entries]
        if len(paths) != len(set(paths)):
            raise ValueError("manifest relative_path values must be unique")
        archive_hashes = {entry.archive_sha256 for entry in self.entries}
        if archive_hashes != {self.archive_sha256}:
            raise ValueError("every manifest entry must use the manifest archive_sha256")
        return self

    def canonical_payload(self) -> dict[str, Any]:
        """Return the identity content covered by the manifest digest.

        ``staged_file_path`` is intentionally omitted from each entry: it is
        an operator-local placement detail, not part of the archive identity.
        This keeps re-registering the same archive idempotent after a staging
        directory is relocated while the stored path is still revalidated
        against the configured root before every import.
        """

        payload = self.model_dump(mode="json", exclude={"manifest_sha256"})
        payload["entries"] = sorted(payload["entries"], key=lambda entry: entry["relative_path"])
        for entry in payload["entries"]:
            entry.pop("staged_file_path", None)
        return payload

    def content_sha256(self) -> str:
        payload = json.dumps(
            self.canonical_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(payload).hexdigest()

    def verify_declared_digest(self) -> None:
        if self.manifest_sha256 is not None and self.manifest_sha256 != self.content_sha256():
            raise ManifestError("manifest_sha256 does not match the canonical manifest")


def load_manifest(path: Path) -> ArtifactManifest:
    """Load and validate a JSON manifest from an operator-controlled file."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ManifestError(f"cannot read manifest {path}: {error}") from error
    try:
        manifest = ArtifactManifest.model_validate(payload)
    except ValueError as error:
        raise ManifestError(f"invalid manifest {path}: {error}") from error
    manifest.verify_declared_digest()
    return manifest


def _regular_file_without_links(path: Path) -> os.stat_result:
    try:
        result = os.lstat(path)
    except OSError as error:
        raise ManifestError(f"staged file is not accessible: {path}") from error
    if stat.S_ISLNK(result.st_mode):
        raise ManifestError(f"staged file must not be a symbolic link: {path}")
    if not stat.S_ISREG(result.st_mode):
        raise ManifestError(f"staged path must be a regular file: {path}")
    if result.st_nlink != 1:
        raise ManifestError(f"staged file must not be a hard link: {path}")
    return result


def _open_regular_file_without_links(path: Path) -> tuple[int, os.stat_result]:
    """Open the final path component without following a replacement link."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    try:
        descriptor = os.open(path, flags)
        result = os.fstat(descriptor)
    except OSError as error:
        raise ManifestError(f"staged file is not safely readable: {path}") from error
    if not stat.S_ISREG(result.st_mode):
        os.close(descriptor)
        raise ManifestError(f"staged path must be a regular file: {path}")
    if result.st_nlink != 1:
        os.close(descriptor)
        raise ManifestError(f"staged file must not be a hard link: {path}")
    return descriptor, result


def validate_staged_file(path: str | Path, *, staging_root: Path) -> Path:
    """Validate one manifest path without following links or leaving ``staging_root``."""

    root = Path(staging_root)
    if not root.is_absolute():
        raise ManifestError("staging_root must be an absolute path")
    try:
        original_root_stat = os.lstat(root)
    except OSError as error:
        raise ManifestError(f"staging_root is not accessible: {root}") from error
    if stat.S_ISLNK(original_root_stat.st_mode):
        raise ManifestError("staging_root must not be a symbolic link")
    try:
        root = root.resolve(strict=True)
        root_stat = os.lstat(root)
    except OSError as error:
        raise ManifestError(f"staging_root is not accessible: {root}") from error
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ManifestError("staging_root must be a real directory")

    candidate = Path(path)
    if not candidate.is_absolute():
        raise ManifestError("staged_file_path must be absolute")
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise ManifestError("staged_file_path is outside the configured staging root") from error
    if ".." in relative.parts or "." in relative.parts:
        raise ManifestError("staged_file_path contains an unsafe path component")

    current = root
    for component in relative.parts[:-1]:
        current /= component
        try:
            component_stat = os.lstat(current)
        except OSError as error:
            raise ManifestError(f"staged directory is not accessible: {current}") from error
        if stat.S_ISLNK(component_stat.st_mode) or not stat.S_ISDIR(component_stat.st_mode):
            raise ManifestError(f"staged path contains a link or non-directory: {current}")

    _regular_file_without_links(candidate)
    return candidate


def fingerprint_staged_file(path: str | Path, *, staging_root: Path) -> tuple[Path, int, str]:
    """Revalidate and hash one staged file immediately before import."""

    candidate = validate_staged_file(path, staging_root=staging_root)
    descriptor, initial_stat = _open_regular_file_without_links(candidate)
    digest = sha256()
    size = 0
    try:
        with os.fdopen(descriptor, "rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    except OSError as error:
        raise ManifestError(f"cannot hash staged file {candidate}: {error}") from error
    # Re-stat after reading.  A replacement or in-place mutation during the
    # read is rejected even if the final digest happens to match a stale size.
    final_stat = _regular_file_without_links(candidate)
    if (
        final_stat.st_size != size
        or final_stat.st_dev != initial_stat.st_dev
        or final_stat.st_ino != initial_stat.st_ino
    ):
        raise ManifestError(f"staged file changed while being hashed: {candidate}")
    return candidate, size, digest.hexdigest()


def validate_manifest_staging(
    manifest: ArtifactManifest,
    *,
    staging_root: Path,
) -> list[tuple[ArtifactManifestEntry, Path]]:
    """Validate every selected manifest entry and return only selected files."""

    manifest.verify_declared_digest()
    selected: list[tuple[ArtifactManifestEntry, Path]] = []
    for entry in manifest.entries:
        path, size, digest = fingerprint_staged_file(
            entry.staged_file_path,
            staging_root=staging_root,
        )
        if size != entry.size_bytes or digest != entry.file_sha256:
            raise ManifestError(
                "manifest file identity mismatch for "
                f"{entry.relative_path}: expected {entry.file_sha256}/{entry.size_bytes}, "
                f"got {digest}/{size}"
            )
        if entry.selection_status == "selected":
            selected.append((entry, path))
    return selected


def guessed_media_type(path: str | Path) -> str:
    """Use the same conservative MIME fallback as the local importer."""

    return mimetypes.guess_type(Path(path).name, strict=False)[0] or "application/octet-stream"


__all__ = [
    "ArtifactManifest",
    "ArtifactManifestEntry",
    "MANIFEST_SCHEMA_VERSION",
    "ManifestError",
    "ManifestSelectionStatus",
    "fingerprint_staged_file",
    "guessed_media_type",
    "load_manifest",
    "normalize_relative_path",
    "validate_manifest_staging",
    "validate_staged_file",
]
