"""Clear parsed materialization for an explicit set of artifact files.

The command keeps the immutable ArtifactFile/RustFS source and removes every
revision-owned parse row in one authorized transaction. The selected
calculation ingestions are reset to ``pending`` so the upload worker can pick
them up again. Pass ``--artifact-id`` repeatedly or provide a whitespace-,
comma-, or newline-separated ID file with ``--artifact-id-file``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from uuid import UUID

from tricycle_reaction_db.application.services.artifact_uploads import (
    ArtifactUploadError,
    ArtifactUploadService,
)
from tricycle_reaction_db.core.config import get_settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Delete all ParseRevision materialization for explicit calculation "
            "artifacts and reset their ingestions to pending"
        )
    )
    parser.add_argument(
        "--artifact-id",
        action="append",
        dest="artifact_ids",
        type=UUID,
        help="ArtifactFile UUID; repeat for every file in the set",
    )
    parser.add_argument(
        "--artifact-id-file",
        action="append",
        type=Path,
        dest="artifact_id_files",
        help="file containing UUIDs separated by whitespace, commas, or newlines; use - for stdin",
    )
    parser.add_argument(
        "--user-id",
        type=UUID,
        help="user used for project authorization (defaults to the development user)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and report the number of requested IDs without changing the database",
    )
    return parser


def _tokens_from_lines(lines: Iterable[str]) -> list[UUID]:
    artifact_ids: list[UUID] = []
    for line_number, line in enumerate(lines, start=1):
        content = line.split("#", 1)[0]
        for token in content.replace(",", " ").split():
            try:
                artifact_ids.append(UUID(token))
            except ValueError as error:
                raise ValueError(
                    f"invalid artifact UUID at input line {line_number}: {token}"
                ) from error
    return artifact_ids


def _load_ids(paths: Iterable[Path] | None) -> list[UUID]:
    artifact_ids: list[UUID] = []
    for path in paths or ():
        if str(path) == "-":
            artifact_ids.extend(_tokens_from_lines(sys.stdin))
        else:
            with path.open("r", encoding="utf-8") as stream:
                artifact_ids.extend(_tokens_from_lines(stream))
    return artifact_ids


def _requested_ids(arguments: argparse.Namespace) -> tuple[UUID, ...]:
    artifact_ids = [*(arguments.artifact_ids or ()), *_load_ids(arguments.artifact_id_files)]
    ordered_unique = tuple(dict.fromkeys(artifact_ids))
    if not ordered_unique:
        raise ValueError("provide at least one --artifact-id or --artifact-id-file")
    return ordered_unique


async def _run(arguments: argparse.Namespace) -> int:
    artifact_ids = _requested_ids(arguments)
    if arguments.dry_run:
        print(json.dumps({"artifact_count": len(artifact_ids)}, sort_keys=True))
        return 0

    cleanup = await ArtifactUploadService.clear_previous_parse_results(
        artifact_ids=artifact_ids,
        user_id=arguments.user_id or get_settings().development_user_id,
    )
    print(
        json.dumps(
            {
                "artifact_count": len(artifact_ids),
                "deleted_revision_count": cleanup.deleted_revision_count,
                "deleted_segment_count": cleanup.deleted_segment_count,
                "deleted_frame_count": cleanup.deleted_frame_count,
                "deleted_inference_count": cleanup.deleted_inference_count,
                "deleted_node_geometry_count": cleanup.deleted_node_geometry_count,
                "affected_mapped_reaction_count": len(cleanup.affected_mapped_reaction_ids),
                "ingestions_reset_to_pending": True,
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(_run(_parser().parse_args())))
    except (ArtifactUploadError, OSError, ValueError) as error:
        print(f"artifact parse cleanup failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
