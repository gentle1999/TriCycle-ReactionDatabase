from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import httpx

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts/probe_upload_limit.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("probe_upload_limit", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upload_limit_probe_requires_stable_preflight_413() -> None:
    module = _load_script()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/artifacts"
        return httpx.Response(
            413,
            request=request,
            headers={"X-Upload-Rejection-Stage": "preflight"},
            json={"detail": "uploaded artifact exceeds the 8-byte limit"},
        )

    original_client = module.httpx.Client

    class MockClient:
        def __init__(self, **_: object) -> None:
            self._client = original_client(transport=httpx.MockTransport(handler))

        def __enter__(self) -> httpx.Client:
            return self._client

        def post(self, *args: object, **kwargs: object) -> httpx.Response:
            return self._client.post(*args, **kwargs)

        def __exit__(self, *_: object) -> None:
            self._client.close()

    module.httpx.Client = MockClient  # type: ignore[assignment]
    try:
        result = module.probe_upload_limit(
            api_url="https://api.example.test",
            project_id="00000000-0000-7000-8000-000000000201",
            token="token",
            maximum_upload_bytes=8,
        )
    finally:
        module.httpx.Client = original_client

    assert result["succeeded"] is True
    assert len(result["observations"]) == 2


def test_upload_limit_probe_rejects_non_preflight_response() -> None:
    module = _load_script()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(413, request=request, json={"detail": "too large"})

    original_client = module.httpx.Client

    class MockClient:
        def __init__(self, **_: object) -> None:
            self._client = original_client(transport=httpx.MockTransport(handler))

        def __enter__(self) -> httpx.Client:
            return self._client

        def post(self, *args: object, **kwargs: object) -> httpx.Response:
            return self._client.post(*args, **kwargs)

        def __exit__(self, *_: object) -> None:
            self._client.close()

    module.httpx.Client = MockClient  # type: ignore[assignment]
    try:
        result = module.probe_upload_limit(
            api_url="https://api.example.test",
            project_id="00000000-0000-7000-8000-000000000201",
            token="token",
            maximum_upload_bytes=8,
        )
    finally:
        module.httpx.Client = original_client

    assert result["succeeded"] is False
