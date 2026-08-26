"""Measure MolOP batch RSS and process counts in isolated child processes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter, sleep
from typing import TypedDict

from tricycle_reaction_db.application.services.artifact_uploads import (
    _parse_calculation_outputs_batch,
)

REPOSITORY_ROOT = Path(__file__).parents[1]
UPLOAD_BENCHMARK_SCHEMA_VERSION = "upload-resource-benchmark-v2"


class ProcessSample(TypedDict):
    rss_kib: int
    child_count: int


def _process_table() -> dict[int, tuple[int, int]]:
    table: dict[int, tuple[int, int]] = {}
    for status_path in Path("/proc").glob("[0-9]*/status"):
        try:
            values: dict[str, str] = {}
            for line in status_path.read_text(encoding="utf-8").splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    values[key] = value.strip()
            pid = int(status_path.parent.name)
            ppid = int(values["PPid"])
            rss_kib = int(values.get("VmRSS", "0 kB").split()[0])
            table[pid] = (ppid, rss_kib)
        except (FileNotFoundError, KeyError, PermissionError, ProcessLookupError, ValueError):
            continue
    return table


def _sample_process_tree(root_pid: int) -> ProcessSample:
    table = _process_table()
    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _) in table.items():
            if ppid in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    return {
        "rss_kib": sum(table.get(pid, (0, 0))[1] for pid in descendants),
        "child_count": max(0, len(descendants) - 1),
    }


def _child(batch_size: int, n_jobs: int, fixture: Path) -> dict[str, object]:
    payload = fixture.read_bytes()
    stop = threading.Event()
    peak = _sample_process_tree(os.getpid())

    def monitor() -> None:
        nonlocal peak
        while not stop.is_set():
            sample = _sample_process_tree(os.getpid())
            peak = {
                "rss_kib": max(peak["rss_kib"], sample["rss_kib"]),
                "child_count": max(peak["child_count"], sample["child_count"]),
            }
            sleep(0.01)

    monitor_thread = threading.Thread(target=monitor, daemon=True)
    monitor_thread.start()
    started_at = perf_counter()
    phase_timings_ms: dict[str, float] = {}
    try:
        results = _parse_calculation_outputs_batch(
            [(payload, f"baseline-{index:02d}{fixture.suffix}") for index in range(batch_size)],
            n_jobs=n_jobs,
            timings_ms=phase_timings_ms,
        )
    finally:
        elapsed_seconds = perf_counter() - started_at
        stop.set()
        monitor_thread.join(timeout=2)
    failures = sum(isinstance(result, Exception) for result in results.values())
    try:
        fixture_label = str(fixture.relative_to(REPOSITORY_ROOT))
    except ValueError:
        fixture_label = str(fixture)
    return {
        "batch_size": batch_size,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "fixture": fixture_label,
        "n_jobs": n_jobs,
        "peak_child_processes": peak["child_count"],
        "peak_process_tree_rss_mib": round(peak["rss_kib"] / 1024, 1),
        "succeeded_count": len(results) - failures,
        "failed_count": failures,
        "phase_timings_ms": {
            phase: round(elapsed, 3) for phase, elapsed in phase_timings_ms.items()
        },
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-sizes", type=int, nargs="+", default=[1, 8, 32])
    parser.add_argument("--n-jobs", type=int, default=2)
    parser.add_argument(
        "--fixture",
        type=Path,
        required=True,
        help="real Gaussian/ORCA file; synthetic repository fixtures are not allowed",
    )
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--child-size", type=int, default=None, help=argparse.SUPPRESS)
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
        "with --fixture"
    )


def main() -> None:
    arguments = _arguments()
    fixture = _reject_synthetic_fixture(arguments.fixture)
    if arguments.child_size is not None:
        print(json.dumps(_child(arguments.child_size, arguments.n_jobs, fixture), sort_keys=True))
        return

    if any(batch_size <= 0 for batch_size in arguments.batch_sizes):
        raise SystemExit("--batch-sizes values must be positive")
    if arguments.n_jobs <= 0:
        raise SystemExit("--n-jobs must be positive")
    if not fixture.is_file():
        raise SystemExit(f"fixture does not exist: {fixture}")

    results: list[dict[str, object]] = []
    for batch_size in arguments.batch_sizes:
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--child-size",
            str(batch_size),
            "--n-jobs",
            str(arguments.n_jobs),
            "--fixture",
            str(fixture),
        ]
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
        results.append(json.loads(completed.stdout.strip().splitlines()[-1]))
    serialized = (
        json.dumps(
            {
                "schema_version": UPLOAD_BENCHMARK_SCHEMA_VERSION,
                "generated_at": datetime.now(UTC).isoformat(),
                "node": socket.gethostname(),
                "fixture": str(fixture),
                "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
                "n_jobs": arguments.n_jobs,
                "batch_sizes": arguments.batch_sizes,
                "results": results,
                "succeeded": all(result.get("failed_count") == 0 for result in results),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    if arguments.output is None:
        print(serialized, end="")
    else:
        arguments.output.write_text(serialized, encoding="utf-8")


if __name__ == "__main__":
    main()
