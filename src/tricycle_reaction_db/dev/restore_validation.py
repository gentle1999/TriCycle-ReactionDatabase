"""Validate a restored PostgreSQL database against its exact S3 object versions."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from sqlalchemy import create_engine, func, text
from sqlmodel import Session, col, select

from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    CalculationFrame,
    Geometry,
    LogicalReaction,
    MappedReaction,
    ScientificArray,
)
from tricycle_reaction_db.domain.enums import StorageStatus
from tricycle_reaction_db.storage.rustfs import RustFSObjectStore, RustFSSettings

RESTORE_VALIDATION_SCHEMA_VERSION = "restore-validation-v1"


@dataclass(frozen=True, slots=True)
class ArtifactValidationFailure:
    artifact_id: str
    bucket: str
    object_key: str
    version_id: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class RestoreValidationResult:
    schema_version: str
    validation_timestamp: str
    database: str
    postgresql_version: str
    rdkit_extension_version: str
    alembic_version: str
    row_counts: dict[str, int]
    storage_status_counts: dict[str, int]
    bucket_versioning: dict[str, str]
    bucket_failures: dict[str, str]
    available_artifact_count: int
    checked_artifact_count: int
    checked_artifact_bytes: int
    artifact_manifest_digest: str
    manifest_mismatches: tuple[str, ...]
    failures: tuple[ArtifactValidationFailure, ...]
    succeeded: bool


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-artifacts",
        type=int,
        default=0,
        help="check at most this many artifacts; 0 checks every available artifact (default: 0)",
    )
    parser.add_argument(
        "--expected-manifest",
        type=Path,
        default=None,
        help=(
            "compare the full restore result with a JSON manifest captured before backup; "
            "cannot be combined with --max-artifacts"
        ),
    )
    arguments = parser.parse_args()
    if arguments.max_artifacts < 0:
        parser.error("--max-artifacts must be non-negative")
    if arguments.expected_manifest is not None and arguments.max_artifacts:
        parser.error("--expected-manifest cannot be combined with --max-artifacts")
    return arguments


def _hash_chunks(chunks: Iterator[bytes]) -> tuple[int, str]:
    digest = sha256()
    size = 0
    for chunk in chunks:
        digest.update(chunk)
        size += len(chunk)
    return size, digest.hexdigest()


def _artifact_manifest_digest(artifacts: Sequence[ArtifactFile]) -> str:
    """Hash the ordered available-Artifact identity used by restore acceptance."""

    records = [
        {
            "id": str(artifact.id),
            "bucket": artifact.bucket,
            "object_key": artifact.object_key,
            "version_id": artifact.version_id,
            "content_sha256": artifact.content_sha256,
            "size_bytes": artifact.size_bytes,
        }
        for artifact in sorted(artifacts, key=lambda item: str(item.id))
    ]
    payload = json.dumps(
        records,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _load_expected_manifest(path: Path) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read expected manifest {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"expected manifest {path} must contain a JSON object")
    return payload


def _manifest_mismatches(
    expected: Mapping[str, object],
    *,
    alembic_version: str,
    row_counts: dict[str, int],
    storage_status_counts: dict[str, int],
    available_artifact_count: int,
    checked_artifact_count: int,
    checked_artifact_bytes: int,
    artifact_manifest_digest: str,
) -> tuple[str, ...]:
    actual: dict[str, object] = {
        "alembic_version": alembic_version,
        "row_counts": row_counts,
        "storage_status_counts": storage_status_counts,
        "available_artifact_count": available_artifact_count,
        "checked_artifact_count": checked_artifact_count,
        "checked_artifact_bytes": checked_artifact_bytes,
        "artifact_manifest_digest": artifact_manifest_digest,
    }
    mismatches: list[str] = []
    expected_schema = expected.get("schema_version")
    if expected_schema != RESTORE_VALIDATION_SCHEMA_VERSION:
        mismatches.append(
            f"schema_version expected={RESTORE_VALIDATION_SCHEMA_VERSION} got={expected_schema!r}"
        )
    if expected.get("succeeded") is not True:
        mismatches.append(f"expected manifest succeeded=true got={expected.get('succeeded')!r}")
    for name, value in actual.items():
        if name not in expected:
            mismatches.append(f"{name} missing from expected manifest")
        elif expected[name] != value:
            mismatches.append(f"{name} expected={expected[name]!r} got={value!r}")
    return tuple(mismatches)


def _validate_artifact(
    artifact: ArtifactFile,
    *,
    store: RustFSObjectStore,
    versioning_enabled: bool,
) -> tuple[int, ArtifactValidationFailure | None]:
    artifact_id = str(artifact.id)
    if versioning_enabled and artifact.version_id is None:
        return 0, ArtifactValidationFailure(
            artifact_id=artifact_id,
            bucket=artifact.bucket,
            object_key=artifact.object_key,
            version_id=None,
            reason="versioned bucket artifact is missing version_id",
        )
    try:
        metadata = store.head(artifact.object_key, version_id=artifact.version_id)
        size, digest = _hash_chunks(
            store.iter_bytes(artifact.object_key, version_id=artifact.version_id)
        )
    except Exception as error:
        return 0, ArtifactValidationFailure(
            artifact_id=artifact_id,
            bucket=artifact.bucket,
            object_key=artifact.object_key,
            version_id=artifact.version_id,
            reason=f"{type(error).__name__}: {error}",
        )
    mismatches: list[str] = []
    if metadata.size != artifact.size_bytes or size != artifact.size_bytes:
        mismatches.append(
            f"size expected={artifact.size_bytes} head={metadata.size} streamed={size}"
        )
    if metadata.sha256 != artifact.content_sha256:
        mismatches.append(
            f"metadata sha256 expected={artifact.content_sha256} got={metadata.sha256}"
        )
    if digest != artifact.content_sha256:
        mismatches.append(f"content sha256 expected={artifact.content_sha256} got={digest}")
    if artifact.version_id is not None and metadata.version_id != artifact.version_id:
        mismatches.append(f"version expected={artifact.version_id} got={metadata.version_id}")
    if not mismatches:
        return size, None
    return size, ArtifactValidationFailure(
        artifact_id=artifact_id,
        bucket=artifact.bucket,
        object_key=artifact.object_key,
        version_id=artifact.version_id,
        reason="; ".join(mismatches),
    )


def validate_restore(
    *,
    max_artifacts: int = 0,
    expected_manifest: Path | None = None,
) -> RestoreValidationResult:
    if max_artifacts < 0:
        raise ValueError("max_artifacts must be non-negative")
    if expected_manifest is not None and max_artifacts:
        raise ValueError("expected_manifest cannot be combined with max_artifacts")
    expected = _load_expected_manifest(expected_manifest) if expected_manifest else None
    settings = get_settings()
    database_engine = create_engine(settings.database_url, pool_pre_ping=True)
    stores: dict[str, RustFSObjectStore] = {}
    try:
        with Session(database_engine) as session:
            database_row = session.execute(
                text(
                    """
                    SELECT
                        current_database(),
                        current_setting('server_version'),
                        COALESCE(
                            (SELECT extversion FROM pg_extension WHERE extname = 'rdkit'),
                            ''
                        ),
                        (SELECT version_num FROM alembic_version)
                    """
                )
            ).one()
            row_counts = {
                "artifact_file": session.exec(select(func.count()).select_from(ArtifactFile)).one(),
                "calculation_frame": session.exec(
                    select(func.count()).select_from(CalculationFrame)
                ).one(),
                "geometry": session.exec(select(func.count()).select_from(Geometry)).one(),
                "logical_reaction": session.exec(
                    select(func.count()).select_from(LogicalReaction)
                ).one(),
                "mapped_reaction": session.exec(
                    select(func.count()).select_from(MappedReaction)
                ).one(),
                "scientific_array": session.exec(
                    select(func.count()).select_from(ScientificArray)
                ).one(),
            }
            storage_status_counts = {
                str(status): int(count)
                for status, count in session.exec(
                    select(ArtifactFile.storage_status, func.count()).group_by(
                        ArtifactFile.storage_status
                    )
                ).all()
            }
            statement = (
                select(ArtifactFile)
                .where(ArtifactFile.storage_status == StorageStatus.AVAILABLE)
                .order_by(col(ArtifactFile.id))
            )
            available_count = storage_status_counts.get(StorageStatus.AVAILABLE.value, 0)
            if max_artifacts:
                statement = statement.limit(max_artifacts)
            artifacts = list(session.exec(statement).all())

        buckets = sorted({artifact.bucket for artifact in artifacts})
        versioning: dict[str, str] = {}
        bucket_failures: dict[str, str] = {}
        for bucket in buckets:
            store_settings = RustFSSettings().model_copy(update={"bucket": bucket})
            store = RustFSObjectStore(store_settings)
            stores[bucket] = store
            try:
                status = store.bucket_versioning_status()
                versioning[bucket] = status or "disabled"
            except Exception as error:
                versioning[bucket] = "unavailable"
                bucket_failures[bucket] = f"{type(error).__name__}: {error}"

        failures: list[ArtifactValidationFailure] = []
        checked_bytes = 0
        for artifact in artifacts:
            if artifact.bucket in bucket_failures:
                failures.append(
                    ArtifactValidationFailure(
                        artifact_id=str(artifact.id),
                        bucket=artifact.bucket,
                        object_key=artifact.object_key,
                        version_id=artifact.version_id,
                        reason=bucket_failures[artifact.bucket],
                    )
                )
                continue
            size, failure = _validate_artifact(
                artifact,
                store=stores[artifact.bucket],
                versioning_enabled=versioning[artifact.bucket] == "Enabled",
            )
            checked_bytes += size
            if failure is not None:
                failures.append(failure)

        invalid_storage_rows = sum(
            storage_status_counts.get(status.value, 0)
            for status in (StorageStatus.PENDING, StorageStatus.MISSING, StorageStatus.CORRUPT)
        )
        checked_artifact_count = len(artifacts)
        checked_artifact_bytes = checked_bytes
        artifact_manifest_digest = _artifact_manifest_digest(artifacts)
        manifest_mismatches = (
            _manifest_mismatches(
                expected,
                alembic_version=str(database_row[3]),
                row_counts=row_counts,
                storage_status_counts=storage_status_counts,
                available_artifact_count=available_count,
                checked_artifact_count=checked_artifact_count,
                checked_artifact_bytes=checked_artifact_bytes,
                artifact_manifest_digest=artifact_manifest_digest,
            )
            if expected is not None
            else ()
        )
        if max_artifacts:
            manifest_mismatches = (
                *manifest_mismatches,
                "partial validation requested; rerun without --max-artifacts",
            )
        return RestoreValidationResult(
            schema_version=RESTORE_VALIDATION_SCHEMA_VERSION,
            validation_timestamp=datetime.now(UTC).isoformat(),
            database=str(database_row[0]),
            postgresql_version=str(database_row[1]),
            rdkit_extension_version=str(database_row[2]),
            alembic_version=str(database_row[3]),
            row_counts=row_counts,
            storage_status_counts=storage_status_counts,
            bucket_versioning=versioning,
            bucket_failures=bucket_failures,
            available_artifact_count=available_count,
            checked_artifact_count=checked_artifact_count,
            checked_artifact_bytes=checked_artifact_bytes,
            artifact_manifest_digest=artifact_manifest_digest,
            manifest_mismatches=manifest_mismatches,
            failures=tuple(failures),
            succeeded=(
                bool(database_row[2])
                and not failures
                and invalid_storage_rows == 0
                and max_artifacts == 0
                and not manifest_mismatches
            ),
        )
    finally:
        for store in stores.values():
            store.close()
        database_engine.dispose()


def main() -> None:
    arguments = _arguments()
    try:
        result = validate_restore(
            max_artifacts=arguments.max_artifacts,
            expected_manifest=arguments.expected_manifest,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(asdict(result), sort_keys=True))
    if not result.succeeded:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
