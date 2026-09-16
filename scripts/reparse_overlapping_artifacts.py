"""Reset and reparse artifacts with overlapping materialized frame results.

The historical bug allowed more than one successful ``ParseRevision`` to
expose the same source frame. This command records the candidate set, removes
all old parse materialization first, and only then reparses the immutable
RustFS bytes through the normal shared pipeline. Legitimate one-revision
single-point artifacts are intentionally not selected.

The checkpoint has separate ``clear`` and ``reparse`` phases. A restart can
therefore resume either phase without ever parsing a file while another
candidate still has its old materialization.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlmodel import col, select

from tricycle_reaction_db.application.dtos import ArtifactUploadResult
from tricycle_reaction_db.application.services.artifact_uploads import (
    ArtifactUploadService,
    close_molop_process_pool,
)
from tricycle_reaction_db.application.services.database_statistics import (
    refresh_project_statistics,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import ArtifactFile, ParseRevision
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactKind,
    StorageStatus,
)


@dataclass(frozen=True, slots=True)
class OverlappingArtifact:
    id: UUID
    project_id: UUID
    content_sha256: str
    original_filename: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reset and reparse artifacts that have more than one materialized ParseRevision"
        )
    )
    parser.add_argument(
        "--project-id",
        action="append",
        type=UUID,
        dest="project_ids",
        help="restrict the repair to one project; repeat for multiple projects",
    )
    parser.add_argument(
        "--user-id",
        type=UUID,
        help="user used for project reparse authorization (defaults to development user)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=32,
        help="artifacts sent to one shared parser/persistence batch (default: 32)",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(".tmp/reparse-overlapping-artifacts-clean-first.jsonl"),
        help="append-only two-phase resumability checkpoint",
    )
    parser.add_argument(
        "--filename-contains",
        type=str,
        default=None,
        help="restrict the repair to ArtifactFile names containing this text",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="process at most this many pending artifacts",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the exact multi-revision candidate set without changing data",
    )
    return parser


async def _load_candidates(
    project_ids: Sequence[UUID] | None,
    *,
    filename_contains: str | None,
) -> list[OverlappingArtifact]:
    multi_revision_artifacts = (
        select(col(ParseRevision.artifact_file_id).label("artifact_file_id"))
        .group_by(col(ParseRevision.artifact_file_id))
        .having(func.count(col(ParseRevision.id)) > 1)
        .subquery()
    )
    statement = (
        select(
            col(ArtifactFile.id),
            col(ArtifactFile.project_id),
            col(ArtifactFile.content_sha256),
            col(ArtifactFile.original_filename),
        )
        .where(
            col(ArtifactFile.artifact_kind) == ArtifactKind.CALCULATION_OUTPUT,
            col(ArtifactFile.storage_status) == StorageStatus.AVAILABLE,
            col(ArtifactFile.id).in_(select(multi_revision_artifacts.c.artifact_file_id)),
        )
        .order_by(col(ArtifactFile.project_id), col(ArtifactFile.id))
    )
    if project_ids:
        statement = statement.where(col(ArtifactFile.project_id).in_(project_ids))
    if filename_contains:
        statement = statement.where(col(ArtifactFile.original_filename).contains(filename_contains))
    async with session_factory() as session:
        rows = (await session.exec(statement)).all()
    candidates: list[OverlappingArtifact] = []
    for artifact_id, project_id, content_sha256, original_filename in rows:
        if not isinstance(artifact_id, UUID) or not isinstance(project_id, UUID):
            raise RuntimeError("overlap candidate is missing its ArtifactFile identity")
        candidates.append(
            OverlappingArtifact(
                id=artifact_id,
                project_id=project_id,
                content_sha256=str(content_sha256),
                original_filename=str(original_filename),
            )
        )
    return candidates


def _candidate_digest(candidates: Sequence[OverlappingArtifact]) -> str:
    payload = [
        {
            "artifact_id": str(candidate.id),
            "project_id": str(candidate.project_id),
            "content_sha256": candidate.content_sha256,
        }
        for candidate in candidates
    ]
    return sha256(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def _load_manifest(path: Path) -> list[OverlappingArtifact]:
    """Load the candidate manifest written before the destructive phase."""

    if not path.exists():
        return []
    candidates: dict[UUID, OverlappingArtifact] = {}
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("phase") != "manifest":
                continue
            try:
                artifact_id = UUID(record["artifact_id"])
                project_id = UUID(record["project_id"])
                content_sha256 = str(record["content_sha256"])
                original_filename = str(record["filename"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"invalid manifest checkpoint at line {line_number}") from error
            candidates[artifact_id] = OverlappingArtifact(
                id=artifact_id,
                project_id=project_id,
                content_sha256=content_sha256,
                original_filename=original_filename,
            )
    return sorted(candidates.values(), key=lambda candidate: (candidate.project_id, candidate.id))


def _load_phase_completed(
    path: Path,
    *,
    phase: str,
    candidates: Sequence[OverlappingArtifact],
) -> set[UUID]:
    completed: set[UUID] = set()
    if not path.exists():
        return completed
    current_candidates = {candidate.id: candidate for candidate in candidates}
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("phase") != phase or record.get("status") != "succeeded":
                continue
            try:
                artifact_id = UUID(record["artifact_id"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"invalid checkpoint at line {line_number}") from error
            candidate = current_candidates.get(artifact_id)
            if candidate is not None and record.get("content_sha256") == candidate.content_sha256:
                completed.add(artifact_id)
    return completed


def _append_manifest(
    path: Path,
    *,
    candidate: OverlappingArtifact,
    candidate_digest: str,
) -> None:
    _append_checkpoint(
        path,
        candidate=candidate,
        candidate_digest=candidate_digest,
        phase="manifest",
        status="selected",
    )


def _append_checkpoint(
    path: Path,
    *,
    candidate: OverlappingArtifact,
    candidate_digest: str,
    phase: str,
    status: str,
    **details: object,
) -> None:
    record: dict[str, object] = {
        "artifact_id": str(candidate.id),
        "project_id": str(candidate.project_id),
        "content_sha256": candidate.content_sha256,
        "filename": candidate.original_filename,
        "candidate_digest": candidate_digest,
        "phase": phase,
        "status": status,
        **details,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()


def _result_status(result: ArtifactUploadResult | Exception | None) -> str:
    if isinstance(result, ArtifactUploadResult):
        return result.ingestion_status.value if result.ingestion_status is not None else "failed"
    return "failed"


async def _run(args: argparse.Namespace) -> int:
    if args.batch_size < 1:
        raise ValueError("batch-size must be positive")
    if args.limit is not None and args.limit < 1:
        raise ValueError("limit must be positive")

    queried_candidates = await _load_candidates(
        args.project_ids,
        filename_contains=args.filename_contains,
    )
    if args.dry_run:
        candidates = (
            queried_candidates[: args.limit] if args.limit is not None else queried_candidates
        )
    else:
        candidates = _load_manifest(args.state_file)
        if not candidates:
            candidates = queried_candidates
            if args.limit is not None:
                candidates = candidates[: args.limit]
            candidate_digest = _candidate_digest(candidates)
            for candidate in candidates:
                _append_manifest(
                    args.state_file,
                    candidate=candidate,
                    candidate_digest=candidate_digest,
                )
    candidate_digest = _candidate_digest(candidates)
    by_project: dict[UUID, list[OverlappingArtifact]] = defaultdict(list)
    for candidate in candidates:
        by_project[candidate.project_id].append(candidate)

    summary = {
        "candidate_digest": candidate_digest,
        "candidate_count": len(candidates),
        "candidate_project_counts": {
            str(project_id): len(project_candidates)
            for project_id, project_candidates in by_project.items()
        },
    }
    if args.dry_run:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0

    settings = get_settings()
    user_id = args.user_id or settings.development_user_id
    clear_completed = _load_phase_completed(
        args.state_file,
        phase="clear",
        candidates=candidates,
    )
    clear_pending = [candidate for candidate in candidates if candidate.id not in clear_completed]
    totals: dict[str, Any] = {
        **summary,
        "clear_checkpointed_before_run": len(clear_completed),
        "clear_pending": len(clear_pending),
        "cleared": 0,
        "clear_failed": 0,
        "reparse_checkpointed_before_run": 0,
        "pending": 0,
        "succeeded": 0,
        "partial": 0,
        "failed": 0,
        "source_frames": 0,
        "transition_state_frames": 0,
        "inferred_reactions": 0,
    }

    clear_pending_by_project: dict[UUID, list[OverlappingArtifact]] = defaultdict(list)
    for candidate in clear_pending:
        clear_pending_by_project[candidate.project_id].append(candidate)

    # Phase one is intentionally complete before phase two starts. A clean
    # reparse must never leave another candidate's obsolete revision visible.
    clear_processed = 0
    clear_error = False
    try:
        for project_id, project_candidates in clear_pending_by_project.items():
            for offset in range(0, len(project_candidates), args.batch_size):
                batch = project_candidates[offset : offset + args.batch_size]
                try:
                    cleanup = await ArtifactUploadService.clear_previous_parse_results(
                        artifact_ids=[candidate.id for candidate in batch],
                        user_id=user_id,
                        refresh_statistics=False,
                    )
                    for candidate in batch:
                        _append_checkpoint(
                            args.state_file,
                            candidate=candidate,
                            candidate_digest=candidate_digest,
                            phase="clear",
                            status="succeeded",
                            deleted_revision_count=cleanup.deleted_revision_count,
                            deleted_frame_count=cleanup.deleted_frame_count,
                            deleted_segment_count=cleanup.deleted_segment_count,
                            deleted_inference_count=cleanup.deleted_inference_count,
                            deleted_node_geometry_count=cleanup.deleted_node_geometry_count,
                        )
                        clear_completed.add(candidate.id)
                        totals["cleared"] += 1
                    print(
                        json.dumps(
                            {
                                "phase": "clear",
                                "project_id": str(project_id),
                                "batch_end": clear_processed + len(batch),
                                "pending_total": len(clear_pending),
                                "cleared": len(batch),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
                except Exception as error:
                    clear_error = True
                    totals["clear_failed"] += len(batch)
                    for candidate in batch:
                        _append_checkpoint(
                            args.state_file,
                            candidate=candidate,
                            candidate_digest=candidate_digest,
                            phase="clear",
                            status="failed",
                            error_code=type(error).__name__,
                            error_message=str(error) or type(error).__name__,
                        )
                    print(
                        f"overlapping-artifact clear batch failed at {clear_processed + 1}-"
                        f"{clear_processed + len(batch)}: {error}",
                        file=sys.stderr,
                        flush=True,
                    )
                clear_processed += len(batch)
        if clear_error or len(clear_completed) != len(candidates):
            for project_id in clear_pending_by_project:
                await refresh_project_statistics(
                    (project_id,),
                    reason="overlapping-artifact-clear-complete",
                )
            totals["failed"] += totals["clear_failed"]
            totals["pending"] = len(candidates) - len(clear_completed)
            print(json.dumps(totals, ensure_ascii=False, sort_keys=True))
            return 1

        reparse_completed = _load_phase_completed(
            args.state_file,
            phase="reparse",
            candidates=candidates,
        )
        pending = [candidate for candidate in candidates if candidate.id not in reparse_completed]
        totals["reparse_checkpointed_before_run"] = len(reparse_completed)
        totals["pending"] = len(pending)
        pending_by_project: dict[UUID, list[OverlappingArtifact]] = defaultdict(list)
        for candidate in pending:
            pending_by_project[candidate.project_id].append(candidate)

        processed = 0
        for project_id, project_candidates in pending_by_project.items():
            for offset in range(0, len(project_candidates), args.batch_size):
                batch = project_candidates[offset : offset + args.batch_size]
                try:
                    results = await ArtifactUploadService.reparse_batch(
                        artifact_ids=[candidate.id for candidate in batch],
                        user_id=user_id,
                        force_reparse=True,
                        previous_results_cleared=True,
                        refresh_statistics=False,
                    )
                    for candidate in batch:
                        result = results.get(candidate.id)
                        status = _result_status(result)
                        if status == ArtifactIngestionStatus.SUCCEEDED.value:
                            if not isinstance(result, ArtifactUploadResult):
                                raise RuntimeError("successful reparse has no result")
                            totals["succeeded"] += 1
                            totals["source_frames"] += result.source_frame_count or 0
                            totals["transition_state_frames"] += (
                                result.transition_state_frame_count or 0
                            )
                            totals["inferred_reactions"] += result.inferred_reaction_count
                            _append_checkpoint(
                                args.state_file,
                                candidate=candidate,
                                candidate_digest=candidate_digest,
                                phase="reparse",
                                status="succeeded",
                                ingestion_status=status,
                                ingestion_id=(
                                    str(result.ingestion_id)
                                    if result.ingestion_id is not None
                                    else None
                                ),
                                parse_revision_id=(
                                    str(result.parse_revision_id)
                                    if result.parse_revision_id is not None
                                    else None
                                ),
                            )
                        else:
                            if status == ArtifactIngestionStatus.PARTIAL.value:
                                totals["partial"] += 1
                            else:
                                totals["failed"] += 1
                            _append_checkpoint(
                                args.state_file,
                                candidate=candidate,
                                candidate_digest=candidate_digest,
                                phase="reparse",
                                status="partial" if status == "partial" else "failed",
                                ingestion_status=status,
                                error_message=(
                                    str(result) if isinstance(result, Exception) else None
                                ),
                            )
                    print(
                        json.dumps(
                            {
                                "phase": "reparse",
                                "project_id": str(project_id),
                                "batch_end": processed + len(batch),
                                "pending_total": len(pending),
                                "succeeded": sum(
                                    _result_status(results.get(candidate.id)) == "succeeded"
                                    for candidate in batch
                                ),
                                "failed_or_partial": sum(
                                    _result_status(results.get(candidate.id)) != "succeeded"
                                    for candidate in batch
                                ),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
                except Exception as error:
                    totals["failed"] += len(batch)
                    for candidate in batch:
                        _append_checkpoint(
                            args.state_file,
                            candidate=candidate,
                            candidate_digest=candidate_digest,
                            phase="reparse",
                            status="failed",
                            error_code=type(error).__name__,
                            error_message=str(error) or type(error).__name__,
                        )
                    print(
                        f"overlapping-artifact reparse batch failed at {processed + 1}-"
                        f"{processed + len(batch)}: {error}",
                        file=sys.stderr,
                        flush=True,
                    )
                processed += len(batch)
            await refresh_project_statistics(
                (project_id,),
                reason="overlapping-artifact-reparse-complete",
            )
    finally:
        await close_molop_process_pool()

    print(json.dumps(totals, ensure_ascii=False, sort_keys=True))
    return 1 if totals["failed"] or totals["partial"] else 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(_run(_parser().parse_args())))
    except (ValueError, OSError) as error:
        print(f"overlapping-artifact reparse failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
