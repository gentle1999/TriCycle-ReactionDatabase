"""Import an on-disk artifact tree directly into PostgreSQL and RustFS."""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import sys
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID

from sqlalchemy import event

from tricycle_reaction_db.application.services.artifact_uploads import (
    ArtifactUploadPayload,
    ArtifactUploadService,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.session import engine
from tricycle_reaction_db.domain.enums import ArtifactKind

HASH_CHUNK_BYTES = 1024 * 1024
MAX_FINGERPRINT_WORKERS = 32


@dataclass(frozen=True, slots=True)
class ImportCandidate:
    path: Path
    size_bytes: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class ImportFingerprint:
    size_bytes: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ImportSummary:
    scanned: int = 0
    skipped: int = 0
    attempted: int = 0
    succeeded: int = 0
    filtered: int = 0
    failed: int = 0
    bytes_succeeded: int = 0

    def add(self, other: ImportSummary) -> ImportSummary:
        return ImportSummary(
            scanned=self.scanned + other.scanned,
            skipped=self.skipped + other.skipped,
            attempted=self.attempted + other.attempted,
            succeeded=self.succeeded + other.succeeded,
            filtered=self.filtered + other.filtered,
            failed=self.failed + other.failed,
            bytes_succeeded=self.bytes_succeeded + other.bytes_succeeded,
        )


@dataclass(slots=True)
class ImportMetrics:
    """Wall-clock and database measurements for one import invocation."""

    step_timings_ms: dict[str, float] = field(default_factory=dict)
    phase_timings_ms: dict[str, float] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    sql_statement_count: int = 0
    sql_executemany_count: int = 0
    sql_elapsed_ms_by_operation: dict[str, float] = field(default_factory=dict)

    def add_step_timing(self, name: str, elapsed_ms: float) -> None:
        self.step_timings_ms[name] = self.step_timings_ms.get(name, 0.0) + elapsed_ms

    def add_phase_timing(self, name: str, elapsed_ms: float) -> None:
        self.phase_timings_ms[name] = self.phase_timings_ms.get(name, 0.0) + elapsed_ms

    def as_dict(self, *, total_ms: float) -> dict[str, Any]:
        return {
            "total_ms": round(total_ms, 3),
            "step_timings_ms": {
                key: round(value, 3) for key, value in sorted(self.step_timings_ms.items())
            },
            "phase_timings_ms": {
                key: round(value, 3) for key, value in sorted(self.phase_timings_ms.items())
            },
            "steps": self.steps,
            "sql_statement_count": self.sql_statement_count,
            "sql_executemany_count": self.sql_executemany_count,
            "sql_elapsed_ms_by_operation": {
                key: round(value, 3)
                for key, value in sorted(self.sql_elapsed_ms_by_operation.items())
            },
        }


class _SQLStats:
    """Collect aggregate statement counts and elapsed time for an import."""

    def __init__(self) -> None:
        self.count = 0
        self.executemany_count = 0
        self.elapsed_ms_by_operation: dict[str, float] = {}

    def before_cursor_execute(
        self,
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        self.count += 1
        if executemany:
            self.executemany_count += 1
        context._import_timing_started_at = perf_counter()

    def after_cursor_execute(
        self,
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        context: Any,
        _executemany: bool,
    ) -> None:
        started_at = getattr(context, "_import_timing_started_at", None)
        if started_at is None:
            return
        elapsed_ms = (perf_counter() - float(started_at)) * 1000
        operation = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else "EMPTY"
        self.elapsed_ms_by_operation[operation] = (
            self.elapsed_ms_by_operation.get(operation, 0.0) + elapsed_ms
        )


def discover_files(roots: Iterable[Path]) -> list[ImportCandidate]:
    """Return regular files below roots in deterministic order.

    Symlinks are ignored so an import root cannot unexpectedly walk outside the
    explicitly selected tree. Passing a symlink as a root is still allowed and
    resolves that root once before scanning.
    """

    candidates: dict[Path, ImportCandidate] = {}
    for raw_root in roots:
        root = raw_root.expanduser().resolve()
        if not root.exists():
            raise ValueError(f"import path does not exist: {raw_root}")
        paths = [root] if root.is_file() else root.rglob("*")
        for path in paths:
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve()
            stat = resolved.stat()
            candidates.setdefault(
                resolved,
                ImportCandidate(path=resolved, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns),
            )
    return sorted(candidates.values(), key=lambda item: item.path.as_posix().casefold())


def iter_batches(
    candidates: Iterable[ImportCandidate],
    *,
    max_files: int,
    max_bytes: int,
) -> Iterable[list[ImportCandidate]]:
    """Group candidates within the same server-side upload budget."""

    if max_files < 1 or max_bytes < 1:
        raise ValueError("batch limits must be positive")
    current: list[ImportCandidate] = []
    current_bytes = 0
    for candidate in candidates:
        if current and (
            len(current) >= max_files or current_bytes + candidate.size_bytes > max_bytes
        ):
            yield current
            current = []
            current_bytes = 0
        current.append(candidate)
        current_bytes += candidate.size_bytes
    if current:
        yield current


def file_fingerprint(path: Path) -> ImportFingerprint:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    stat = path.stat()
    return ImportFingerprint(
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sha256=digest.hexdigest(),
    )


class ImportState:
    """Append-only state file used to resume a large import safely."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._records: dict[str, dict[str, Any]] = {}
        if path is not None and path.exists():
            with path.open("r", encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, start=1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise ValueError(
                            f"invalid import state at line {line_number}: {error}"
                        ) from error
                    source = record.get("source")
                    if not isinstance(source, str) or not source:
                        raise ValueError(f"import state line {line_number} has no source")
                    self._records[source] = record

    def succeeded(
        self,
        path: Path,
        *,
        project_id: UUID,
        artifact_kind: ArtifactKind,
        fingerprint: ImportFingerprint,
    ) -> bool:
        record = self._records.get(str(path))
        return bool(
            record
            and record.get("status") == "succeeded"
            and record.get("project_id") == str(project_id)
            and record.get("artifact_kind") == artifact_kind.value
            and record.get("size_bytes") == fingerprint.size_bytes
            and record.get("mtime_ns") == fingerprint.mtime_ns
            and record.get("sha256") == fingerprint.sha256
        )

    def terminal(
        self,
        path: Path,
        *,
        project_id: UUID,
        artifact_kind: ArtifactKind,
        fingerprint: ImportFingerprint,
    ) -> bool:
        record = self._records.get(str(path))
        return bool(
            record
            and record.get("status") in {"succeeded", "filtered"}
            and record.get("project_id") == str(project_id)
            and record.get("artifact_kind") == artifact_kind.value
            and record.get("size_bytes") == fingerprint.size_bytes
            and record.get("mtime_ns") == fingerprint.mtime_ns
            and record.get("sha256") == fingerprint.sha256
        )

    def append(self, record: dict[str, Any]) -> None:
        source = record["source"]
        self._records[source] = record
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()


def _media_type(path: Path) -> str:
    return mimetypes.guess_type(path.name, strict=False)[0] or "application/octet-stream"


def _record(
    candidate: ImportCandidate,
    fingerprint: ImportFingerprint,
    *,
    project_id: UUID,
    artifact_kind: ArtifactKind,
    status: str,
    artifact_id: UUID | None = None,
    ingestion_status: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "source": str(candidate.path),
        "filename": candidate.path.name,
        "project_id": str(project_id),
        "artifact_kind": artifact_kind.value,
        "size_bytes": fingerprint.size_bytes,
        "mtime_ns": fingerprint.mtime_ns,
        "sha256": fingerprint.sha256,
        "status": status,
        "artifact_id": str(artifact_id) if artifact_id else None,
        "ingestion_status": ingestion_status,
        "error": error,
    }


async def import_files(
    candidates: list[ImportCandidate],
    *,
    project_id: UUID,
    user_id: UUID,
    artifact_kind: ArtifactKind,
    state: ImportState,
    dry_run: bool,
    fingerprint_workers: int | None = None,
    metrics: ImportMetrics | None = None,
) -> ImportSummary:
    metrics = metrics or ImportMetrics()
    settings = get_settings()
    summary = ImportSummary(scanned=len(candidates))
    workers = fingerprint_workers or min(MAX_FINGERPRINT_WORKERS, max(4, os.cpu_count() or 4))
    if workers < 1:
        raise ValueError("fingerprint_workers must be positive")
    step_started = perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        fingerprints = dict(
            zip(
                candidates,
                executor.map(file_fingerprint, (candidate.path for candidate in candidates)),
                strict=True,
            )
        )
    metrics.add_step_timing("fingerprint", (perf_counter() - step_started) * 1000)
    step_started = perf_counter()
    pending: list[tuple[ImportCandidate, ImportFingerprint]] = []
    for candidate in candidates:
        fingerprint = fingerprints[candidate]
        if state.path is not None and state.terminal(
            candidate.path,
            project_id=project_id,
            artifact_kind=artifact_kind,
            fingerprint=fingerprint,
        ):
            summary = summary.add(ImportSummary(skipped=1))
            continue
        pending.append((candidate, fingerprint))
    metrics.add_step_timing("state_filter", (perf_counter() - step_started) * 1000)

    if dry_run:
        metrics.add_step_timing("dry_run", 0.0)
        return summary.add(
            ImportSummary(
                attempted=len(pending),
                bytes_succeeded=sum(candidate.size_bytes for candidate, _ in pending),
            )
        )

    fingerprints = dict(pending)

    sql_stats = _SQLStats()
    event.listen(engine.sync_engine, "before_cursor_execute", sql_stats.before_cursor_execute)
    event.listen(engine.sync_engine, "after_cursor_execute", sql_stats.after_cursor_execute)

    async def import_batch(batch: list[ImportCandidate]) -> ImportSummary:
        batch_started = perf_counter()
        payloads = [
            ArtifactUploadPayload(
                filename=candidate.path.name,
                media_type=_media_type(candidate.path),
                payload=None,
                spool_path=candidate.path,
            )
            for candidate in batch
        ]
        print(
            f"importing {len(batch)} files ({sum(item.size_bytes for item in batch)} bytes)",
            file=sys.stderr,
        )
        try:
            service_started = perf_counter()
            result = await ArtifactUploadService.upload_batch(
                files=payloads,
                artifact_kind=artifact_kind,
                project_id=project_id,
                user_id=user_id,
            )
            service_elapsed_ms = (perf_counter() - service_started) * 1000
            metrics.add_phase_timing("upload_batch_service_ms", service_elapsed_ms)
            for phase, elapsed_ms in result.timings_ms.items():
                metrics.add_phase_timing(f"upload_batch_{phase}", elapsed_ms)
            metrics.steps.append(
                {
                    "batch_size": len(batch),
                    "source_bytes": sum(item.size_bytes for item in batch),
                    "elapsed_ms": round((perf_counter() - batch_started) * 1000, 3),
                    "service_elapsed_ms": round(service_elapsed_ms, 3),
                    "succeeded": result.succeeded_count,
                    "failed": result.failed_count,
                    "timings_ms": {
                        key: round(value, 3) for key, value in sorted(result.timings_ms.items())
                    },
                }
            )
        except ValueError as error:
            if len(batch) > 1:
                midpoint = len(batch) // 2
                print(
                    f"batch failed ({error}); retrying as {midpoint} and "
                    f"{len(batch) - midpoint} files",
                    file=sys.stderr,
                )
                first = await import_batch(batch[:midpoint])
                second = await import_batch(batch[midpoint:])
                return first.add(second)
            candidate = batch[0]
            state.append(
                _record(
                    candidate,
                    fingerprints[candidate],
                    project_id=project_id,
                    artifact_kind=artifact_kind,
                    status="failed",
                    error=str(error) or type(error).__name__,
                )
            )
            return ImportSummary(attempted=1, failed=1)
        except Exception as error:
            message = str(error) or type(error).__name__
            for candidate in batch:
                state.append(
                    _record(
                        candidate,
                        fingerprints[candidate],
                        project_id=project_id,
                        artifact_kind=artifact_kind,
                        status="failed",
                        error=message,
                    )
                )
            raise

        batch_summary = ImportSummary(attempted=len(batch))
        for candidate, item in zip(batch, result.items, strict=True):
            fingerprint = fingerprints[candidate]
            artifact_id = item.result.artifact_id if item.result is not None else None
            filtered = item.error_code == "no_calculation_frames" or (
                item.result is not None and item.result.source_frame_count == 0
            )
            if filtered:
                batch_summary = batch_summary.add(ImportSummary(filtered=1))
                status = "filtered"
            elif item.succeeded:
                batch_summary = batch_summary.add(
                    ImportSummary(succeeded=1, bytes_succeeded=candidate.size_bytes)
                )
                status = "succeeded"
            else:
                batch_summary = batch_summary.add(ImportSummary(failed=1))
                status = "failed"
            state.append(
                _record(
                    candidate,
                    fingerprint,
                    project_id=project_id,
                    artifact_kind=artifact_kind,
                    status=status,
                    artifact_id=artifact_id,
                    ingestion_status=(
                        item.result.ingestion_status.value
                        if item.result is not None and item.result.ingestion_status is not None
                        else None
                    ),
                    error=item.error_message,
                )
            )
        return batch_summary

    batches_started_at = perf_counter()
    try:
        for batch in iter_batches(
            [candidate for candidate, _ in pending],
            max_files=settings.max_batch_files,
            max_bytes=settings.max_batch_bytes,
        ):
            summary = summary.add(await import_batch(batch))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", sql_stats.before_cursor_execute)
        event.remove(engine.sync_engine, "after_cursor_execute", sql_stats.after_cursor_execute)
    metrics.sql_statement_count = sql_stats.count
    metrics.sql_executemany_count = sql_stats.executemany_count
    metrics.sql_elapsed_ms_by_operation = sql_stats.elapsed_ms_by_operation
    metrics.add_step_timing("upload_batches", (perf_counter() - batches_started_at) * 1000)
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import files directly into the configured PostgreSQL/RustFS services.",
    )
    parser.add_argument(
        "roots",
        nargs="+",
        type=Path,
        help="files or directories to import recursively",
    )
    parser.add_argument("--project-id", required=True, type=UUID)
    parser.add_argument(
        "--user-id",
        type=UUID,
        help="authenticated project user; defaults to development user",
    )
    parser.add_argument(
        "--artifact-kind",
        choices=[kind.value for kind in ArtifactKind],
        default=ArtifactKind.CALCULATION_OUTPUT.value,
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        help="append-only JSONL checkpoint file for resumable imports",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="scan and report without writing PostgreSQL/RustFS",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    user_id = args.user_id or settings.development_user_id
    if settings.environment == "production" and args.user_id is None:
        raise ValueError("--user-id is required when TRICYCLE_ENVIRONMENT=production")
    artifact_kind = ArtifactKind(args.artifact_kind)
    started_at = perf_counter()
    metrics = ImportMetrics()
    discover_started = perf_counter()
    candidates = discover_files(args.roots)
    metrics.add_step_timing("discover", (perf_counter() - discover_started) * 1000)
    state = ImportState(args.state_file)
    summary = await import_files(
        candidates,
        project_id=args.project_id,
        user_id=user_id,
        artifact_kind=artifact_kind,
        state=state,
        dry_run=args.dry_run,
        metrics=metrics,
    )
    payload = asdict(summary)
    payload["timings"] = metrics.as_dict(total_ms=(perf_counter() - started_at) * 1000)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 1 if summary.failed else 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(_run(_parser().parse_args())))
    except (ValueError, OSError) as error:
        print(f"import failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
