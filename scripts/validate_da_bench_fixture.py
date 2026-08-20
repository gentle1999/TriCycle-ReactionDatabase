"""Validate the checked-in DA-bench fixture and every manifest digest."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import zlib
from pathlib import Path
from typing import cast

JsonObject = dict[str, object]

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE_ROOT = REPOSITORY_ROOT / "tests/fixtures/da_bench_minimal"
EXPECTED_SCHEMA_VERSION = "da-bench-fixture-v3"


def _mapping(value: object, label: str, errors: list[str]) -> JsonObject | None:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        errors.append(f"{label} must be an object with string keys")
        return None
    return cast(JsonObject, value)


def _string(mapping: JsonObject, key: str, label: str, errors: list[str]) -> str | None:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        errors.append(f"{label}.{key} must be a non-empty string")
        return None
    return value


def _nonnegative_int(mapping: JsonObject, key: str, label: str, errors: list[str]) -> int | None:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        errors.append(f"{label}.{key} must be a non-negative integer")
        return None
    return value


def _digest(mapping: JsonObject, key: str, label: str, errors: list[str]) -> str | None:
    value = _string(mapping, key, label, errors)
    if value is None:
        return None
    if len(value) != hashlib.sha256().digest_size * 2 or any(
        character not in "0123456789abcdef" for character in value
    ):
        errors.append(f"{label}.{key} must be a lowercase SHA-256 digest")
        return None
    return value


def _fixture_file(
    fixture_root: Path,
    relative_path: str,
    label: str,
    errors: list[str],
) -> Path | None:
    fragment = Path(relative_path)
    if fragment.is_absolute() or ".." in fragment.parts:
        errors.append(f"{label} path escapes fixture root: {relative_path}")
        return None

    path = (fixture_root / fragment).resolve()
    try:
        path.relative_to(fixture_root)
    except ValueError:
        errors.append(f"{label} path escapes fixture root: {relative_path}")
        return None
    if not path.is_file():
        errors.append(f"{label} file does not exist: {relative_path}")
        return None
    return path


def _load_manifest(manifest_path: Path, errors: list[str]) -> JsonObject | None:
    try:
        value: object = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"cannot read fixture manifest {manifest_path}: {exc}")
        return None
    return _mapping(value, "manifest", errors)


def _read_bytes(path: Path, label: str, relative_path: str, errors: list[str]) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError as exc:
        errors.append(f"cannot read {label} file {relative_path}: {exc}")
        return None


def validate_fixture(fixture_root: Path = DEFAULT_FIXTURE_ROOT) -> list[str]:
    """Return all contract violations found under ``fixture_root``.

    The validator intentionally reads only paths listed by ``manifest.json``. It
    rejects absolute or parent-traversing paths before resolving any file, so it
    can run safely in CI before the seed command consumes the fixture.
    """

    errors: list[str] = []
    fixture_root = fixture_root.resolve()
    if not fixture_root.is_dir():
        return [f"fixture root does not exist: {fixture_root}"]

    manifest_path = fixture_root / "manifest.json"
    manifest = _load_manifest(manifest_path, errors)
    if manifest is None:
        return errors

    schema_version = _string(manifest, "schema_version", "manifest", errors)
    if schema_version is not None and schema_version != EXPECTED_SCHEMA_VERSION:
        errors.append(
            f"manifest.schema_version must be {EXPECTED_SCHEMA_VERSION!r}, got {schema_version!r}"
        )

    raw_logs = manifest.get("logs")
    if not isinstance(raw_logs, list) or not raw_logs:
        errors.append("manifest.logs must be a non-empty array")
        raw_logs = []
    raw_metadata = manifest.get("metadata_files")
    if not isinstance(raw_metadata, list) or not raw_metadata:
        errors.append("manifest.metadata_files must be a non-empty array")
        raw_metadata = []

    seen_paths: set[str] = set()
    seen_roles: set[str] = set()
    for index, raw_entry in enumerate(raw_logs):
        label = f"manifest.logs[{index}]"
        entry = _mapping(raw_entry, label, errors)
        if entry is None:
            continue
        role = _string(entry, "role", label, errors)
        if role is not None:
            if role in seen_roles:
                errors.append(f"duplicate log role: {role}")
            seen_roles.add(role)
        relative_path = _string(entry, "relative_path", label, errors)
        source_size = _nonnegative_int(entry, "source_size_bytes", label, errors)
        source_digest = _digest(entry, "source_sha256", label, errors)
        gzip_digest = _digest(entry, "gzip_sha256", label, errors)
        if relative_path is None:
            continue
        if relative_path in seen_paths:
            errors.append(f"duplicate fixture path: {relative_path}")
        seen_paths.add(relative_path)
        path = _fixture_file(fixture_root, relative_path, label, errors)
        if path is None:
            continue

        compressed = _read_bytes(path, label, relative_path, errors)
        if compressed is None:
            continue
        if gzip_digest is not None and hashlib.sha256(compressed).hexdigest() != gzip_digest:
            errors.append(f"{label} gzip SHA-256 mismatch: {relative_path}")
        try:
            source = gzip.decompress(compressed)
        except (OSError, EOFError, zlib.error) as exc:
            errors.append(f"{label} is not a valid gzip stream: {relative_path}: {exc}")
            continue
        if source_size is not None and len(source) != source_size:
            errors.append(
                f"{label} source size mismatch for {relative_path}: "
                f"expected {source_size}, got {len(source)}"
            )
        if source_digest is not None and hashlib.sha256(source).hexdigest() != source_digest:
            errors.append(f"{label} source SHA-256 mismatch: {relative_path}")

    seen_metadata_paths: set[str] = set()
    for index, raw_entry in enumerate(raw_metadata):
        label = f"manifest.metadata_files[{index}]"
        entry = _mapping(raw_entry, label, errors)
        if entry is None:
            continue
        relative_path = _string(entry, "relative_path", label, errors)
        size = _nonnegative_int(entry, "size_bytes", label, errors)
        digest = _digest(entry, "sha256", label, errors)
        if relative_path is None:
            continue
        if relative_path in seen_paths or relative_path in seen_metadata_paths:
            errors.append(f"duplicate fixture path: {relative_path}")
        seen_paths.add(relative_path)
        seen_metadata_paths.add(relative_path)
        path = _fixture_file(fixture_root, relative_path, label, errors)
        if path is None:
            continue
        payload = _read_bytes(path, label, relative_path, errors)
        if payload is None:
            continue
        if size is not None and len(payload) != size:
            errors.append(
                f"{label} size mismatch for {relative_path}: expected {size}, got {len(payload)}"
            )
        if digest is not None and hashlib.sha256(payload).hexdigest() != digest:
            errors.append(f"{label} SHA-256 mismatch: {relative_path}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-root", type=Path, default=DEFAULT_FIXTURE_ROOT)
    arguments = parser.parse_args()
    fixture_root = arguments.fixture_root.resolve()
    errors = validate_fixture(fixture_root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    manifest = json.loads((fixture_root / "manifest.json").read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "fixture_root": str(fixture_root),
                "schema_version": manifest["schema_version"],
                "logs": len(manifest["logs"]),
                "metadata_files": len(manifest["metadata_files"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
