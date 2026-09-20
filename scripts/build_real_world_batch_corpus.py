"""Build a deterministic, balanced fixture corpus from a local complete_set tree.

The source dataset is intentionally not downloaded by CI. This maintainer tool
selects different reaction pairs across the complete sorted corpus, then stores
one real product and one real TS Gaussian log per selected pair.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path
from typing import TypedDict


class FixtureEntry(TypedDict):
    file: str
    archive_file: str
    pair_id: str
    category: str
    source_relative_path: str
    source_sha256: str
    source_size_bytes: int


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        required=True,
        help="local complete_set directory containing reaction/site/prod and ts folders",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="new fixture directory to create",
    )
    parser.add_argument("--file-count", type=int, default=256)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the deterministic sample plan without writing fixture files",
    )
    return parser.parse_args()


def _select_pairs(source_root: Path, pair_count: int) -> list[tuple[str, Path, Path]]:
    if not source_root.is_dir():
        raise ValueError(f"source root does not exist: {source_root}")

    grouped: dict[str, dict[str, Path]] = {}
    for path in sorted(source_root.rglob("*.log")):
        try:
            relative = path.relative_to(source_root)
        except ValueError:
            continue
        if len(relative.parts) < 4:
            continue
        pair_id = relative.parts[0]
        category = path.parent.name
        if category in {"prod", "ts"}:
            grouped.setdefault(pair_id, {}).setdefault(category, path)

    eligible = [
        (pair_id, sources["prod"], sources["ts"])
        for pair_id, sources in sorted(grouped.items())
        if "prod" in sources and "ts" in sources
    ]
    if pair_count > len(eligible):
        raise ValueError(
            f"requested {pair_count} reaction pairs, but only {len(eligible)} have both "
            "prod and ts logs"
        )
    if pair_count < 1:
        raise ValueError("file count must include at least one product/TS pair")

    if pair_count == 1:
        indices = [len(eligible) // 2]
    else:
        indices = [
            round(index * (len(eligible) - 1) / (pair_count - 1)) for index in range(pair_count)
        ]
    return [eligible[index] for index in indices]


def _hash_file(source_path: Path) -> tuple[str, int]:
    source_digest = hashlib.sha256()
    source_size = 0
    with source_path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            source_digest.update(chunk)
            source_size += len(chunk)
    return source_digest.hexdigest(), source_size


def _tar_info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o644
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    return info


def _write_raw_log_archive(
    archive_path: Path,
    plan: list[tuple[str, str, Path]],
    entries: list[FixtureEntry],
    manifest_json: bytes,
) -> None:
    with (
        archive_path.open("wb") as archive_output,
        gzip.GzipFile(
            filename="",
            fileobj=archive_output,
            mode="wb",
            compresslevel=6,
            mtime=0,
        ) as compressed_output,
        tarfile.open(
            fileobj=compressed_output,
            mode="w|",
            format=tarfile.USTAR_FORMAT,
        ) as tar,
    ):
        for (_, _, source_path), entry in zip(plan, entries, strict=True):
            with source_path.open("rb") as source:
                tar.addfile(
                    _tar_info(entry["file"], entry["source_size_bytes"]),
                    source,
                )
        tar.addfile(
            _tar_info("manifest.json", len(manifest_json)),
            io.BytesIO(manifest_json),
        )


def build_corpus(source_root: Path, output_dir: Path, file_count: int) -> dict[str, object]:
    if file_count % 2:
        raise ValueError("file count must be even to balance product and TS logs")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty fixture directory: {output_dir}")

    selected_pairs = _select_pairs(source_root, file_count // 2)
    plan = [
        (pair_id, category, source_path)
        for pair_id, product_path, ts_path in selected_pairs
        for category, source_path in (("prod", product_path), ("ts", ts_path))
    ]
    if len(plan) != file_count:
        raise RuntimeError(f"sample plan selected {len(plan)} files, expected {file_count}")
    if output_dir.exists():
        output_dir.rmdir()
    output_dir.mkdir(parents=True)

    entries: list[FixtureEntry] = []
    for index, (pair_id, category, source_path) in enumerate(plan):
        filename = f"{index:03d}__{pair_id}__{category}.log"
        archive_file = f"corpus-{index // 32 + 1:03d}.tar.gz"
        source_hash, source_size = _hash_file(source_path)
        entries.append(
            {
                "file": filename,
                "archive_file": archive_file,
                "pair_id": pair_id,
                "category": category,
                "source_relative_path": source_path.relative_to(source_root).as_posix(),
                "source_sha256": source_hash,
                "source_size_bytes": source_size,
            }
        )

    manifest: dict[str, object] = {
        "schema_version": "real-world-batch-corpus-v2",
        "source_dataset": "complete_set",
        "sampling": "evenly spaced reaction-pair IDs, one product and one TS log per pair",
        "storage_format": "sharded tar.gz archives containing raw .log members",
        "expected_file_count": file_count,
        "reaction_pair_count": len(selected_pairs),
        "samples": entries,
    }
    manifest_json = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    (output_dir / "manifest.json").write_bytes(manifest_json)
    for shard_index, first_sample in enumerate(range(0, len(plan), 32), start=1):
        last_sample = min(first_sample + 32, len(plan))
        archive_path = output_dir / f"corpus-{shard_index:03d}.tar.gz"
        _write_raw_log_archive(
            archive_path,
            plan[first_sample:last_sample],
            entries[first_sample:last_sample],
            manifest_json,
        )
    return manifest


def main() -> None:
    arguments = _arguments()
    source_root = arguments.source_root.expanduser().resolve()
    output_dir = arguments.output_dir.expanduser().resolve()
    if arguments.dry_run:
        if arguments.file_count % 2:
            raise SystemExit("--file-count must be even")
        selected = _select_pairs(source_root, arguments.file_count // 2)
        source_bytes = sum(
            path.stat().st_size
            for _, product_path, ts_path in selected
            for path in (product_path, ts_path)
        )
        print(
            json.dumps(
                {
                    "source_root": str(source_root),
                    "eligible_reaction_pairs_sampled": len(selected),
                    "file_count": len(selected) * 2,
                    "source_mib": round(source_bytes / 1024**2, 3),
                    "sampled_pair_id_range": [selected[0][0], selected[-1][0]],
                },
                indent=2,
            )
        )
        return

    manifest = build_corpus(source_root, output_dir, arguments.file_count)
    samples = manifest["samples"]
    assert isinstance(samples, list)
    total_source_bytes = sum(int(sample["source_size_bytes"]) for sample in samples)
    archive_paths = sorted(output_dir.glob("corpus-*.tar.gz"))
    archive_size_bytes = sum(path.stat().st_size for path in archive_paths)
    print(
        json.dumps(
            {
                "output_dir": str(output_dir),
                "archive_count": len(archive_paths),
                "archive_mib_each": [
                    round(path.stat().st_size / 1024**2, 3) for path in archive_paths
                ],
                "file_count": manifest["expected_file_count"],
                "reaction_pair_count": manifest["reaction_pair_count"],
                "source_mib": round(total_source_bytes / 1024**2, 3),
                "archive_mib": round(archive_size_bytes / 1024**2, 3),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
