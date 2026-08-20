"""Probe the authenticated upload preflight limit over HTTPS."""

from __future__ import annotations

import argparse
import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from tricycle_reaction_db.core.tls import verified_tls_context

UPLOAD_LIMIT_SCHEMA_VERSION = "upload-limit-probe-v1"


def probe_upload_limit(
    *,
    api_url: str,
    project_id: str,
    token: str,
    maximum_upload_bytes: int,
    attempts: int = 2,
    ca_bundle: str | None = None,
) -> dict[str, Any]:
    parsed = urlsplit(api_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("--api-url must be an HTTPS origin")
    if not project_id or not token:
        raise ValueError("project ID and bearer token are required")
    if maximum_upload_bytes < 1:
        raise ValueError("--maximum-upload-bytes must be positive")
    if attempts < 2:
        raise ValueError("--attempts must be at least 2")

    payload = b"x" * (maximum_upload_bytes + 1)
    observations: list[dict[str, Any]] = []
    with httpx.Client(
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
        follow_redirects=False,
        verify=verified_tls_context(ca_bundle=ca_bundle),
    ) as client:
        for attempt in range(1, attempts + 1):
            response = client.post(
                f"{api_url.rstrip('/')}/api/artifacts",
                data={"project_id": project_id},
                files={"file": ("oversize-upload.bin", payload, "application/octet-stream")},
            )
            try:
                body = response.json()
            except json.JSONDecodeError:
                body = None
            detail = body.get("detail") if isinstance(body, dict) else None
            observations.append(
                {
                    "attempt": attempt,
                    "request_bytes": len(payload),
                    "status_code": response.status_code,
                    "detail": detail,
                    "rejection_stage": response.headers.get("X-Upload-Rejection-Stage"),
                }
            )
    succeeded = (
        all(
            item["status_code"] == 413
            and item["rejection_stage"] == "preflight"
            and isinstance(item["detail"], str)
            and f"{maximum_upload_bytes}-byte limit" in item["detail"]
            for item in observations
        )
        and len(
            {
                (item["status_code"], item["detail"], item["rejection_stage"])
                for item in observations
            }
        )
        == 1
    )
    return {
        "schema_version": UPLOAD_LIMIT_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "probe_node": socket.gethostname(),
        "api_origin": f"{parsed.scheme}://{parsed.netloc}",
        "project_id": project_id,
        "maximum_upload_bytes": maximum_upload_bytes,
        "attempts": attempts,
        "observations": observations,
        "succeeded": succeeded,
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--maximum-upload-bytes", type=int, required=True)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--token-env", default="TRICYCLE_ACCEPTANCE_BEARER_TOKEN")
    parser.add_argument("--ca-bundle", default=None)
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    token = os.getenv(arguments.token_env, "")
    if not token:
        raise SystemExit(f"bearer token environment variable is empty: {arguments.token_env}")
    try:
        result = probe_upload_limit(
            api_url=arguments.api_url,
            project_id=arguments.project_id,
            token=token,
            maximum_upload_bytes=arguments.maximum_upload_bytes,
            attempts=arguments.attempts,
            ca_bundle=arguments.ca_bundle,
        )
    except Exception as error:
        result = {
            "schema_version": UPLOAD_LIMIT_SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "probe_node": socket.gethostname(),
            "succeeded": False,
            "error": f"{type(error).__name__}: {error}",
        }
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if arguments.output is None:
        print(serialized, end="")
    else:
        arguments.output.write_text(serialized, encoding="utf-8")
    if result.get("succeeded") is not True:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
