"""Probe shared API-node rate limits or fail-closed behavior over HTTPS."""

from __future__ import annotations

import argparse
import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx

from tricycle_reaction_db.core.tls import verified_tls_context

SHARED_RATE_LIMIT_SCHEMA_VERSION = "shared-rate-limit-v1"
FAIL_CLOSED_SCHEMA_VERSION = "rate-limit-fail-closed-v1"


def _request_url(api_url: str, path: str) -> str:
    return urljoin(f"{api_url.rstrip('/')}/", path.lstrip("/"))


def _origins(api_urls: list[str]) -> list[str]:
    origins: list[str] = []
    for api_url in api_urls:
        parsed = urlsplit(api_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError(f"API node URL must be an HTTPS origin: {api_url}")
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in origins:
            raise ValueError(f"duplicate API node origin: {origin}")
        origins.append(origin)
    if len(origins) < 2:
        raise ValueError("at least two distinct --api-url values are required")
    return origins


def _integer_header(response: httpx.Response, name: str) -> int:
    value = response.headers.get(name)
    if value is None:
        raise RuntimeError(f"{response.request.url} response is missing {name}")
    try:
        parsed = int(value)
    except ValueError as error:
        raise RuntimeError(f"{response.request.url} returned invalid {name}={value!r}") from error
    if parsed < 0:
        raise RuntimeError(f"{response.request.url} returned negative {name}={parsed}")
    return parsed


def _probe_shared(
    client: httpx.Client,
    *,
    origins: list[str],
    path: str,
    maximum_requests: int,
) -> dict[str, object]:
    observations: list[dict[str, object]] = []
    previous_remaining: int | None = None
    rate_limit: int | None = None
    policy: str | None = None
    seen_origins: set[str] = set()
    shared_budget = True
    rejection_observed = False

    for index in range(maximum_requests + 1):
        origin = origins[index % len(origins)]
        response = client.get(_request_url(origin, path))
        seen_origins.add(origin)
        current_policy = response.headers.get("X-RateLimit-Policy")
        current_limit = _integer_header(response, "X-RateLimit-Limit")
        remaining = _integer_header(response, "X-RateLimit-Remaining")
        if policy is None:
            policy = current_policy
            rate_limit = current_limit
        elif current_policy != policy or current_limit != rate_limit:
            shared_budget = False
        observations.append(
            {
                "sequence": index + 1,
                "origin": origin,
                "status_code": response.status_code,
                "remaining": remaining,
                "policy": current_policy,
            }
        )
        if response.status_code == 429:
            rejection_observed = True
            if remaining != 0 or previous_remaining != 0:
                shared_budget = False
            break
        if previous_remaining is not None and remaining != previous_remaining - 1:
            shared_budget = False
        previous_remaining = remaining

    all_nodes_observed = seen_origins == set(origins)
    succeeded = bool(
        shared_budget
        and rejection_observed
        and all_nodes_observed
        and policy
        and rate_limit is not None
    )
    return {
        "schema_version": SHARED_RATE_LIMIT_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "probe_node": socket.gethostname(),
        "api_node_count": len(origins),
        "api_origins": origins,
        "path": path,
        "policy": policy,
        "limit": rate_limit,
        "observations": observations,
        "shared_budget": shared_budget,
        "rejection_observed": rejection_observed,
        "all_nodes_observed": all_nodes_observed,
        "tls": True,
        "succeeded": succeeded,
    }


def _probe_fail_closed(
    client: httpx.Client,
    *,
    origins: list[str],
    path: str,
) -> dict[str, object]:
    observations: list[dict[str, object]] = []
    for origin in origins:
        response = client.get(_request_url(origin, path))
        try:
            payload = response.json()
        except json.JSONDecodeError:
            payload = None
        detail = payload.get("detail") if isinstance(payload, dict) else None
        code = detail.get("code") if isinstance(detail, dict) else None
        observations.append(
            {
                "origin": origin,
                "status_code": response.status_code,
                "error_code": code,
                "cache_control": response.headers.get("Cache-Control"),
                "retry_after": response.headers.get("Retry-After"),
            }
        )
    fail_closed = all(
        item["status_code"] == 503
        and item["error_code"] == "rate_limit_backend_unavailable"
        and item["cache_control"] == "no-store"
        and item["retry_after"] == "1"
        for item in observations
    )
    return {
        "schema_version": FAIL_CLOSED_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "probe_node": socket.gethostname(),
        "api_node_count": len(origins),
        "api_origins": origins,
        "path": path,
        "observations": observations,
        "fail_closed": fail_closed,
        "tls": True,
        "succeeded": fail_closed,
    }


def probe_rate_limit(
    *,
    mode: Literal["shared", "fail-closed"],
    api_urls: list[str],
    path: str,
    token: str,
    ca_bundle: str | None,
    maximum_requests: int,
) -> dict[str, object]:
    origins = _origins(api_urls)
    if not path.startswith("/"):
        raise ValueError("--path must start with /")
    if not token:
        raise ValueError("bearer token must not be empty")
    with httpx.Client(
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
        follow_redirects=False,
        verify=verified_tls_context(ca_bundle=ca_bundle),
    ) as client:
        if mode == "shared":
            return _probe_shared(
                client,
                origins=origins,
                path=path,
                maximum_requests=maximum_requests,
            )
        return _probe_fail_closed(client, origins=origins, path=path)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("shared", "fail-closed"), required=True)
    parser.add_argument("--api-url", action="append", required=True)
    parser.add_argument("--path", default="/api/topologies?limit=1")
    parser.add_argument("--token-env", default="TRICYCLE_ACCEPTANCE_BEARER_TOKEN")
    parser.add_argument("--ca-bundle", default=None)
    parser.add_argument("--maximum-requests", type=int, default=1000)
    parser.add_argument("--output", type=Path, default=None)
    arguments = parser.parse_args()
    if arguments.maximum_requests < 2:
        parser.error("--maximum-requests must be at least 2")
    return arguments


def main() -> None:
    arguments = _arguments()
    token = os.getenv(arguments.token_env, "")
    if not token:
        raise SystemExit(f"bearer token environment variable is empty: {arguments.token_env}")
    try:
        result = probe_rate_limit(
            mode=arguments.mode,
            api_urls=arguments.api_url,
            path=arguments.path,
            token=token,
            ca_bundle=arguments.ca_bundle,
            maximum_requests=arguments.maximum_requests,
        )
    except Exception as error:
        result = {
            "schema_version": (
                SHARED_RATE_LIMIT_SCHEMA_VERSION
                if arguments.mode == "shared"
                else FAIL_CLOSED_SCHEMA_VERSION
            ),
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
