"""Bounded, side-effect-free validation for artifact upload sources.

This module only inspects bytes and spool files.  Authorization, object-store
reservation, and database persistence stay in the upload orchestrator, which
makes the resource limits independently testable and keeps source handling
from being mixed with ingestion state transitions.
"""

from __future__ import annotations

import gzip
import io
import zlib
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from tricycle_reaction_db.application.services.artifact_upload_types import (
    ArtifactUploadError,
    ArtifactUploadLimitError,
    ArtifactUploadPayload,
    _InspectedUploadSource,
)


class _InspectingReader:
    """Record raw source identity while a gzip reader consumes a stream."""

    def __init__(self, stream: io.BufferedReader, *, probe_size: int) -> None:
        self._stream = stream
        self._digest = sha256()
        self._probe = bytearray()
        self._probe_size = probe_size
        self.size_bytes = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size)
        if chunk:
            self._digest.update(chunk)
            self.size_bytes += len(chunk)
            if len(self._probe) < self._probe_size:
                self._probe.extend(chunk[: self._probe_size - len(self._probe)])
        return chunk

    @property
    def content_sha256(self) -> str:
        return self._digest.hexdigest()

    @property
    def media_probe(self) -> bytes:
        return bytes(self._probe)


def safe_parser_suffix(filename: str) -> str:
    """Return a parser-safe suffix without trusting an uploaded path."""

    name = Path(filename).name.lower()
    if name.endswith(".gz"):
        name = name[:-3]
    suffix = Path(name).suffix
    return suffix if suffix in {".log", ".out", ".xyz"} else ".log"


def require_upload_size(payload: bytes, *, maximum_size: int) -> None:
    if len(payload) > maximum_size:
        raise ArtifactUploadError(f"uploaded artifact exceeds the {maximum_size}-byte limit")


def parser_payload(
    payload: bytes,
    filename: str,
    *,
    max_decompressed_bytes: int,
) -> tuple[bytes, str | None]:
    """Decode one parser payload while bounding both compressed and raw sizes."""

    maximum = max_decompressed_bytes
    if len(payload) > maximum:
        raise ArtifactUploadError(f"uploaded artifact exceeds the {maximum}-byte limit")
    if payload.startswith(b"\x1f\x8b") or filename.lower().endswith(".gz"):
        try:
            output = bytearray()
            with gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb") as stream:
                while True:
                    chunk = stream.read(min(1024 * 1024, maximum + 1 - len(output)))
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > maximum:
                        raise ArtifactUploadError(
                            f"decompressed artifact exceeds the {maximum}-byte limit"
                        )
            return bytes(output), "gzip"
        except (EOFError, OSError, zlib.error) as error:
            raise ArtifactUploadError("uploaded gzip artifact is invalid") from error
    return payload, None


def require_decompressed_upload_size(
    payload: bytes,
    filename: str,
    *,
    maximum_size: int,
) -> None:
    """Reject compressed resource bombs while preserving invalid-file isolation."""

    if not (payload.startswith(b"\x1f\x8b") or filename.lower().endswith(".gz")):
        return
    try:
        parser_payload(payload, filename, max_decompressed_bytes=maximum_size)
    except ArtifactUploadError as error:
        if "exceeds the" in str(error):
            raise ArtifactUploadLimitError(str(error)) from error


def inspect_upload_source(
    file: ArtifactUploadPayload,
    *,
    maximum_size: int,
) -> _InspectedUploadSource:
    """Inspect one source without materializing a spooled file in memory."""

    if file.payload is not None:
        payload = file.payload
        if len(payload) > maximum_size:
            raise ArtifactUploadLimitError(
                f"uploaded artifact exceeds the {maximum_size}-byte limit"
            )
        require_decompressed_upload_size(payload, file.filename, maximum_size=maximum_size)
        return _InspectedUploadSource(
            source=payload,
            size_bytes=len(payload),
            content_sha256=sha256(payload).hexdigest(),
            media_probe=payload[: 64 * 1024],
        )

    if file.spool_path is None:
        raise ArtifactUploadError("uploaded artifact has no payload")
    expected_size = file.spool_path.stat().st_size
    if expected_size > maximum_size:
        raise ArtifactUploadLimitError(f"uploaded artifact exceeds the {maximum_size}-byte limit")

    with file.spool_path.open("rb") as stream:
        source = _InspectingReader(stream, probe_size=64 * 1024)
        is_gzip = file.filename.lower().endswith(".gz") or stream.peek(2)[:2] == b"\x1f\x8b"
        if is_gzip:
            try:
                with gzip.GzipFile(fileobj=cast(Any, source), mode="rb") as decompressed:
                    decompressed_size = 0
                    while chunk := decompressed.read(min(1024 * 1024, maximum_size + 1)):
                        decompressed_size += len(chunk)
                        if decompressed_size > maximum_size:
                            raise ArtifactUploadLimitError(
                                f"decompressed artifact exceeds the {maximum_size}-byte limit"
                            )
            except ArtifactUploadLimitError:
                raise
            except (EOFError, OSError, zlib.error):
                # Invalid gzip remains an isolated MolOP parse failure, matching
                # the bytes-upload path's validation behavior.
                pass
        while source.read(1024 * 1024):
            pass

    if source.size_bytes != expected_size:
        raise ArtifactUploadError("uploaded spool file changed while being inspected")
    return _InspectedUploadSource(
        source=file.spool_path,
        size_bytes=source.size_bytes,
        content_sha256=source.content_sha256,
        media_probe=source.media_probe,
    )


def require_batch_upload_budget(
    files: list[ArtifactUploadPayload],
    *,
    maximum_upload_size: int,
    maximum_batch_files: int,
    maximum_batch_bytes: int,
    enforce_batch_files: bool = True,
    enforce_batch_bytes: bool = True,
) -> dict[int, _InspectedUploadSource]:
    """Validate every batch dimension before authorization, storage, or parsing."""

    if enforce_batch_files and len(files) > maximum_batch_files:
        raise ArtifactUploadLimitError(f"upload batch exceeds the {maximum_batch_files}-file limit")
    total_bytes = 0
    inspected_by_index: dict[int, _InspectedUploadSource] = {}
    for index, file in enumerate(files):
        if file.payload is None and file.spool_path is None:
            continue
        inspected = inspect_upload_source(file, maximum_size=maximum_upload_size)
        if enforce_batch_bytes:
            total_bytes += inspected.size_bytes
        if enforce_batch_bytes and total_bytes > maximum_batch_bytes:
            raise ArtifactUploadLimitError(
                f"upload batch exceeds the {maximum_batch_bytes}-byte limit"
            )
        inspected_by_index[index] = inspected
    return inspected_by_index


def upload_payload_bytes(file: ArtifactUploadPayload) -> bytes:
    if file.payload is not None:
        return file.payload
    if file.spool_path is not None:
        return file.spool_path.read_bytes()
    raise ArtifactUploadError("uploaded artifact has no payload")


__all__ = [
    "_InspectingReader",
    "inspect_upload_source",
    "parser_payload",
    "require_batch_upload_budget",
    "require_decompressed_upload_size",
    "require_upload_size",
    "safe_parser_suffix",
    "upload_payload_bytes",
]
