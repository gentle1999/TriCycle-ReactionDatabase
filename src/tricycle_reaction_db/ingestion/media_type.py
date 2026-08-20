"""Content-aware MIME detection shared by all artifact ingestion paths."""

from __future__ import annotations

import mimetypes
from pathlib import PurePosixPath

TEXT_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/x-yaml",
        "chemical/x-gaussian-log",
        "chemical/x-mdl-molfile",
    }
)
TEXT_SUFFIX_MEDIA_TYPES = {
    ".cfg": "text/plain",
    ".com": "text/plain",
    ".csv": "text/csv",
    ".dat": "text/plain",
    ".err": "text/plain",
    ".gjf": "text/plain",
    ".inp": "text/plain",
    ".input": "text/plain",
    ".json": "application/json",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".out": "text/plain",
    ".stdout": "text/plain",
    ".stderr": "text/plain",
    ".tsv": "text/tab-separated-values",
    ".txt": "text/plain",
    ".xml": "application/xml",
    ".xyz": "text/plain",
    ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
}
_GENERIC_MEDIA_TYPES = {"", "application/octet-stream", "binary/octet-stream"}
_BINARY_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x1f\x8b", "application/gzip"),
    (b"BZh", "application/x-bzip2"),
    (b"\xfd7zXZ\x00", "application/x-xz"),
    (b"\x7fELF", "application/x-elf"),
    (b"\x89HDF\r\n\x1a\n", "application/x-hdf5"),
)


def _looks_like_utf8_text(payload: bytes) -> bool:
    sample = payload[:64 * 1024]
    if not sample or b"\x00" in sample:
        return False
    if any(sample.startswith(signature) for signature, _ in _BINARY_SIGNATURES):
        return False
    try:
        text = sample.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    control_count = sum(
        character < " " and character not in "\n\r\t\f\b" for character in text
    )
    return control_count <= max(1, len(text) // 100)


def _signature_media_type(payload: bytes) -> str | None:
    for signature, media_type in _BINARY_SIGNATURES:
        if payload.startswith(signature):
            return media_type
    return None


def is_text_media_type(media_type: str) -> bool:
    normalized = media_type.partition(";")[0].strip().lower()
    return normalized.startswith("text/") or normalized in TEXT_MEDIA_TYPES


def detect_artifact_media_type(
    filename: str,
    media_type: str | None,
    payload: bytes | None = None,
) -> str:
    """Resolve MIME from declared type, filename, and content evidence."""

    normalized = (media_type or "").partition(";")[0].strip().lower()
    if payload is not None:
        signature_type = _signature_media_type(payload)
        if signature_type is not None:
            return signature_type
        if _looks_like_utf8_text(payload):
            suffix = PurePosixPath(filename).suffix.lower()
            if suffix in TEXT_SUFFIX_MEDIA_TYPES:
                return TEXT_SUFFIX_MEDIA_TYPES[suffix]
            if is_text_media_type(normalized):
                return normalized
            return "text/plain"
        if normalized in _GENERIC_MEDIA_TYPES:
            return "application/octet-stream"
        return normalized
    if normalized not in _GENERIC_MEDIA_TYPES:
        return normalized
    guessed_type, _ = mimetypes.guess_type(filename)
    return guessed_type or "application/octet-stream"


__all__ = ["TEXT_MEDIA_TYPES", "detect_artifact_media_type", "is_text_media_type"]
