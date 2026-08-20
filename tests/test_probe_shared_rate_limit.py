from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import httpx

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts/probe_shared_rate_limit.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("probe_shared_rate_limit", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shared_probe_observes_one_budget_across_two_origins() -> None:
    module = _load_script()
    origins = ["https://api-01.example.test", "https://api-02.example.test"]
    responses = iter(
        [
            (200, "2"),
            (200, "1"),
            (200, "0"),
            (429, "0"),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        status, remaining = next(responses)
        return httpx.Response(
            status,
            request=request,
            headers={
                "X-RateLimit-Limit": "3",
                "X-RateLimit-Remaining": remaining,
                "X-RateLimit-Policy": "read",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = module._probe_shared(
            client,
            origins=origins,
            path="/api/topologies?limit=1",
            maximum_requests=10,
        )

    assert result["shared_budget"] is True
    assert result["all_nodes_observed"] is True
    assert result["succeeded"] is True


def test_shared_probe_rejects_independent_node_counters() -> None:
    module = _load_script()
    responses = iter([(200, "2"), (200, "2"), (200, "1"), (429, "0")])

    def handler(request: httpx.Request) -> httpx.Response:
        status, remaining = next(responses)
        return httpx.Response(
            status,
            request=request,
            headers={
                "X-RateLimit-Limit": "3",
                "X-RateLimit-Remaining": remaining,
                "X-RateLimit-Policy": "read",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = module._probe_shared(
            client,
            origins=["https://api-01.example.test", "https://api-02.example.test"],
            path="/api/topologies?limit=1",
            maximum_requests=10,
        )

    assert result["shared_budget"] is False
    assert result["succeeded"] is False


def test_fail_closed_probe_requires_stable_503_contract_on_every_node() -> None:
    module = _load_script()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            request=request,
            headers={"Cache-Control": "no-store", "Retry-After": "1"},
            json={"detail": {"code": "rate_limit_backend_unavailable"}},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = module._probe_fail_closed(
            client,
            origins=["https://api-01.example.test", "https://api-02.example.test"],
            path="/api/topologies?limit=1",
        )

    assert result["fail_closed"] is True
    assert result["succeeded"] is True
