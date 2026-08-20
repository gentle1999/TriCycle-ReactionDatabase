import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete
from sqlmodel import Session, select

from tricycle_reaction_db.application.services.storage_gc import (
    StorageGarbageCollectionSettings,
    run_incremental_storage_gc,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    StorageGarbageCollectionRun,
    StorageGarbageCollectionState,
)
from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    StorageGarbageCollectionRunStatus,
    StorageStatus,
)
from tricycle_reaction_db.domain.identity import DEVELOPMENT_USER_ID, SYSTEM_PROJECT_ID
from tricycle_reaction_db.storage.rustfs import ListedObject

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


class _FakeStore:
    def __init__(self, bucket: str, objects: list[ListedObject]) -> None:
        self.settings = SimpleNamespace(bucket=bucket)
        self.objects = {item.key: item for item in objects}

    def iter_objects(self, *, prefix: str):
        prefix = f"{prefix.rstrip('/')}/"
        yield from (
            replace(item, bucket=self.settings.bucket)
            for key, item in list(self.objects.items())
            if key.startswith(prefix)
        )

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self.objects


class _FailingStore(_FakeStore):
    def delete(self, key: str) -> None:
        raise RuntimeError(f"delete failed for {key}")


def _listed(key: str, modified_at: datetime, payload: bytes = b"artifact") -> ListedObject:
    return ListedObject(
        bucket="unused",
        key=key,
        size=len(payload),
        etag="etag",
        last_modified=modified_at,
    )


def _artifact(bucket: str, key: str, payload: bytes, status: StorageStatus) -> ArtifactFile:
    return ArtifactFile(
        project_id=SYSTEM_PROJECT_ID,
        created_by_user_id=DEVELOPMENT_USER_ID,
        bucket=bucket,
        object_key=key,
        content_sha256=sha256(payload).hexdigest(),
        size_bytes=len(payload),
        original_filename="test.log",
        media_type="text/plain",
        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
        storage_status=status,
    )


def test_incremental_gc_retains_available_and_deletes_orphans_and_stale_pending() -> None:
    base = datetime.now(UTC) - timedelta(hours=3)
    bucket = f"gc-{uuid4().hex}"
    partition = base.strftime("%Y/%m/%d/%H")
    available_key = f"uploads/{partition}/sha256/aa/available"
    pending_key = f"uploads/{partition}/sha256/bb/pending"
    missing_pending_key = f"uploads/{partition}/sha256/dd/missing-pending"
    orphan_key = f"uploads/{partition}/sha256/cc/orphan"
    available_payload = f"available-{uuid4()}".encode()
    pending_payload = f"pending-{uuid4()}".encode()
    missing_pending_payload = f"missing-pending-{uuid4()}".encode()
    store = _FakeStore(
        bucket,
        [
            _listed(available_key, base + timedelta(minutes=10)),
            _listed(pending_key, base + timedelta(minutes=20)),
            _listed(orphan_key, base + timedelta(minutes=30)),
        ],
    )
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    settings = StorageGarbageCollectionSettings(
        _env_file=None,
        grace_period_seconds=3600,
        initial_lookback_seconds=24 * 3600,
        partition_clock_skew_seconds=0,
    )
    started_at = base + timedelta(hours=3)
    try:
        with engine.connect() as connection:
            with Session(bind=connection) as session:
                available = _artifact(
                    bucket, available_key, available_payload, StorageStatus.AVAILABLE
                )
                pending = _artifact(bucket, pending_key, pending_payload, StorageStatus.PENDING)
                missing_pending = _artifact(
                    bucket,
                    missing_pending_key,
                    missing_pending_payload,
                    StorageStatus.PENDING,
                )
                pending.created_at = base + timedelta(minutes=20)
                missing_pending.created_at = base + timedelta(minutes=25)
                session.add(available)
                session.add(pending)
                session.add(missing_pending)
                session.commit()
            result = run_incremental_storage_gc(
                connection,
                store,  # type: ignore[arg-type]
                settings=settings,
                started_at=started_at,
            )
            assert result.objects_seen == 3
            assert result.objects_deleted == 2
            assert result.objects_retained == 1
            assert available_key in store.objects
            assert pending_key not in store.objects
            assert orphan_key not in store.objects
            with Session(bind=connection) as session:
                assert (
                    session.exec(
                        select(ArtifactFile).where(
                            ArtifactFile.bucket == bucket,
                            ArtifactFile.object_key == pending_key,
                        )
                    ).first()
                    is None
                )
                assert (
                    session.exec(
                        select(ArtifactFile).where(
                            ArtifactFile.bucket == bucket,
                            ArtifactFile.object_key == missing_pending_key,
                        )
                    ).first()
                    is None
                )
                assert (
                    session.exec(
                        select(StorageGarbageCollectionRun).where(
                            StorageGarbageCollectionRun.id == result.run_id
                        )
                    )
                    .one()
                    .status.value
                    == "succeeded"
                )
                state = session.exec(
                    select(StorageGarbageCollectionState).where(
                        StorageGarbageCollectionState.id == result.state_id
                    )
                ).one()
                assert state.watermark_at == result.scan_until
                session.exec(
                    delete(StorageGarbageCollectionRun).where(
                        StorageGarbageCollectionRun.state_id == result.state_id
                    )
                )
                session.delete(state)
                session.commit()
    finally:
        engine.dispose()


def test_failed_gc_does_not_advance_watermark() -> None:
    base = datetime.now(UTC) - timedelta(hours=3)
    bucket = f"gc-failure-{uuid4().hex}"
    partition = base.strftime("%Y/%m/%d/%H")
    key = f"uploads/{partition}/sha256/ff/orphan"
    store = _FailingStore(bucket, [_listed(key, base + timedelta(minutes=10))])
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    settings = StorageGarbageCollectionSettings(
        _env_file=None,
        grace_period_seconds=3600,
        initial_lookback_seconds=24 * 3600,
        partition_clock_skew_seconds=0,
    )
    started_at = base + timedelta(hours=3)
    try:
        with engine.connect() as connection:
            with pytest.raises(RuntimeError, match="delete failed"):
                run_incremental_storage_gc(
                    connection,
                    store,  # type: ignore[arg-type]
                    settings=settings,
                    started_at=started_at,
                )
            with Session(bind=connection) as session:
                state = session.exec(
                    select(StorageGarbageCollectionState).where(
                        StorageGarbageCollectionState.bucket == bucket
                    )
                ).one()
                run = session.exec(
                    select(StorageGarbageCollectionRun).where(
                        StorageGarbageCollectionRun.state_id == state.id
                    )
                ).one()
                assert state.watermark_at == started_at - timedelta(hours=24)
                assert run.status is StorageGarbageCollectionRunStatus.FAILED
                session.exec(
                    delete(StorageGarbageCollectionRun).where(
                        StorageGarbageCollectionRun.state_id == state.id
                    )
                )
                session.delete(state)
                session.commit()
    finally:
        engine.dispose()
