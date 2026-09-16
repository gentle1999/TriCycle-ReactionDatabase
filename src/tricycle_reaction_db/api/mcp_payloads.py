"""Shared payload decoding rules for MCP file transports."""

from __future__ import annotations

import base64
import binascii

from tricycle_reaction_db.application.services.artifact_uploads import (
    ArtifactUploadLimitError,
)


def decode_base64_payload(
    content_base64: str,
    *,
    maximum_bytes: int,
    payload_description: str,
) -> bytes:
    """Decode a standard Base64 payload while enforcing its decoded-size budget.

    MCP Apps' ``DropZone`` and the direct MCP upload tool use the same wire
    representation. Keeping validation here prevents one transport from
    accepting a payload that the other transport would reject.
    """

    encoded = content_base64.strip()
    if not encoded:
        raise ValueError("content_base64 must not be empty")
    maximum_encoded_length = 4 * ((maximum_bytes + 2) // 3)
    if len(encoded) > maximum_encoded_length:
        raise ArtifactUploadLimitError(
            f"encoded {payload_description} exceeds the {maximum_bytes}-byte limit"
        )
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, UnicodeError, ValueError) as error:
        raise ValueError("content_base64 must be valid standard base64") from error
    if not payload:
        raise ValueError(f"uploaded {payload_description} is empty")
    return payload


__all__ = ["decode_base64_payload"]
