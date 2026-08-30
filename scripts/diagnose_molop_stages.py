"""Measure raw AutoFileParser and ingestion conversion stages on real files."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path


def _parse_only(path: str, capture_source_evidence: bool) -> tuple[float, int]:
    from molop import AutoFileParser

    started = time.perf_counter()
    chem_file = AutoFileParser(
        path,
        parser_detection="auto",
        capture_source_evidence=capture_source_evidence,
        release_file_content=True,
    )
    return time.perf_counter() - started, len(chem_file)


def _parse_and_convert(path: str, capture_source_evidence: bool) -> tuple[float, int]:
    from tricycle_reaction_db.application.services.artifact_uploads import (
        _parse_calculation_path_worker,
    )

    started = time.perf_counter()
    parsed, error = _parse_calculation_path_worker(path, None)
    if parsed is None:
        raise RuntimeError(error or "worker returned no parsed artifact")
    return time.perf_counter() - started, parsed.source_frame_count


async def _selected_paths(root: Path, count: int) -> list[Path]:
    from sqlmodel import select

    from tricycle_reaction_db.db.models import ArtifactFile
    from tricycle_reaction_db.db.session import engine

    candidates = sorted(root.rglob("*.log"))
    if len(candidates) < count:
        raise ValueError(f"{root} has only {len(candidates)} .log files; {count} required")
    async with engine.connect() as connection:
        rows = await connection.execute(select(ArtifactFile.content_sha256))
        existing_hashes = set(rows.scalars().all())
    ordered = [
        candidates[((index * len(candidates)) // count) % len(candidates)]
        for index in range(count * 4)
    ]
    ordered.extend(candidates)
    selected: list[Path] = []
    selected_hashes: set[str] = set()
    for path in ordered:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in existing_hashes or digest in selected_hashes:
            continue
        selected.append(path)
        selected_hashes.add(digest)
        if len(selected) >= count:
            return selected
    raise ValueError(f"could only select {len(selected)} cold files; {count} required")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--count", type=int, default=128)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--capture-source-evidence", action="store_true")
    parser.add_argument("--stage", choices=("parse", "convert"), default="parse")
    args = parser.parse_args()
    paths = asyncio.run(_selected_paths(args.root.resolve(), args.count))
    digest = hashlib.sha256("\n".join(str(path) for path in paths).encode()).hexdigest()
    function = _parse_only if args.stage == "parse" else _parse_and_convert
    started = time.perf_counter()
    results: list[tuple[float, int]] = []
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=args.workers, mp_context=context) as pool:
        futures = [pool.submit(function, str(path), args.capture_source_evidence) for path in paths]
        for future in as_completed(futures):
            results.append(future.result())
    elapsed = time.perf_counter() - started
    print(
        json.dumps(
            {
                "stage": args.stage,
                "capture_source_evidence": args.capture_source_evidence,
                "workers": args.workers,
                "files": len(results),
                "source_names_sha256": digest,
                "wall_seconds": round(elapsed, 3),
                "sum_worker_seconds": round(sum(item[0] for item in results), 3),
                "max_worker_seconds": round(max(item[0] for item in results), 3),
                "source_frames": sum(item[1] for item in results),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
