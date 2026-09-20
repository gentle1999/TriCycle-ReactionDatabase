"""Benchmark the shared MolOP parse/frame pipeline on real calculation files.

The report measures source preparation, MolOP parsing, and frame materialization.
It intentionally excludes RustFS staging and database persistence; use the remote
upload benchmark for those external-service costs.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import socket
import tempfile
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import TypedDict

from tricycle_reaction_db.application.services import artifact_uploads
from tricycle_reaction_db.application.services.artifact_upload_types import (
    _FailedInference,
    _ParsedArtifact,
    _SuccessfulInference,
)
from tricycle_reaction_db.core.config import get_settings

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REAL_WORLD_FIXTURE_ROOT = (REPOSITORY_ROOT / "tests/fixtures/real_world_extremes").resolve()
REAL_WORLD_BATCH_FIXTURE_ROOT = (REPOSITORY_ROOT / "tests/fixtures/real_world_batch_256").resolve()
REAL_WORLD_FIXTURE_ROOTS = (REAL_WORLD_FIXTURE_ROOT, REAL_WORLD_BATCH_FIXTURE_ROOT)
REPORT_SCHEMA_VERSION = "real-world-ingestion-performance-v1"
SUPPORTED_SUFFIXES = (".log", ".out", ".orcaout", ".log.gz", ".out.gz", ".orcaout.gz")


class SampleReport(TypedDict, total=False):
    fixture: str
    status: str
    stored_size_bytes: int
    source_size_bytes: int
    stored_sha256: str
    source_sha256: str
    queue_wait_seconds: float
    elapsed_seconds: float
    stored_mib_per_second: float
    source_mib_per_second: float
    source_format: str | None
    source_frame_count: int
    materialized_frame_count: int
    segment_frame_counts: list[int]
    inference_count: int
    successful_inference_count: int
    failed_inference_count: int
    parse_diagnostic_count: int
    error: str


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        type=Path,
        action="append",
        default=[],
        help="real Gaussian/ORCA file; repeat to benchmark a representative corpus",
    )
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        action="append",
        default=[],
        help="directory of real calculation files; repeatable",
    )
    parser.add_argument("--n-jobs", type=int, default=None)
    parser.add_argument("--parallel-files", type=int, default=None)
    parser.add_argument("--output", type=Path, required=True, help="JSON report destination")
    parser.add_argument("--markdown-output", type=Path, default=None)
    return parser.parse_args()


def _is_supported_source(path: Path) -> bool:
    lowered_name = path.name.lower()
    return path.is_file() and any(lowered_name.endswith(suffix) for suffix in SUPPORTED_SUFFIXES)


def _resolve_fixtures(files: Sequence[Path], directories: Sequence[Path]) -> tuple[Path, ...]:
    resolved: set[Path] = set()
    for candidate in files:
        path = candidate.expanduser().resolve()
        if not _is_supported_source(path):
            raise ValueError(f"unsupported or missing calculation file: {candidate}")
        resolved.add(path)
    for candidate in directories:
        directory = candidate.expanduser().resolve()
        if not directory.is_dir():
            raise ValueError(f"fixture directory does not exist: {candidate}")
        paths = (path for path in directory.rglob("*") if _is_supported_source(path))
        resolved.update(path.resolve() for path in paths)
    if not resolved:
        raise ValueError("provide at least one supported file or fixture directory")
    for path in resolved:
        try:
            path.relative_to(REPOSITORY_ROOT / "tests/fixtures")
        except ValueError:
            continue
        if not any(path.is_relative_to(root) for root in REAL_WORLD_FIXTURE_ROOTS):
            raise ValueError(
                "repository fixtures are only accepted from the pinned real-world test corpora"
            )
    for candidate in directories:
        directory = candidate.expanduser().resolve()
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_count = manifest.get("expected_file_count") if isinstance(manifest, dict) else None
        if expected_count is None:
            continue
        entries = manifest.get("samples")
        if not isinstance(expected_count, int) or not isinstance(entries, list):
            raise ValueError(f"invalid corpus size contract in {manifest_path}")
        manifest_files = {
            entry["file"]
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("file"), str)
        }
        resolved_files = {
            path.relative_to(directory).as_posix()
            for path in resolved
            if path.is_relative_to(directory)
        }
        if (
            len(entries) != expected_count
            or len(manifest_files) != expected_count
            or resolved_files != manifest_files
        ):
            raise ValueError(
                f"corpus must contain exactly {expected_count} manifest files; "
                f"found {len(resolved_files)} files and {len(manifest_files)} manifest entries"
            )
    return tuple(sorted(resolved, key=lambda path: str(path)))


def _fixture_label(path: Path) -> str:
    try:
        return path.relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return path.name


def _hash_stream(stream: Iterable[bytes]) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in stream:
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _file_fingerprints(path: Path) -> tuple[int, str, int, str]:
    def raw_chunks() -> Iterable[bytes]:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                yield chunk

    stored_hash, stored_size = _hash_stream(raw_chunks())
    if path.name.lower().endswith(".gz"):

        def expanded_chunks() -> Iterable[bytes]:
            with gzip.open(path, "rb") as source:
                while chunk := source.read(1024 * 1024):
                    yield chunk

        source_hash, source_size = _hash_stream(expanded_chunks())
    else:
        source_hash, source_size = stored_hash, stored_size
    return stored_size, stored_hash, source_size, source_hash


def _manifest_contract(path: Path) -> dict[str, object] | None:
    for fixture_root in path.parents:
        manifest_path = fixture_root / "manifest.json"
        if manifest_path.is_file():
            try:
                relative = path.relative_to(fixture_root)
            except ValueError:
                continue
            if fixture_root == REPOSITORY_ROOT.parent:
                break
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            entries = manifest.get("samples") if isinstance(manifest, dict) else None
            if not isinstance(entries, list):
                raise ValueError(f"invalid real-world fixture manifest: {manifest_path}")
            for entry in entries:
                if isinstance(entry, dict) and entry.get("file") == relative.as_posix():
                    return entry
            raise ValueError(f"fixture is not listed in {manifest_path}: {relative.as_posix()}")
        if fixture_root == REPOSITORY_ROOT:
            break
    return None


def _expand_compressed_fixtures(
    paths: tuple[Path, ...],
    destination: Path,
) -> dict[Path, Path]:
    """Prepare uncompressed parser inputs before the timed benchmark window."""
    parser_paths: dict[Path, Path] = {}
    for index, path in enumerate(paths):
        if not path.name.lower().endswith(".gz"):
            parser_paths[path] = path
            continue
        output_name = f"{index:03d}__{path.name.removesuffix('.gz')}"
        expanded_path = destination / output_name
        with gzip.open(path, "rb") as source, expanded_path.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        parser_paths[path] = expanded_path
    return parser_paths


def _validate_contract(
    contract: dict[str, object] | None,
    *,
    stored_hash: str,
    stored_size: int,
    source_hash: str,
    source_size: int,
    parsed: _ParsedArtifact,
    materialized_count: int,
) -> str | None:
    if contract is None:
        return None
    expected_hash = contract.get("source_sha256")
    expected_size = contract.get("source_size_bytes")
    expected_stored_hash = contract.get("stored_sha256")
    expected_stored_size = contract.get("stored_size_bytes")
    expected_frames = contract.get("source_frame_count")
    expected_segments = contract.get("segment_frame_counts")
    expected_inferences = contract.get("inference_count")
    actual_segments = [segment.frame_count for segment in parsed.chem_file.source_segments]
    mismatches: list[str] = []
    if expected_hash != source_hash:
        mismatches.append("source SHA-256 differs from manifest")
    if expected_size != source_size:
        mismatches.append("source size differs from manifest")
    if expected_stored_hash is not None and expected_stored_hash != stored_hash:
        mismatches.append("stored SHA-256 differs from manifest")
    if expected_stored_size is not None and expected_stored_size != stored_size:
        mismatches.append("stored size differs from manifest")
    if expected_frames is not None and expected_frames != parsed.source_frame_count:
        mismatches.append(
            f"source frame count expected {expected_frames}, got {parsed.source_frame_count}"
        )
    if expected_frames is not None and expected_frames != materialized_count:
        mismatches.append(
            f"materialized frame count expected {expected_frames}, got {materialized_count}"
        )
    if expected_segments is not None and expected_segments != actual_segments:
        mismatches.append(
            f"segment frame counts expected {expected_segments}, got {actual_segments}"
        )
    if expected_inferences is not None and expected_inferences != len(parsed.inferences):
        mismatches.append(
            f"inference count expected {expected_inferences}, got {len(parsed.inferences)}"
        )
    expected_failed_inferences = contract.get("failed_inference_count")
    actual_failed_inferences = sum(
        isinstance(inference, _FailedInference) for inference in parsed.inferences
    )
    if (
        expected_failed_inferences is not None
        and expected_failed_inferences != actual_failed_inferences
    ):
        mismatches.append(
            "failed inference count expected "
            f"{expected_failed_inferences}, got {actual_failed_inferences}"
        )
    return "; ".join(mismatches) or None


def _sample_record(
    path: Path,
    *,
    parsed: _ParsedArtifact,
    queue_wait: float,
    elapsed: float,
    fingerprints: tuple[int, str, int, str],
    contract: dict[str, object] | None,
) -> SampleReport:
    stored_size, stored_hash, source_size, source_hash = fingerprints
    materialized_count = len(parsed.frame_records)
    successful_inference_count = sum(
        isinstance(inference, _SuccessfulInference) for inference in parsed.inferences
    )
    failed_inference_count = sum(
        isinstance(inference, _FailedInference) for inference in parsed.inferences
    )
    contract_error = _validate_contract(
        contract,
        stored_hash=stored_hash,
        stored_size=stored_size,
        source_hash=source_hash,
        source_size=source_size,
        parsed=parsed,
        materialized_count=materialized_count,
    )
    is_partial = (
        materialized_count != parsed.source_frame_count
        or failed_inference_count > 0
        or bool(parsed.parse_diagnostics)
    )
    record: SampleReport = {
        "fixture": _fixture_label(path),
        "status": "failed" if contract_error else "partial" if is_partial else "succeeded",
        "stored_size_bytes": stored_size,
        "source_size_bytes": source_size,
        "stored_sha256": stored_hash,
        "source_sha256": source_hash,
        "queue_wait_seconds": round(queue_wait, 3),
        "elapsed_seconds": round(elapsed, 3),
        "stored_mib_per_second": round(stored_size / max(elapsed, 0.001) / 1024**2, 3),
        "source_mib_per_second": round(source_size / max(elapsed, 0.001) / 1024**2, 3),
        "source_format": parsed.source_format,
        "source_frame_count": parsed.source_frame_count,
        "materialized_frame_count": materialized_count,
        "segment_frame_counts": [
            segment.frame_count for segment in parsed.chem_file.source_segments
        ],
        "inference_count": len(parsed.inferences),
        "successful_inference_count": successful_inference_count,
        "failed_inference_count": failed_inference_count,
        "parse_diagnostic_count": len(parsed.parse_diagnostics),
    }
    if contract_error:
        record["error"] = contract_error
    return record


async def _benchmark(
    paths: tuple[Path, ...],
    parser_paths: dict[Path, Path],
    *,
    workers: int,
    parallel_files: int,
) -> tuple[list[SampleReport], float, float]:
    file_slots = asyncio.Semaphore(parallel_files)
    frame_slots = asyncio.Semaphore(workers * 2)

    async def parse_one(
        path: Path,
    ) -> tuple[Path, _ParsedArtifact | Exception, float, float]:
        queued_at = perf_counter()
        await file_slots.acquire()
        queue_wait = perf_counter() - queued_at
        started = perf_counter()
        try:
            parsed = await artifact_uploads._run_molop_file_pipeline(
                parser_paths[path],
                parser_paths[path].name,
                submission_slots=frame_slots,
                # The benchmark's outer semaphore already times and bounds the
                # file queue. This local permit preserves the production helper
                # path without counting its second acquire as parse latency.
                file_slots=asyncio.Semaphore(1),
            )
            return path, parsed, perf_counter() - started, queue_wait
        except Exception as error:
            return path, error, perf_counter() - started, queue_wait
        finally:
            file_slots.release()

    started = perf_counter()
    parsed_results = await asyncio.gather(*(parse_one(path) for path in paths))
    wall_seconds = perf_counter() - started

    # Hash and manifest validation deliberately happen after the timed parse
    # window, so report bookkeeping does not contend with the shared MolOP pool.
    verification_started = perf_counter()
    records: list[SampleReport] = []
    for path, outcome, elapsed, queue_wait in parsed_results:
        fingerprints = _file_fingerprints(path)
        if isinstance(outcome, Exception):
            stored_size, stored_hash, source_size, source_hash = fingerprints
            records.append(
                {
                    "fixture": _fixture_label(path),
                    "status": "failed",
                    "stored_size_bytes": stored_size,
                    "source_size_bytes": source_size,
                    "stored_sha256": stored_hash,
                    "source_sha256": source_hash,
                    "queue_wait_seconds": round(queue_wait, 3),
                    "elapsed_seconds": round(elapsed, 3),
                    "stored_mib_per_second": 0.0,
                    "source_mib_per_second": 0.0,
                    "error": f"{type(outcome).__name__}: {outcome}",
                }
            )
            continue
        records.append(
            _sample_record(
                path,
                parsed=outcome,
                queue_wait=queue_wait,
                elapsed=elapsed,
                fingerprints=fingerprints,
                contract=_manifest_contract(path),
            )
        )
    verification_seconds = perf_counter() - verification_started
    return records, wall_seconds, verification_seconds


def _markdown_report(report: dict[str, object]) -> str:
    results = report["results"]
    if not isinstance(results, list):
        raise TypeError("benchmark results must be an array")
    summary = report["summary"]
    if not isinstance(summary, dict):
        raise TypeError("benchmark summary must be an object")
    rows = [
        "# Real-world ingestion performance",
        "",
        f"Generated: `{report['generated_at']}`  ",
        f"Host: `{report['host']}` · Python `{report['python_version']}` · "
        f"MolOP `{report['molop_version']}`  ",
        f"Scope: `{report['measurement_scope']}` · workers `{report['workers']}` · "
        f"concurrent files `{report['parallel_files']}`",
        "",
        "## Aggregate",
        "",
        f"- Result: **{report['status']}** "
        f"({summary['succeeded_files']}/{summary['file_count']} files)",
        f"- Wall time: {summary['wall_seconds']} s; "
        f"summed parse time: {summary['summed_parse_seconds']} s; "
        f"summed file-queue wait: {summary['summed_queue_wait_seconds']} s; "
        f"post-run hash/manifest verification: {summary['verification_seconds']} s",
        f"- Input: {summary['source_mib']} MiB uncompressed "
        f"({summary['stored_mib']} MiB original fixture files)",
        f"- Throughput: {summary['frames_per_second']} frames/s; "
        f"{summary['source_mib_per_second']} uncompressed MiB/s",
        f"- Frames: {summary['materialized_frames']}/{summary['source_frames']}; "
        f"segments: {summary['segments']}",
        "",
        "## Per-file results",
        "",
        "| File | Input MiB | Frames | Segments | Queue (s) | Parse (s) | Frames/s | "
        "MiB/s | Inferences | Status |",
        "| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in results:
        assert isinstance(item, dict)
        elapsed = float(item.get("elapsed_seconds", 0.0))
        frames = int(item.get("materialized_frame_count", 0))
        size_mib = int(item.get("source_size_bytes", 0)) / 1024**2
        segments = ", ".join(str(count) for count in item.get("segment_frame_counts", []))
        rows.append(
            f"| `{item['fixture']}` | {size_mib:.2f} | "
            f"{item.get('materialized_frame_count', 0)}/{item.get('source_frame_count', 0)} | "
            f"{segments or '—'} | {item.get('queue_wait_seconds', 0.0):.3f} | {elapsed:.3f} | "
            f"{frames / max(elapsed, 0.001):.2f} | "
            f"{size_mib / max(elapsed, 0.001):.2f} | "
            f"{item.get('successful_inference_count', '—')}/"
            f"{item.get('inference_count', '—')} | {item['status']} |"
        )
        if item.get("error"):
            rows.extend(("", f"Error for `{item['fixture']}`: {item['error']}", ""))
    rows.extend(
        (
            "",
            "This report measures local source preparation, MolOP parsing, and frame "
            "materialization through the shared parser pool. All parser inputs are "
            "uncompressed text; archive extraction and gzip-fixture expansion happen "
            "before the timed window. Queue wait is reported separately from parse time. "
            "It excludes RustFS upload, "
            "PostgreSQL persistence, and profile refresh. Hashing and manifest validation "
            "run after the timed parse window; timings are hardware-dependent and may "
            "benefit from the host filesystem cache.",
            "",
        )
    )
    return "\n".join(rows)


def _write_report(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> None:
    arguments = _arguments()
    paths = _resolve_fixtures(arguments.fixture, arguments.fixture_dir)
    if arguments.n_jobs is not None:
        if arguments.n_jobs == 0 or arguments.n_jobs < -1:
            raise SystemExit("--n-jobs must be -1 (all visible CPUs) or positive")
        os.environ["TRICYCLE_MOLOP_BATCH_N_JOBS"] = str(arguments.n_jobs)
        get_settings.cache_clear()
    workers = artifact_uploads.molop_process_worker_count()
    parallel_files = (
        arguments.parallel_files if arguments.parallel_files is not None else workers * 4
    )
    if parallel_files < 1:
        raise SystemExit("--parallel-files must be positive")

    try:
        with tempfile.TemporaryDirectory(prefix="tricycle-real-world-inputs-") as temp_dir:
            parser_paths = _expand_compressed_fixtures(paths, Path(temp_dir))
            results, wall_seconds, verification_seconds = asyncio.run(
                _benchmark(
                    paths,
                    parser_paths,
                    workers=workers,
                    parallel_files=parallel_files,
                )
            )
    finally:
        asyncio.run(artifact_uploads.close_molop_process_pool())

    materialized_frames = sum(int(item.get("materialized_frame_count", 0)) for item in results)
    source_frames = sum(int(item.get("source_frame_count", 0)) for item in results)
    stored_bytes = sum(int(item.get("stored_size_bytes", 0)) for item in results)
    source_bytes = sum(int(item.get("source_size_bytes", 0)) for item in results)
    succeeded_files = sum(item.get("status") == "succeeded" for item in results)
    passed = (
        succeeded_files == len(paths)
        and materialized_frames == source_frames
        and all(item.get("failed_inference_count", 0) == 0 for item in results)
        and all(item.get("parse_diagnostic_count", 0) == 0 for item in results)
    )
    try:
        molop_version = importlib.metadata.version("molop")
    except importlib.metadata.PackageNotFoundError:
        molop_version = "unknown"
    report: dict[str, object] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "molop_version": molop_version,
        "workers": workers,
        "parallel_files": parallel_files,
        "measurement_scope": "shared_molop_parse_and_frame_materialization_no_database",
        "parser_input_format": "uncompressed_text",
        "compressed_fixture_expansion_in_timed_window": False,
        "fixture_count": len(paths),
        "status": "passed" if passed else "failed",
        "summary": {
            "file_count": len(paths),
            "succeeded_files": succeeded_files,
            "stored_mib": round(stored_bytes / 1024**2, 3),
            "source_mib": round(source_bytes / 1024**2, 3),
            "source_frames": source_frames,
            "materialized_frames": materialized_frames,
            "failed_inferences": sum(
                int(item.get("failed_inference_count", 0)) for item in results
            ),
            "parse_diagnostics": sum(
                int(item.get("parse_diagnostic_count", 0)) for item in results
            ),
            "segments": sum(len(item.get("segment_frame_counts", [])) for item in results),
            "wall_seconds": round(wall_seconds, 3),
            "verification_seconds": round(verification_seconds, 3),
            "summed_parse_seconds": round(
                sum(float(item.get("elapsed_seconds", 0.0)) for item in results), 3
            ),
            "summed_queue_wait_seconds": round(
                sum(float(item.get("queue_wait_seconds", 0.0)) for item in results), 3
            ),
            "frames_per_second": round(materialized_frames / max(wall_seconds, 0.001), 3),
            "source_mib_per_second": round(source_bytes / max(wall_seconds, 0.001) / 1024**2, 3),
        },
        "results": results,
    }
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    _write_report(arguments.output, serialized)
    if arguments.markdown_output is not None:
        _write_report(arguments.markdown_output, _markdown_report(report))
    print(serialized, end="")
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
