from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from tricycle_reaction_db.application.services.artifact_content import (
    artifact_preview_available,
)
from tricycle_reaction_db.ingestion.media_type import detect_artifact_media_type
from tricycle_reaction_db.storage.rustfs import (
    RustFSObjectStore,
    RustFSSettings,
    content_addressed_key,
    time_partitioned_content_addressed_key,
)

PROJECT_ROOT = Path(__file__).parents[2]


def test_content_addressed_key_uses_sha256_fanout() -> None:
    assert content_addressed_key(b"Gaussian log\n") == (
        "raw/sha256/9e/9ee66716f16ef5d7a5729e63f6c2c6ddec99cb39d912e9a62d3616e757bc0c9f"
    )


def test_time_partitioned_content_addressed_key_is_hourly_and_stable() -> None:
    uploaded_at = datetime(2026, 8, 10, 12, 34, 56, tzinfo=UTC)
    key = time_partitioned_content_addressed_key(b"Gaussian log\n", uploaded_at=uploaded_at)
    assert key == (
        "uploads/2026/08/10/12/sha256/9e/"
        "9ee66716f16ef5d7a5729e63f6c2c6ddec99cb39d912e9a62d3616e757bc0c9f"
    )


class _Paginator:
    def __init__(self, pages: list[dict[str, object]]) -> None:
        self.pages = pages
        self.prefixes: list[str] = []

    def paginate(self, *, Bucket: str, Prefix: str) -> list[dict[str, object]]:
        self.prefixes.append(Prefix)
        return self.pages


class _ListClient:
    def __init__(self) -> None:
        self.paginator = _Paginator(
            [
                {
                    "Contents": [
                        {
                            "Key": "uploads/2026/08/10/12/sha256/aa/digest",
                            "Size": 12,
                            "ETag": '"etag"',
                            "LastModified": datetime(2026, 8, 10, 12, tzinfo=UTC),
                        }
                    ]
                },
                {},
            ]
        )

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "list_objects_v2"
        return self.paginator

    def close(self) -> None:
        pass


class _Body:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self, maximum: int | None = None) -> bytes:
        return self.payload if maximum is None else self.payload[:maximum]

    def iter_chunks(self, *, chunk_size: int):
        for offset in range(0, len(self.payload), chunk_size):
            yield self.payload[offset : offset + chunk_size]

    def close(self) -> None:
        pass


class _VersionedClient:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.requests: list[tuple[str, dict[str, object]]] = []

    def put_object(self, **request: object) -> dict[str, str]:
        self.requests.append(("put", request))
        return {"VersionId": "version-1"}

    def head_object(self, **request: object) -> dict[str, object]:
        self.requests.append(("head", request))
        return {
            "ContentLength": len(self.payload),
            "ETag": '"etag"',
            "LastModified": datetime(2026, 8, 19, tzinfo=UTC),
            "ContentType": "text/plain",
            "Metadata": {"sha256": sha256(self.payload).hexdigest()},
            "VersionId": "version-1",
        }

    def get_object(self, **request: object) -> dict[str, object]:
        self.requests.append(("get", request))
        return {
            "Body": _Body(self.payload),
            "Metadata": {"sha256": sha256(self.payload).hexdigest()},
            "VersionId": "version-1",
        }

    def delete_object(self, **request: object) -> dict[str, object]:
        self.requests.append(("delete", request))
        return {}

    def close(self) -> None:
        pass


def test_iter_objects_uses_list_objects_v2_pagination() -> None:
    client = _ListClient()
    store = RustFSObjectStore(
        RustFSSettings(_env_file=None),
        client=client,  # type: ignore[arg-type]
    )
    listed = list(store.iter_objects(prefix="uploads/2026/08/10/12"))
    assert listed[0].key.endswith("/digest")
    assert listed[0].size == 12
    assert listed[0].etag == "etag"
    assert client.paginator.prefixes == ["uploads/2026/08/10/12/"]


def test_versioned_put_and_reads_use_the_exact_s3_version() -> None:
    payload = b"versioned artifact\n"
    client = _VersionedClient(payload)
    store = RustFSObjectStore(
        RustFSSettings(_env_file=None, bucket="versioned-artifacts"),
        client=client,  # type: ignore[arg-type]
    )

    metadata = store.put_bytes(key="raw/versioned.log", payload=payload)

    assert metadata.version_id == "version-1"
    assert store.get_bytes("raw/versioned.log", version_id=metadata.version_id) == payload
    assert (
        store.get_range("raw/versioned.log", max_bytes=7, version_id=metadata.version_id)
        == payload[:7]
    )
    assert (
        b"".join(
            store.iter_bytes("raw/versioned.log", chunk_size=3, version_id=metadata.version_id)
        )
        == payload
    )
    versioned_requests = [request for operation, request in client.requests if operation != "put"]
    assert versioned_requests
    assert all(request["VersionId"] == "version-1" for request in versioned_requests)
    store.delete("raw/versioned.log", version_id=metadata.version_id)
    assert client.requests[-1] == (
        "delete",
        {"Bucket": "versioned-artifacts", "Key": "raw/versioned.log", "VersionId": "version-1"},
    )


