"""Run an end-to-end upload_batch benchmark against configured remote services.

The benchmark uses a savepoint-backed outer PostgreSQL transaction and removes
the RustFS objects it created, so a run does not leave test rows or objects in
the remote development environment.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import socket
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from tricycle_reaction_db.application.dtos import ArtifactBatchUploadResult
from tricycle_reaction_db.application.services import artifact_uploads, authorization
from tricycle_reaction_db.application.services.artifact_uploads import (
    ArtifactUploadPayload,
    ArtifactUploadService,
    close_molop_process_pool,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import ArtifactFile
from tricycle_reaction_db.db.session import engine
from tricycle_reaction_db.domain.enums import ArtifactKind
from tricycle_reaction_db.domain.identity import DEVELOPMENT_USER_ID, SYSTEM_PROJECT_ID
from tricycle_reaction_db.storage.rustfs import (
    RustFSObjectStore,
    RustFSSettings,
    time_partitioned_content_addressed_key_for_sha256,
)

REPOSITORY_ROOT = Path(__file__).parents[1]
SCHEMA_VERSION = "remote-upload-benchmark-v1"


@dataclass(frozen=True, slots=True)
class _ProcessRecord:
    parent_pid: int
    cpu_ticks: int
    rss_pages: int


def _process_table() -> dict[int, _ProcessRecord]:
    table: dict[int, _ProcessRecord] = {}
    for stat_path in Path("/proc").glob("[0-9]*/stat"):
        try:
            raw = stat_path.read_text(encoding="utf-8")
            closing_paren = raw.rfind(")")
            fields = raw[closing_paren + 2 :].split()
            pid = int(stat_path.parent.name)
            table[pid] = _ProcessRecord(
                parent_pid=int(fields[1]),
                cpu_ticks=int(fields[11]) + int(fields[12]),
                rss_pages=max(0, int(fields[21])),
            )
        except (FileNotFoundError, IndexError, PermissionError, ProcessLookupError, ValueError):
            continue
    return table


def _process_tree(table: dict[int, _ProcessRecord], root_pid: int) -> set[int]:
    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, record in table.items():
            if record.parent_pid in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    return descendants


class _ProcessTreeMonitor:
    """Sample process-tree CPU to verify that parser pools actually run in parallel."""

    def __init__(self, root_pid: int) -> None:
        self.root_pid = root_pid
        self._clock_ticks = int(os.sysconf("SC_CLK_TCK"))
        self._page_size = int(os.sysconf("SC_PAGE_SIZE"))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._previous_ticks: dict[int, int] = {}
        self._sampled_cpu_seconds = 0.0
        self._peak_cpu_percent = 0.0
        self._peak_process_count = 0
        self._peak_rss_mib = 0.0
        self._samples = 0
        self._started_at: float | None = None

    def _sample(self) -> None:
        now = perf_counter()
        table = _process_table()
        process_ids = _process_tree(table, self.root_pid)
        current_ticks = {
            pid: table[pid].cpu_ticks for pid in process_ids if pid in table
        }
        previous_ticks = self._previous_ticks
        delta_ticks = sum(
            max(0, ticks - previous_ticks[pid])
            for pid, ticks in current_ticks.items()
            if pid in previous_ticks
        )
        previous_ticks.clear()
        previous_ticks.update(current_ticks)
        previous_now = getattr(self, "_previous_sample_at", None)
        self._previous_sample_at = now
        if previous_now is not None and now > previous_now:
            cpu_seconds = delta_ticks / self._clock_ticks
            self._sampled_cpu_seconds += cpu_seconds
            self._peak_cpu_percent = max(
                self._peak_cpu_percent,
                cpu_seconds / (now - previous_now) * 100,
            )
        rss_mib = (
            sum(table[pid].rss_pages for pid in process_ids if pid in table)
            * self._page_size
            / 1024
            / 1024
        )
        self._peak_process_count = max(self._peak_process_count, len(process_ids))
        self._peak_rss_mib = max(self._peak_rss_mib, rss_mib)
        self._samples += 1

    def _run(self) -> None:
        self._sample()
        while not self._stop.wait(0.25):
            self._sample()
        self._sample()

    def start(self) -> None:
        self._started_at = perf_counter()
        self._thread = threading.Thread(target=self._run, name="benchmark-process-monitor")
        self._thread.start()

    def stop(self) -> dict[str, object]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        elapsed = perf_counter() - (self._started_at or perf_counter())
        return {
            "sample_count": self._samples,
            "peak_process_count": self._peak_process_count,
            "peak_child_process_count": max(0, self._peak_process_count - 1),
            "peak_process_tree_cpu_percent": round(self._peak_cpu_percent, 1),
            "average_process_tree_cpu_percent": round(
                self._sampled_cpu_seconds / max(elapsed, 0.001) * 100,
                1,
            ),
            "sampled_process_tree_cpu_seconds": round(self._sampled_cpu_seconds, 3),
            "peak_process_tree_rss_mib": round(self._peak_rss_mib, 1),
        }


class SQLStats:
    def __init__(self) -> None:
        self.count = 0
        self.executemany_count = 0
        self.operations: Counter[str] = Counter()
        self.operation_elapsed_ms: dict[str, float] = {}
        self.statement_stats: dict[str, dict[str, float | int]] = {}
        self.statement_batch_rows: dict[str, int] = {}

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
        operation = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else "EMPTY"
        self.operations[operation] += 1
        context._remote_benchmark_started_at = perf_counter()

    def after_cursor_execute(
        self,
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        context: Any,
        _executemany: bool,
    ) -> None:
        started_at = getattr(context, "_remote_benchmark_started_at", None)
        if started_at is None:
            return
        elapsed_ms = (perf_counter() - float(started_at)) * 1000
        operation = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else "EMPTY"
        self.operation_elapsed_ms[operation] = (
            self.operation_elapsed_ms.get(operation, 0.0) + elapsed_ms
        )
        normalized = re.sub(r"\s+", " ", statement.strip())[:500]
        if _executemany and isinstance(_parameters, (list, tuple)):
            self.statement_batch_rows[normalized] = len(_parameters)
        stats = self.statement_stats.setdefault(
            normalized,
            {"count": 0, "elapsed_ms": 0.0, "max_elapsed_ms": 0.0},
        )
        stats["count"] = int(stats["count"]) + 1
        stats["elapsed_ms"] = float(stats["elapsed_ms"]) + elapsed_ms
        stats["max_elapsed_ms"] = max(float(stats["max_elapsed_ms"]), elapsed_ms)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-sizes", type=int, nargs="+", default=[1, 8, 32])
    parser.add_argument(
        "--fixture",
        type=Path,
        required=True,
        help="real Gaussian/ORCA file or directory; synthetic repository fixtures are not allowed",
    )
    parser.add_argument("--selection-offset", type=int, default=0)
    return parser.parse_args()


def _reject_synthetic_fixture(fixture: Path) -> Path:
    resolved = fixture.resolve()
    synthetic_root = (REPOSITORY_ROOT / "tests/fixtures").resolve()
    try:
        resolved.relative_to(synthetic_root)
    except ValueError:
        return resolved
    raise ValueError(
        "synthetic repository fixtures are not allowed; provide a real Gaussian/ORCA file "
        "or directory with --fixture"
    )


async def _delete_objects(keys: list[str]) -> int:
    if not keys:
        return 0

    def delete() -> int:
        with RustFSObjectStore(RustFSSettings()) as store:
            for key in keys:
                store.delete(key)
        return len(keys)

    return await asyncio.to_thread(delete)


def _candidate_object_keys(content_hashes: list[str]) -> set[str]:
    now = datetime.now(UTC)
    return {
        time_partitioned_content_addressed_key_for_sha256(
            content_hash,
            uploaded_at=now - timedelta(hours=hours),
            prefix="uploads",
        )
        for content_hash in content_hashes
        for hours in range(0, 6)
    }


async def _delete_candidate_objects(content_hashes: list[str]) -> int:
    keys = _candidate_object_keys(content_hashes)
    if not keys:
        return 0

    def delete_candidates() -> int:
        deleted = 0
        with RustFSObjectStore(RustFSSettings()) as store:
            for key in keys:
                if store.exists(key):
                    store.delete(key)
                    deleted += 1
        return deleted

    return await asyncio.to_thread(delete_candidates)


async def _assert_cold_payloads(
    connection: Any,
    content_hashes: list[str],
) -> dict[str, int]:
    """Reject a run if any generated content already exists remotely."""

    if len(set(content_hashes)) != len(content_hashes):
        raise RuntimeError("cold-data preflight found duplicate content hashes in the batch")
    existing_rows = await connection.execute(
        select(ArtifactFile.content_sha256).where(
            col(ArtifactFile.content_sha256).in_(content_hashes)
        )
    )
    existing_database_hashes = set(existing_rows.scalars().all())

    settings = RustFSSettings()
    now = datetime.now(UTC)
    object_keys_by_hash = {
        content_hash: {
            time_partitioned_content_addressed_key_for_sha256(
                content_hash,
                uploaded_at=stamp,
                prefix="uploads",
            )
            for stamp in (now, now - timedelta(hours=1))
        }
        for content_hash in content_hashes
    }
    candidate_object_keys = sorted({key for keys in object_keys_by_hash.values() for key in keys})

    def existing_object_keys() -> set[str]:
        with RustFSObjectStore(settings) as store:
            worker_count = min(8, len(candidate_object_keys))
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                return {
                    key
                    for key, exists in zip(
                        candidate_object_keys,
                        executor.map(store.exists, candidate_object_keys),
                        strict=True,
                    )
                    if exists
                }

    listed_object_keys = await asyncio.to_thread(existing_object_keys)
    existing_storage_keys = {
        key for keys in object_keys_by_hash.values() for key in keys if key in listed_object_keys
    }
    if existing_database_hashes or existing_storage_keys:
        raise RuntimeError(
            "cold-data preflight found previously uploaded content: "
            f"database={len(existing_database_hashes)} "
            f"rustfs={len(existing_storage_keys)}"
        )
    return {
        "content_hash_count": len(content_hashes),
        "content_hash_unique_count": len(set(content_hashes)),
        "existing_database_hash_count": 0,
        "existing_rustfs_object_count": 0,
    }


def _source_candidates(fixture: Path) -> list[Path]:
    if fixture.is_file():
        return [fixture]
    if not fixture.is_dir():
        raise ValueError(f"fixture does not exist: {fixture}")
    candidates = sorted(
        path
        for path in fixture.rglob("*")
        if path.is_file() and path.suffix.lower() in {".log", ".out", ".orcaout"}
    )
    if not candidates:
        raise ValueError(f"fixture directory has no supported calculation files: {fixture}")
    return candidates


def _selected_sources(
    candidates: list[Path],
    batch_size: int,
    *,
    selection_offset: int,
) -> list[Path]:
    if len(candidates) == 1:
        return [candidates[0]] * batch_size
    if len(candidates) < batch_size:
        raise ValueError(
            f"fixture directory has {len(candidates)} supported files, "
            f"but batch size {batch_size} was requested"
        )
    # Spread a batch across the complete snapshot instead of taking one hot
    # directory/reaction prefix. Each selected path is distinct in directory mode.
    return [
        candidates[((index * len(candidates)) // batch_size + selection_offset) % len(candidates)]
        for index in range(batch_size)
    ]


def _load_batch_sources(
    candidates: list[Path],
    *,
    batch_size: int,
    nonce: str,
    selection_offset: int,
) -> tuple[list[ArtifactUploadPayload], list[Path]]:
    selected = _selected_sources(
        candidates,
        batch_size,
        selection_offset=selection_offset,
    )
    files: list[ArtifactUploadPayload] = []
    for index, source_path in enumerate(selected):
        payload = source_path.read_bytes()
        if len(candidates) == 1 and batch_size > 1:
            # Preserve the historical single-fixture microbenchmark behavior.
            payload += f"\n! codex remote upload benchmark {nonce} {index}\n".encode()
        filename = f"codex-remote-{nonce}-{index:04d}-{source_path.name}"
        files.append(ArtifactUploadPayload(filename, "text/plain", payload))
    return files, selected


async def _filter_cold_sources(
    candidates: list[Path],
    *,
    required_count: int,
    selection_offset: int,
) -> tuple[list[Path], int]:
    """Choose distinct real files whose content hashes are absent from PostgreSQL."""

    async with engine.connect() as connection:
        rows = await connection.execute(select(ArtifactFile.content_sha256))
        existing_hashes = set(rows.scalars().all())
    if len(candidates) == 1:
        return candidates, len(existing_hashes)

    ordered = [
        candidates[
            ((index * len(candidates)) // required_count + selection_offset) % len(candidates)
        ]
        for index in range(required_count * 4)
    ]
    ordered.extend(candidates)
    eligible: list[tuple[Path, str]] = []
    selected_hashes: set[str] = set()
    for path in ordered:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in existing_hashes or digest in selected_hashes:
            continue
        eligible.append((path, digest))
        selected_hashes.add(digest)
        if len(eligible) >= required_count * 4:
            break
    existing_storage_hashes = await asyncio.to_thread(
        _existing_storage_hashes,
        [digest for _, digest in eligible],
    )
    selected = [path for path, digest in eligible if digest not in existing_storage_hashes][
        :required_count
    ]
    if len(selected) >= required_count:
        return selected, len(existing_hashes)
    raise ValueError(
        f"could only select {len(selected)} cold unique files; {required_count} required"
    )


def _existing_storage_hashes(content_hashes: list[str]) -> set[str]:
    settings = RustFSSettings()
    now = datetime.now(UTC)
    with RustFSObjectStore(settings) as store, ThreadPoolExecutor(max_workers=8) as executor:

        def exists(content_hash: str) -> tuple[str, bool]:
            keys = (
                time_partitioned_content_addressed_key_for_sha256(
                    content_hash,
                    uploaded_at=now - timedelta(hours=hours),
                    prefix="uploads",
                )
                for hours in range(0, 6)
            )
            return content_hash, any(store.exists(key) for key in keys)

        return {
            content_hash for content_hash, found in executor.map(exists, content_hashes) if found
        }


async def _run_batch(
    candidates: list[Path],
    *,
    fixture_name: str,
    batch_size: int,
    n_jobs: int,
    selection_offset: int,
) -> dict[str, object]:
    nonce = uuid4().hex
    files, selected_sources = _load_batch_sources(
        candidates,
        batch_size=batch_size,
        nonce=nonce,
        selection_offset=selection_offset,
    )
    content_hashes = [hashlib.sha256(file.payload or b"").hexdigest() for file in files]
    connection = await engine.connect()
    transaction = await connection.begin()
    if os.getenv("TRICYCLE_BENCHMARK_SYNCHRONOUS_COMMIT_OFF") == "1":
        await connection.execute(text("SET LOCAL synchronous_commit = off"))
    isolated_factory = async_sessionmaker(
        bind=connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    previous_upload_factory = artifact_uploads.session_factory  # type: ignore[attr-defined]
    previous_authorization_factory = authorization.session_factory  # type: ignore[attr-defined]
    artifact_uploads.session_factory = isolated_factory  # type: ignore[attr-defined]
    authorization.session_factory = isolated_factory  # type: ignore[attr-defined]
    sql_stats = SQLStats()
    result: ArtifactBatchUploadResult | None = None
    object_keys: list[str] = []
    deleted_objects = 0
    cold_preflight: dict[str, int] = {}
    cold_preflight_elapsed_ms = 0.0
    service_elapsed_seconds = 0.0
    cleanup_elapsed_ms = 0.0
    listener_attached = False
    cold_preflight_succeeded = False
    try:
        cold_started = perf_counter()
        cold_preflight = await _assert_cold_payloads(connection, content_hashes)
        cold_preflight_succeeded = True
        cold_preflight_elapsed_ms = (perf_counter() - cold_started) * 1000
        event.listen(engine.sync_engine, "before_cursor_execute", sql_stats.before_cursor_execute)
        event.listen(engine.sync_engine, "after_cursor_execute", sql_stats.after_cursor_execute)
        listener_attached = True
        service_started = perf_counter()
        result = await ArtifactUploadService.upload_batch(
            files=files,
            artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
            project_id=SYSTEM_PROJECT_ID,
            user_id=DEVELOPMENT_USER_ID,
        )
        service_elapsed_seconds = perf_counter() - service_started
        cleanup_started = perf_counter()
        artifact_ids = [item.result.artifact_id for item in result.items if item.result is not None]
        if artifact_ids:
            rows = await connection.execute(
                select(ArtifactFile.object_key).where(col(ArtifactFile.id).in_(artifact_ids))
            )
            object_keys = [str(key) for key in rows.scalars().all()]
        deleted_objects = await _delete_objects(object_keys)
        cleanup_elapsed_ms = (perf_counter() - cleanup_started) * 1000
    finally:
        if listener_attached:
            event.remove(
                engine.sync_engine,
                "before_cursor_execute",
                sql_stats.before_cursor_execute,
            )
            event.remove(
                engine.sync_engine,
                "after_cursor_execute",
                sql_stats.after_cursor_execute,
            )
        artifact_uploads.session_factory = previous_upload_factory  # type: ignore[attr-defined]
        authorization.session_factory = previous_authorization_factory  # type: ignore[attr-defined]
        await transaction.rollback()
        await connection.close()
        if cold_preflight_succeeded:
            deleted_objects += await _delete_candidate_objects(content_hashes)

    if result is None:
        raise RuntimeError("upload_batch did not return a result")
    inference_status_counts = Counter()
    inference_error_counts = Counter()
    inference_error_messages: dict[str, str] = {}
    upload_error_counts = Counter()
    upload_error_messages: dict[str, str] = {}
    for item in result.items:
        if not item.succeeded and item.error_code:
            upload_error_counts[item.error_code] += 1
            if item.error_code not in upload_error_messages and item.error_message:
                upload_error_messages[item.error_code] = item.error_message
        if item.result is None:
            continue
        for inference in item.result.inferences:
            inference_status_counts[str(inference.status.value)] += 1
            if inference.error_code:
                inference_error_counts[inference.error_code] += 1
                if inference.error_code not in inference_error_messages and inference.error_message:
                    inference_error_messages[inference.error_code] = inference.error_message
    return {
        "batch_size": batch_size,
        "fixture": fixture_name,
        "source_mode": "directory" if len(candidates) > 1 else "single_fixture",
        "selection_offset": selection_offset,
        "source_file_count": len(selected_sources),
        "source_bytes_total": sum(len(file.payload or b"") for file in files),
        "source_names_sha256": hashlib.sha256(
            "\n".join(str(path) for path in selected_sources).encode()
        ).hexdigest(),
        "failed_sources": [
            {
                "source": str(selected_sources[index]),
                "error_code": item.error_code,
                "error_message": item.error_message,
            }
            for index, item in enumerate(result.items)
            if not item.succeeded and index < len(selected_sources)
        ],
        "n_jobs": n_jobs,
        "elapsed_seconds": round(service_elapsed_seconds, 3),
        "throughput_mb_per_second": round(
            sum(len(file.payload or b"") for file in files)
            / max(service_elapsed_seconds, 0.001)
            / 1_000_000,
            3,
        ),
        "throughput_mib_per_second": round(
            sum(len(file.payload or b"") for file in files)
            / max(service_elapsed_seconds, 0.001)
            / 1024
            / 1024,
            3,
        ),
        "measurement_scope": "upload_batch_only",
        "stage_parallelism": {
            "configured_workers": n_jobs,
            "molop_file_parse_wall_overlap_factor": round(
                result.timings_ms.get("molop_file_parse_sum_ms", 0.0)
                / max(result.timings_ms.get("molop_file_parse_ms", 1.0), 1.0),
                2,
            ),
            "molgr_frame_reconstruction_wall_overlap_factor": round(
                result.timings_ms.get("molgr_frame_reconstruction_sum_ms", 0.0)
                / max(
                    result.timings_ms.get("molgr_frame_reconstruction_ms", 1.0),
                    1.0,
                ),
                2,
            ),
        },
        "succeeded_count": result.succeeded_count,
        "failed_count": result.failed_count,
        "source_frame_count": result.source_frame_count,
        "transition_state_frame_count": result.transition_state_frame_count,
        "inferred_reaction_count": result.inferred_reaction_count,
        "inference_status_counts": dict(sorted(inference_status_counts.items())),
        "inference_error_counts": dict(sorted(inference_error_counts.items())),
        "inference_error_messages": inference_error_messages,
        "upload_error_counts": dict(sorted(upload_error_counts.items())),
        "upload_error_messages": upload_error_messages,
        "phase_timings_ms": {key: round(value, 3) for key, value in result.timings_ms.items()},
        "sql_statement_count": sql_stats.count,
        "sql_executemany_count": sql_stats.executemany_count,
        "sql_by_operation": dict(sorted(sql_stats.operations.items())),
        "sql_elapsed_ms_by_operation": {
            operation: round(elapsed, 3)
            for operation, elapsed in sorted(sql_stats.operation_elapsed_ms.items())
        },
        "top_sql_by_elapsed_ms": [
            {
                "statement": statement,
                "count": int(stats["count"]),
                "elapsed_ms": round(float(stats["elapsed_ms"]), 3),
                "max_elapsed_ms": round(float(stats["max_elapsed_ms"]), 3),
            }
            for statement, stats in sorted(
                sql_stats.statement_stats.items(),
                key=lambda item: float(item[1]["elapsed_ms"]),
                reverse=True,
            )[:10]
        ],
        "top_sql_batch_rows": [
            {"statement": statement, "rows": rows}
            for statement, rows in sorted(
                sql_stats.statement_batch_rows.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:10]
        ],
        "rustfs_objects_created": len(object_keys),
        "rustfs_objects_deleted": deleted_objects,
        "database_rolled_back": True,
        "cold_data_preflight": cold_preflight,
        "cold_preflight_elapsed_ms_excluded": round(cold_preflight_elapsed_ms, 3),
        "cleanup_elapsed_ms_excluded": round(cleanup_elapsed_ms, 3),
    }


async def _run(arguments: argparse.Namespace) -> dict[str, object]:
    fixture = _reject_synthetic_fixture(arguments.fixture)
    discovered_candidates = _source_candidates(fixture)
    if any(size <= 0 for size in arguments.batch_sizes):
        raise ValueError("--batch-sizes values must be positive")

    settings = get_settings()
    candidates = discovered_candidates
    existing_database_hash_count = 0
    if fixture.is_dir():
        candidates, existing_database_hash_count = await _filter_cold_sources(
            discovered_candidates,
            required_count=max(arguments.batch_sizes),
            selection_offset=arguments.selection_offset,
        )
    results = [
        await _run_batch(
            candidates,
            fixture_name=str(fixture),
            batch_size=batch_size,
            n_jobs=settings.molop_batch_n_jobs,
            selection_offset=arguments.selection_offset,
        )
        for batch_size in arguments.batch_sizes
    ]
    await close_molop_process_pool()
    return {
        "schema_version": SCHEMA_VERSION,
        "node": socket.gethostname(),
        "database_endpoint": settings.database_url.rsplit("@", 1)[-1],
        "rustfs_endpoint": settings.rustfs_endpoint_url,
        "fixture": str(fixture),
        "source_mode": "directory" if fixture.is_dir() else "single_fixture",
        "source_file_count_available": len(candidates),
        "source_file_count_discovered": len(discovered_candidates),
        "existing_database_hash_count_at_selection": existing_database_hash_count,
        "fixture_sha256": (
            hashlib.sha256(fixture.read_bytes()).hexdigest() if fixture.is_file() else None
        ),
        "n_jobs": settings.molop_batch_n_jobs,
        "batch_sizes": arguments.batch_sizes,
        "selection_offset": arguments.selection_offset,
        "succeeded": all(item["failed_count"] == 0 for item in results),
        "results": results,
    }


def main() -> None:
    monitor = None if os.getenv("TRICYCLE_BENCHMARK_DISABLE_PROCESS_MONITOR") == "1" else _ProcessTreeMonitor(os.getpid())
    if monitor is not None:
        monitor.start()
    payload: dict[str, object] | None = None
    try:
        payload = asyncio.run(_run(_arguments()))
    except Exception as error:
        print(f"remote benchmark failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    finally:
        process_metrics = monitor.stop() if monitor is not None else {}
    if payload is not None:
        payload["process_metrics"] = process_metrics
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
