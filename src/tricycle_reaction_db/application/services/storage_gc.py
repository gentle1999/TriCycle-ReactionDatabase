"""Incremental garbage collection for time-partitioned RustFS uploads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Connection, text
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.services._persistence import (
    _acquire_identity_locks,
    _identity_lock_id,
    _require_id,
)
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    StorageGarbageCollectionRun,
    StorageGarbageCollectionState,
)
from tricycle_reaction_db.domain.enums import (
    StorageGarbageCollectionRunStatus,
    StorageStatus,
)
from tricycle_reaction_db.storage.rustfs import RustFSObjectStore


class StorageGarbageCollectionSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TRICYCLE_STORAGE_GC_",
        extra="ignore",
    )

    root_prefix: str = "uploads"
    grace_period_seconds: int = Field(default=60 * 60, ge=60)
    initial_lookback_seconds: int = Field(default=24 * 60 * 60, ge=60)
    partition_clock_skew_seconds: int = Field(default=60 * 60, ge=0, le=24 * 60 * 60)

    @model_validator(mode="after")
    def validate_gc_window(self) -> StorageGarbageCollectionSettings:
        if not self.root_prefix.strip("/"):
            raise ValueError("root_prefix must not be empty")
        if self.initial_lookback_seconds <= self.grace_period_seconds:
            raise ValueError("initial_lookback_seconds must exceed grace_period_seconds")
        return self


class StorageGarbageCollectionAlreadyRunningError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StorageGarbageCollectionResult:
    run_id: UUID
    state_id: UUID
    status: StorageGarbageCollectionRunStatus
    scan_after: datetime
    scan_until: datetime
    objects_seen: int
    objects_deleted: int
    objects_retained: int
    objects_failed: int


def hourly_partition_prefixes(
    *,
    root_prefix: str,
    scan_after: datetime,
    scan_until: datetime,
    clock_skew: timedelta = timedelta(),
) -> tuple[str, ...]:
    """Return bounded hourly prefixes covering a scan window and clock-skew padding."""

    if scan_after.tzinfo is None or scan_after.utcoffset() is None:
        raise ValueError("scan_after must be timezone-aware")
    if scan_until.tzinfo is None or scan_until.utcoffset() is None:
        raise ValueError("scan_until must be timezone-aware")
    if scan_until < scan_after:
        raise ValueError("scan_until must not precede scan_after")
    if clock_skew < timedelta():
        raise ValueError("clock_skew must not be negative")

    root = root_prefix.strip("/")
    if not root:
        raise ValueError("root_prefix must not be empty")
    cursor = (scan_after.astimezone(UTC) - clock_skew).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    end = (scan_until.astimezone(UTC) + clock_skew).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    prefixes: list[str] = []
    while cursor <= end:
        prefixes.append(f"{root}/{cursor:%Y/%m/%d/%H}")
        cursor += timedelta(hours=1)
    return tuple(prefixes)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("object-store timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _get_or_create_state(
    session: Session,
    *,
    bucket: str,
    root_prefix: str,
    started_at: datetime,
    initial_lookback: timedelta,
) -> StorageGarbageCollectionState:
    state = session.exec(
        select(StorageGarbageCollectionState).where(
            StorageGarbageCollectionState.bucket == bucket,
            StorageGarbageCollectionState.root_prefix == root_prefix,
        )
    ).first()
    if state is None:
        state = StorageGarbageCollectionState(
            bucket=bucket,
            root_prefix=root_prefix,
            watermark_at=started_at - initial_lookback,
            updated_at=started_at,
        )
        session.add(state)
        session.flush()
    return state


def _update_run_counts(
    session: Session,
    run: StorageGarbageCollectionRun,
    *,
    seen: int,
    deleted: int,
    retained: int,
    failed: int,
) -> None:
    run.objects_seen = seen
    run.objects_deleted = deleted
    run.objects_retained = retained
    run.objects_failed = failed
    session.add(run)


def _artifacts_for_object(
    session: Session,
    *,
    bucket: str,
    object_key: str,
) -> list[ArtifactFile]:
    return list(
        session.exec(
            select(ArtifactFile).where(
                ArtifactFile.bucket == bucket,
                ArtifactFile.object_key == object_key,
            )
        ).all()
    )


def _reconcile_stale_pending_rows(
    session: Session,
    store: RustFSObjectStore,
    *,
    bucket: str,
    cutoff: datetime,
) -> int:
    """Remove reservations whose DB commit succeeded but whose upload did not."""

    rows = session.exec(
        select(ArtifactFile).where(
            ArtifactFile.bucket == bucket,
            ArtifactFile.storage_status == StorageStatus.PENDING,
            col(ArtifactFile.created_at).is_not(None),
            col(ArtifactFile.created_at) < cutoff,
        )
    ).all()
    deleted = 0
    for artifact in rows:
        _acquire_identity_locks(session, ("artifact-content", artifact.content_sha256))
        session.refresh(artifact)
        created_at = artifact.created_at
        if (
            artifact.storage_status is not StorageStatus.PENDING
            or artifact.bucket != bucket
            or created_at is None
            or _utc(created_at) >= cutoff
        ):
            continue
        shared_reference = session.exec(
            select(ArtifactFile.id).where(
                ArtifactFile.id != artifact.id,
                ArtifactFile.bucket == artifact.bucket,
                ArtifactFile.object_key == artifact.object_key,
                ArtifactFile.storage_status != StorageStatus.RETIRED,
            )
        ).first()
        if shared_reference is None and store.exists(artifact.object_key):
            store.delete(artifact.object_key)
            deleted += 1
        session.delete(artifact)
        session.commit()
    return deleted


def run_incremental_storage_gc(
    connection: Connection,
    store: RustFSObjectStore,
    *,
    settings: StorageGarbageCollectionSettings | None = None,
    started_at: datetime | None = None,
) -> StorageGarbageCollectionResult:
    """Delete unreferenced objects and advance the watermark only after full success."""

    gc_settings = settings or StorageGarbageCollectionSettings()
    started = _utc(started_at or datetime.now(UTC))
    root_prefix = gc_settings.root_prefix.strip("/")
    lock_id = _identity_lock_id("rustfs-gc", store.settings.bucket, root_prefix)
    acquired = bool(
        connection.execute(
            text("SELECT pg_try_advisory_lock(:lock_id)"),
            {"lock_id": lock_id},
        ).scalar_one()
    )
    if not acquired:
        raise StorageGarbageCollectionAlreadyRunningError(
            f"storage GC is already running for {store.settings.bucket}/{root_prefix}"
        )

    run_id: UUID | None = None
    state_id: UUID | None = None
    seen = deleted = retained = failed = 0
    scan_after = started
    scan_until = started
    try:
        with Session(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="control_fully",
        ) as session:
            state = _get_or_create_state(
                session,
                bucket=store.settings.bucket,
                root_prefix=root_prefix,
                started_at=started,
                initial_lookback=timedelta(seconds=gc_settings.initial_lookback_seconds),
            )
            state_id = _require_id(state, label="StorageGarbageCollectionState")
            scan_after = _utc(state.watermark_at)
            eligible_until = started - timedelta(seconds=gc_settings.grace_period_seconds)
            scan_until = max(scan_after, eligible_until)
            run = StorageGarbageCollectionRun(
                state_id=state_id,
                state=state,
                started_at=started,
                scan_after=scan_after,
                scan_until=scan_until,
                status=StorageGarbageCollectionRunStatus.RUNNING,
            )
            session.add(run)
            session.commit()
            run_id = _require_id(run, label="StorageGarbageCollectionRun")

            try:
                prefixes = hourly_partition_prefixes(
                    root_prefix=root_prefix,
                    scan_after=scan_after,
                    scan_until=scan_until,
                    clock_skew=timedelta(seconds=gc_settings.partition_clock_skew_seconds),
                )
                for prefix in prefixes:
                    for listed_object in store.iter_objects(prefix=prefix):
                        modified_at = _utc(listed_object.last_modified)
                        if modified_at < scan_after or modified_at >= scan_until:
                            continue
                        seen += 1
                        artifacts = _artifacts_for_object(
                            session,
                            bucket=listed_object.bucket,
                            object_key=listed_object.key,
                        )
                        if any(
                            artifact.storage_status is StorageStatus.AVAILABLE
                            for artifact in artifacts
                        ):
                            retained += 1
                            _update_run_counts(
                                session,
                                run,
                                seen=seen,
                                deleted=deleted,
                                retained=retained,
                                failed=failed,
                            )
                            session.commit()
                            continue

                        pending_artifacts = [
                            artifact
                            for artifact in artifacts
                            if artifact.storage_status is StorageStatus.PENDING
                        ]
                        if pending_artifacts:
                            for digest in sorted(
                                {artifact.content_sha256 for artifact in pending_artifacts}
                            ):
                                _acquire_identity_locks(
                                    session,
                                    ("artifact-content", digest),
                                )
                            artifacts = _artifacts_for_object(
                                session,
                                bucket=listed_object.bucket,
                                object_key=listed_object.key,
                            )
                            if any(
                                artifact.storage_status is StorageStatus.AVAILABLE
                                for artifact in artifacts
                            ):
                                retained += 1
                                _update_run_counts(
                                    session,
                                    run,
                                    seen=seen,
                                    deleted=deleted,
                                    retained=retained,
                                    failed=failed,
                                )
                                session.commit()
                                continue
                            pending_artifacts = [
                                artifact
                                for artifact in artifacts
                                if artifact.storage_status is StorageStatus.PENDING
                            ]
                            if any(
                                artifact.created_at is None
                                or _utc(artifact.created_at) >= scan_until
                                for artifact in pending_artifacts
                            ):
                                retained += 1
                                _update_run_counts(
                                    session,
                                    run,
                                    seen=seen,
                                    deleted=deleted,
                                    retained=retained,
                                    failed=failed,
                                )
                                session.commit()
                                continue
                            store.delete(listed_object.key)
                            deleted += 1
                            for artifact in pending_artifacts:
                                session.delete(artifact)
                            _update_run_counts(
                                session,
                                run,
                                seen=seen,
                                deleted=deleted,
                                retained=retained,
                                failed=failed,
                            )
                            session.commit()
                            continue

                        store.delete(listed_object.key)
                        deleted += 1
                        _update_run_counts(
                            session,
                            run,
                            seen=seen,
                            deleted=deleted,
                            retained=retained,
                            failed=failed,
                        )
                        session.commit()

                deleted += _reconcile_stale_pending_rows(
                    session,
                    store,
                    bucket=store.settings.bucket,
                    cutoff=scan_until,
                )
                _update_run_counts(
                    session,
                    run,
                    seen=seen,
                    deleted=deleted,
                    retained=retained,
                    failed=failed,
                )
                session.commit()

                run.status = StorageGarbageCollectionRunStatus.SUCCEEDED
                run.completed_at = datetime.now(UTC)
                _update_run_counts(
                    session,
                    run,
                    seen=seen,
                    deleted=deleted,
                    retained=retained,
                    failed=failed,
                )
                state.watermark_at = scan_until
                state.updated_at = run.completed_at
                state.last_successful_run_id = run_id
                session.add(state)
                session.add(run)
                session.commit()
            except Exception as error:
                failed += 1
                session.rollback()
                failed_run = session.get(StorageGarbageCollectionRun, run_id)
                if failed_run is not None:
                    failed_run.status = StorageGarbageCollectionRunStatus.FAILED
                    failed_run.completed_at = datetime.now(UTC)
                    failed_run.error_message = (str(error) or type(error).__name__)[:8000]
                    _update_run_counts(
                        session,
                        failed_run,
                        seen=seen,
                        deleted=deleted,
                        retained=retained,
                        failed=failed,
                    )
                    session.commit()
                raise

            return StorageGarbageCollectionResult(
                run_id=run_id,
                state_id=state_id,
                status=run.status,
                scan_after=scan_after,
                scan_until=scan_until,
                objects_seen=seen,
                objects_deleted=deleted,
                objects_retained=retained,
                objects_failed=failed,
            )
    finally:
        connection.execute(
            text("SELECT pg_advisory_unlock(:lock_id)"),
            {"lock_id": lock_id},
        )


__all__ = [
    "StorageGarbageCollectionAlreadyRunningError",
    "StorageGarbageCollectionResult",
    "StorageGarbageCollectionSettings",
    "hourly_partition_prefixes",
    "run_incremental_storage_gc",
]