def test_put_file_streams_an_inspected_spool_source(tmp_path: Path) -> None:
    payload = b"streamed artifact\n"
    source = tmp_path / "artifact.log"
    source.write_bytes(payload)
    client = _VersionedClient(payload)
    store = RustFSObjectStore(
        RustFSSettings(_env_file=None, bucket="streamed-artifacts"),
        client=client,  # type: ignore[arg-type]
    )

    metadata = store.put_file(
        key="raw/streamed.log",
        path=source,
        content_sha256=sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )

    request = client.requests[0][1]
    assert metadata.size == len(payload)
    assert request["ContentLength"] == len(payload)
    assert not isinstance(request["Body"], bytes)


def test_rustfs_settings_reject_relative_endpoint() -> None:
    with pytest.raises(ValidationError):
        RustFSSettings(_env_file=None, endpoint_url="rustfs:9000")


def test_rustfs_settings_accept_private_ca_bundle() -> None:
    settings = RustFSSettings(
        _env_file=None,
        endpoint_url="https://s3.internal.example:9000",
        ca_bundle="/etc/reaction-database/ca/internal-ca.pem",
    )

    assert settings.verify_tls is True
    assert settings.ca_bundle == "/etc/reaction-database/ca/internal-ca.pem"


def test_rustfs_settings_reject_relative_ca_bundle() -> None:
    with pytest.raises(ValidationError, match="absolute PEM path"):
        RustFSSettings(_env_file=None, ca_bundle="relative-ca.pem")


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"endpoint_url": "http://objects.example.test"}, "HTTPS endpoint"),
        ({"verify_tls": False}, "TLS verification"),
    ],
)
def test_rustfs_settings_fail_closed_for_production_scheduler_transport(
    monkeypatch: pytest.MonkeyPatch,
    updates: dict[str, object],
    message: str,
) -> None:
    monkeypatch.setenv("TRICYCLE_ENVIRONMENT", "production")

    with pytest.raises(ValidationError, match=message):
        RustFSSettings(
            _env_file=None,
            **{"endpoint_url": "https://objects.example.test", **updates},
        )


def test_compose_requires_rustfs_disk_compression() -> None:
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "RUSTFS_COMPRESSION_ENABLED: ${RUSTFS_COMPRESSION_ENABLED:-true}" in compose
    assert 'test "$${RUSTFS_COMPRESSION_ENABLED}" = true' in compose


@pytest.mark.parametrize(
    ("media_type", "expected"),
    [
        ("text/plain", True),
        ("text/csv; charset=utf-8", True),
        ("application/json", True),
        ("application/octet-stream", False),
        ("image/png", False),
    ],
)
def test_artifact_preview_availability_follows_media_type(
    media_type: str,
    expected: bool,
) -> None:
    assert artifact_preview_available(media_type) is expected


@pytest.mark.parametrize("filename", ["calculation.log", "run.out", "geometry.xyz"])
def test_known_text_extensions_enable_preview_when_browser_mime_is_generic(
    filename: str,
) -> None:
    assert (
        detect_artifact_media_type(
            filename,
            "application/octet-stream",
            b"text\n",
        )
        == "text/plain"
    )


def test_upload_type_detection_uses_content_before_filename() -> None:
    assert (
        detect_artifact_media_type(
            "calculation.log",
            "application/octet-stream",
            b"Entering Gaussian System\nNormal termination\n",
        )
        == "text/plain"
    )
    assert (
        detect_artifact_media_type(
            "calculation.log",
            "application/octet-stream",
            b"\x89PNG\r\n\x1a\n",
        )
        == "image/png"
    )


def test_upload_type_detection_recognizes_text_without_extension() -> None:
    assert (
        detect_artifact_media_type(
            "README",
            "application/octet-stream",
            b"plain UTF-8 text without a suffix\n",
        )
        == "text/plain"
    )


def test_binary_extensions_remain_unpreviewable_with_generic_mime() -> None:
    assert (
        detect_artifact_media_type(
            "image.png",
            "application/octet-stream",
            b"\x89PNG\r\n\x1a\n",
        )
        == "image/png"
    )
