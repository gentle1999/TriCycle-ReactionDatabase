from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from tricycle_reaction_db.application.services import units_ts_dataset_export as dataset_export
from tricycle_reaction_db.dev import units_ts_dataset_worker as dataset_worker
from tricycle_reaction_db.domain.enums import UnitsDatasetExportJobStatus

PROJECT_ID = UUID("00000000-0000-7000-8000-000000000401")
LEGACY_JOB_ID = UUID("00000000-0000-7000-8000-000000000402")
CURRENT_JOB_ID = UUID("00000000-0000-7000-8000-000000000403")
ORPHAN_OBJECT_ID = UUID("00000000-0000-7000-8000-000000000404")
CURRENT_LEASE_ID = UUID("00000000-0000-7000-8000-000000000405")


class _Result:
    def __init__(self, rows: list[object] | None = None, *, rowcount: int = 0) -> None:
        self._rows = rows or []
        self.rowcount = rowcount

    def all(self) -> list[object]:
        return self._rows


class _Session:
    def __init__(self, rows: list[object] | None = None) -> None:
        self._query_rows = rows
        self.statements: list[object] = []
        self.commits = 0

    async def exec(self, statement: object) -> _Result:
        self.statements.append(statement)
        if self._query_rows is not None:
            rows, self._query_rows = self._query_rows, None
            return _Result(rows)
        return _Result(rowcount=1)

    async def commit(self) -> None:
        self.commits += 1


class _Settings:
    def __init__(self, bucket: str = "default") -> None:
        self.bucket = bucket

    def model_copy(self, *, update: dict[str, str]) -> _Settings:
        return _Settings(bucket=update["bucket"])


def test_legacy_key_detector_excludes_the_current_nested_key_layout() -> None:
    legacy_key = f"{dataset_export.LEGACY_DATASET_EXPORT_PREFIX}{PROJECT_ID}/{LEGACY_JOB_ID}.npy"
    current_key = (
        f"{dataset_export.LEGACY_DATASET_EXPORT_PREFIX}"
        f"{PROJECT_ID}/{CURRENT_JOB_ID}/{CURRENT_LEASE_ID}.npy"
    )

    assert dataset_export._is_legacy_units_ts_dataset_object_key(legacy_key)
    assert not dataset_export._is_legacy_units_ts_dataset_object_key(current_key)


@pytest.mark.asyncio
async def test_purge_removes_legacy_files_and_jobs_but_preserves_current_exports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    legacy_key = f"{dataset_export.LEGACY_DATASET_EXPORT_PREFIX}{PROJECT_ID}/{LEGACY_JOB_ID}.npy"
    current_key = (
        f"{dataset_export.LEGACY_DATASET_EXPORT_PREFIX}"
        f"{PROJECT_ID}/{CURRENT_JOB_ID}/{CURRENT_LEASE_ID}.npy"
    )
    orphan_key = f"{dataset_export.LEGACY_DATASET_EXPORT_PREFIX}{PROJECT_ID}/{ORPHAN_OBJECT_ID}.npy"
    legacy_job = SimpleNamespace(
        id=LEGACY_JOB_ID,
        project_id=PROJECT_ID,
        object_key=legacy_key,
        bucket="legacy-bucket",
        status=UnitsDatasetExportJobStatus.COMPLETED,
        expires_at=now + timedelta(days=7),
        updated_at=now,
    )
    current_job = SimpleNamespace(
        id=CURRENT_JOB_ID,
        project_id=PROJECT_ID,
        object_key=current_key,
        bucket="current-bucket",
        status=UnitsDatasetExportJobStatus.COMPLETED,
        expires_at=now + timedelta(days=7),
        updated_at=now,
    )
    query_session = _Session([legacy_job, current_job])
    delete_session = _Session()
    sessions = iter((query_session, delete_session))

    @asynccontextmanager
    async def fake_session_factory():
        yield next(sessions)

    class _ObjectStore:
        def __init__(self, _settings: _Settings) -> None:
            pass

        def __enter__(self) -> _ObjectStore:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def iter_objects(self, *, prefix: str) -> list[SimpleNamespace]:
            assert prefix == dataset_export.LEGACY_DATASET_EXPORT_PREFIX
            return [
                SimpleNamespace(bucket="legacy-bucket", key=legacy_key),
                SimpleNamespace(bucket="legacy-bucket", key=orphan_key),
                SimpleNamespace(bucket="current-bucket", key=current_key),
            ]

    deleted_objects: list[tuple[str, str]] = []

    def fake_delete_export_object(settings: _Settings, object_key: str) -> None:
        deleted_objects.append((settings.bucket, object_key))

    monkeypatch.setattr(dataset_export, "session_factory", fake_session_factory)
    monkeypatch.setattr(dataset_export, "RustFSSettings", _Settings)
    monkeypatch.setattr(dataset_export, "RustFSObjectStore", _ObjectStore)
    monkeypatch.setattr(dataset_export, "_delete_export_object", fake_delete_export_object)

    removed_count = await dataset_export.purge_legacy_units_ts_dataset_exports()

    assert removed_count == 2
    assert set(deleted_objects) == {
        ("legacy-bucket", legacy_key),
        ("legacy-bucket", orphan_key),
    }
    assert legacy_job.status is UnitsDatasetExportJobStatus.EXPIRED
    assert legacy_job.expires_at <= datetime.now(UTC)
    assert current_job.status is UnitsDatasetExportJobStatus.COMPLETED
    assert query_session.commits == 1
    assert delete_session.commits == 1
    assert len(delete_session.statements) == 1
    assert "units_ts_dataset_export_job" in str(delete_session.statements[0])


@pytest.mark.asyncio
async def test_dataset_worker_purges_legacy_exports_when_it_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_event = asyncio.Event()
    calls: list[str] = []

    async def purge_legacy() -> int:
        calls.append("purge")
        return 0

    async def expire_exports() -> int:
        calls.append("expire")
        stop_event.set()
        return 0

    async def claim_job() -> None:
        calls.append("claim")

    async def dispose_engine() -> None:
        calls.append("dispose")

    monkeypatch.setattr(
        dataset_worker,
        "get_settings",
        lambda: SimpleNamespace(units_dataset_worker_poll_interval_seconds=0.01),
    )
    monkeypatch.setattr(dataset_worker, "purge_legacy_units_ts_dataset_exports", purge_legacy)
    monkeypatch.setattr(dataset_worker, "expire_units_ts_dataset_exports", expire_exports)
    monkeypatch.setattr(dataset_worker, "claim_units_ts_dataset_export_job", claim_job)
    monkeypatch.setattr(dataset_worker, "dispose_engine", dispose_engine)

    await dataset_worker.UnitsTsDatasetWorker().run(stop_event)

    assert calls == ["purge", "expire", "claim", "dispose"]
