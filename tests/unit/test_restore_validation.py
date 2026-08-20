from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest

from tricycle_reaction_db.db.models import ArtifactFile
from tricycle_reaction_db.dev.restore_validation import (
    RESTORE_VALIDATION_SCHEMA_VERSION,
    _artifact_manifest_digest,
    _manifest_mismatches,
    _validate_artifact,
    validate_restore,
)
from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    ArtifactVisibility,
    StorageStatus,
)
from tricycle_reaction_db.domain.identity import SYSTEM_PROJECT_ID, SYSTEM_USER_ID
from tricycle_reaction_db.storage.rustfs import ObjectMetadata


class _RestoreStore:
    def __init__(self, payload: bytes, *, version_id: str | None) -> None:
        self.payload = payload
        self.version_id = version_id
        self.requests: list[tuple[str, str | None]] = []

    def head(self, key: str, *, version_id: str | None = None) -> ObjectMetadata:
        self.requests.append((key, version_id))
        return ObjectMetadata(
            bucket="restore-bucket",
            key=key,
            version_id=self.version_id,
            size=len(self.payload),
            etag="etag",
            last_modified=datetime(2026, 8, 19, tzinfo=UTC),
            content_type="text/plain",
            sha256=sha256(self.payload).hexdigest(),
        )

    def iter_bytes(
        self,
        key: str,
        *,
        version_id: str | None = None,
    ):
        self.requests.append((key, version_id))
        yield self.payload[:3]
        yield self.payload[3:]


def _artifact(payload: bytes, *, version_id: str | None) -> ArtifactFile:
    return ArtifactFile(
        id=UUID("00000000-0000-7000-8000-000000000901"),
        project_id=SYSTEM_PROJECT_ID,
        created_by_user_id=SYSTEM_USER_ID,
        visibility=ArtifactVisibility.PROJECT,
        bucket="restore-bucket",
        object_key="uploads/restored.log",
        version_id=version_id,
        content_sha256=sha256(payload).hexdigest(),
        size_bytes=len(payload),
        original_filename="restored.log",
        media_type="text/plain",
        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
        storage_status=StorageStatus.AVAILABLE,
    )


def test_restore_validator_streams_the_exact_version_and_hashes_all_bytes() -> None:
    payload = b"restored artifact\n"
    artifact = _artifact(payload, version_id="version-1")
    store = _RestoreStore(payload, version_id="version-1")

    checked_bytes, failure = _validate_artifact(
        artifact,
        store=store,  # type: ignore[arg-type]
        versioning_enabled=True,
    )

    assert checked_bytes == len(payload)
    assert failure is None
    assert store.requests == [
        (artifact.object_key, "version-1"),
        (artifact.object_key, "version-1"),
    ]


def test_restore_validator_rejects_missing_version_id_for_versioned_bucket() -> None:
    payload = b"restored artifact\n"
    artifact = _artifact(payload, version_id=None)
    store = _RestoreStore(payload, version_id=None)

    checked_bytes, failure = _validate_artifact(
        artifact,
        store=store,  # type: ignore[arg-type]
        versioning_enabled=True,
    )

    assert checked_bytes == 0
    assert failure is not None
    assert failure.reason == "versioned bucket artifact is missing version_id"
    assert store.requests == []


def test_artifact_manifest_digest_sorts_records_and_includes_identity_fields() -> None:
    first = _artifact(b"first", version_id="version-1")
    second = _artifact(b"second", version_id="version-2").model_copy(
        update={
            "id": UUID("00000000-0000-7000-8000-000000000902"),
            "object_key": "uploads/second.log",
        }
    )

    assert _artifact_manifest_digest([first, second]) == _artifact_manifest_digest([second, first])
    assert _artifact_manifest_digest([first]) != _artifact_manifest_digest(
        [first.model_copy(update={"size_bytes": first.size_bytes + 1})]
    )


def test_manifest_comparison_reports_source_drift() -> None:
    expected = {
        "schema_version": RESTORE_VALIDATION_SCHEMA_VERSION,
        "succeeded": True,
        "alembic_version": "0001_initial_schema",
        "row_counts": {"artifact_file": 2},
        "storage_status_counts": {"available": 2},
        "available_artifact_count": 2,
        "checked_artifact_count": 2,
        "checked_artifact_bytes": 12,
        "artifact_manifest_digest": "source-digest",
    }

    assert (
        _manifest_mismatches(
            expected,
            alembic_version="0001_initial_schema",
            row_counts={"artifact_file": 2},
            storage_status_counts={"available": 2},
            available_artifact_count=2,
            checked_artifact_count=2,
            checked_artifact_bytes=12,
            artifact_manifest_digest="source-digest",
        )
        == ()
    )
    assert _manifest_mismatches(
        expected,
        alembic_version="0001_initial_schema",
        row_counts={"artifact_file": 1},
        storage_status_counts={"available": 2},
        available_artifact_count=1,
        checked_artifact_count=1,
        checked_artifact_bytes=6,
        artifact_manifest_digest="restored-digest",
    ) == (
        "row_counts expected={'artifact_file': 2} got={'artifact_file': 1}",
        "available_artifact_count expected=2 got=1",
        "checked_artifact_count expected=2 got=1",
        "checked_artifact_bytes expected=12 got=6",
        "artifact_manifest_digest expected='source-digest' got='restored-digest'",
    )


def test_restore_validator_rejects_partial_expected_manifest_validation() -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        validate_restore(
            max_artifacts=1,
            expected_manifest=Path("source-manifest.json"),
        )
