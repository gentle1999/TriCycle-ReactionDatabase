"""Add signed imaginary-mode anchors to existing successful TS inferences."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import delete, text
from sqlmodel import col, select

from tricycle_reaction_db.application.services.transition_state_uploads import (
    TS_PRE_POST_MAX_RATIO,
    TS_PRE_POST_MIN_RATIO,
    TS_PRE_POST_STEPS,
    _parse_calculation_output,
    _persist_transition_state_endpoints,
    _resolve_and_bind_transition_state_reaction,
    _SuccessfulInference,
)
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    CalculationFrame,
    LogicalReaction,
    MappedReaction,
    ParseRevision,
    TransitionStateEndpoint,
    TransitionStateInference,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import TransitionStateInferenceStatus
from tricycle_reaction_db.storage.rustfs import RustFSObjectStore, RustFSSettings


@dataclass(slots=True)
class BackfillResult:
    scanned: int = 0
    completed: int = 0
    skipped: int = 0
    relinked_reactions: int = 0
    removed_reactions: int = 0
    unavailable: int = 0
    failed: int = 0


DEFAULT_STATEMENT_TIMEOUT_MS = 300_000


class SourceReparseMismatch(RuntimeError):
    """A historical successful inference no longer reproduces from its source."""


def _has_both_endpoints(session: Any, frame_id: object) -> bool:
    return (
        len(
            session.exec(
                select(TransitionStateEndpoint.id).where(
                    TransitionStateEndpoint.calculation_frame_id == frame_id
                )
            ).all()
        )
        == 2
    )


def _remove_unreferenced_reaction(
    session: Any,
    *,
    old_logical_reaction_id: UUID | None,
    old_mapped_reaction_id: UUID | None,
    current_logical_reaction_id: UUID,
    current_mapped_reaction_id: UUID,
) -> int:
    """Remove only the obsolete reaction facts left by a relinked inference."""

    if old_mapped_reaction_id is None or old_mapped_reaction_id == current_mapped_reaction_id:
        return 0
    if (
        session.exec(
            select(TransitionStateInference.id).where(
                TransitionStateInference.mapped_reaction_id == old_mapped_reaction_id
            )
        ).first()
        is not None
    ):
        return 0
    session.exec(delete(MappedReaction).where(col(MappedReaction.id) == old_mapped_reaction_id))
    session.flush()

    if (
        old_logical_reaction_id is None
        or old_logical_reaction_id == current_logical_reaction_id
        or session.exec(
            select(MappedReaction.id).where(
                MappedReaction.logical_reaction_id == old_logical_reaction_id
            )
        ).first()
        is not None
        or session.exec(
            select(TransitionStateInference.id).where(
                TransitionStateInference.logical_reaction_id == old_logical_reaction_id
            )
        ).first()
        is not None
    ):
        return 1
    session.exec(delete(LogicalReaction).where(col(LogicalReaction.id) == old_logical_reaction_id))
    session.flush()
    return 2


def _backfill(
    session: Any,
    *,
    limit: int | None,
    dry_run: bool,
    replace: bool,
    statement_timeout_ms: int,
) -> BackfillResult:
    result = BackfillResult()
    session.exec(text(f"SET LOCAL statement_timeout = {statement_timeout_ms}"))
    statement = (
        select(TransitionStateInference, ArtifactFile)
        .join(
            ParseRevision,
            col(TransitionStateInference.parse_revision_id) == col(ParseRevision.id),
        )
        .join(ArtifactFile, col(ParseRevision.artifact_file_id) == col(ArtifactFile.id))
        .where(
            col(TransitionStateInference.status) == TransitionStateInferenceStatus.SUCCEEDED,
            col(TransitionStateInference.calculation_frame_id).is_not(None),
        )
        .order_by(col(ArtifactFile.id), col(TransitionStateInference.file_frame_index))
    )
    if limit is not None:
        statement = statement.limit(limit)

    parsed_cache: dict[tuple[str, str], Any] = {}
    stores: dict[str, RustFSObjectStore] = {}
    settings = RustFSSettings()
    try:
        for inference, artifact in session.exec(statement).all():
            result.scanned += 1
            frame_id = inference.calculation_frame_id
            if frame_id is None:
                result.failed += 1
                continue
            if not replace and _has_both_endpoints(session, frame_id):
                result.skipped += 1
                continue
            cache_key = (artifact.bucket, artifact.object_key)
            try:
                parsed = parsed_cache.get(cache_key)
                if parsed is None:
                    store = stores.setdefault(
                        artifact.bucket,
                        RustFSObjectStore(settings.model_copy(update={"bucket": artifact.bucket})),
                    )
                    parsed = _parse_calculation_output(
                        store.get_bytes(artifact.object_key),
                        artifact.original_filename,
                    )
                    parsed_cache[cache_key] = parsed
                inferred = next(
                    (
                        item
                        for item in parsed.inferences
                        if isinstance(item, _SuccessfulInference)
                        and item.file_frame_index == inference.file_frame_index
                    ),
                    None,
                )
                if inferred is None:
                    raise SourceReparseMismatch(
                        "reparsed artifact did not reproduce a successful TS endpoint"
                    )
                frame = session.get(CalculationFrame, frame_id)
                if frame is None:
                    raise RuntimeError("TS inference references a missing CalculationFrame")
                if not dry_run:
                    with session.begin_nested():
                        if replace:
                            session.exec(
                                delete(TransitionStateEndpoint).where(
                                    TransitionStateEndpoint.calculation_frame_id == frame_id
                                )
                            )
                            session.flush()
                        _persist_transition_state_endpoints(
                            session,
                            calculation_frame=frame,
                            inferred=inferred,
                        )
                        old_logical_reaction_id = inference.logical_reaction_id
                        old_mapped_reaction_id = inference.mapped_reaction_id
                        logical_reaction_id, mapped_reaction_id = (
                            _resolve_and_bind_transition_state_reaction(
                                session,
                                inferred=inferred,
                                calculation_frame=frame,
                            )
                        )
                        inference.logical_reaction_id = logical_reaction_id
                        inference.mapped_reaction_id = mapped_reaction_id
                        inference_settings = {
                            **inference.inference_settings,
                            "endpoint_selection": "molop.possible_pre_post_ts",
                            "sampling_min_ratio": TS_PRE_POST_MIN_RATIO,
                            "sampling_max_ratio": TS_PRE_POST_MAX_RATIO,
                            "sampling_steps": TS_PRE_POST_STEPS,
                        }
                        inference_settings.pop("ratio_attempts", None)
                        inference_settings.pop("steps", None)
                        inference_settings.pop("endpoint_backfill", None)
                        inference.inference_settings = inference_settings
                        session.add(inference)
                        session.flush()
                        if old_mapped_reaction_id != mapped_reaction_id:
                            result.relinked_reactions += 1
                            result.removed_reactions += _remove_unreferenced_reaction(
                                session,
                                old_logical_reaction_id=old_logical_reaction_id,
                                old_mapped_reaction_id=old_mapped_reaction_id,
                                current_logical_reaction_id=logical_reaction_id,
                                current_mapped_reaction_id=mapped_reaction_id,
                            )
                result.completed += 1
            except SourceReparseMismatch as error:
                if not dry_run and replace:
                    with session.begin_nested():
                        session.exec(
                            delete(TransitionStateEndpoint).where(
                                TransitionStateEndpoint.calculation_frame_id == frame_id
                            )
                        )
                        inference.inference_settings = {
                            **inference.inference_settings,
                            "endpoint_selection": "molop.possible_pre_post_ts",
                            "sampling_min_ratio": TS_PRE_POST_MIN_RATIO,
                            "sampling_max_ratio": TS_PRE_POST_MAX_RATIO,
                            "sampling_steps": TS_PRE_POST_STEPS,
                            "endpoint_backfill": {
                                "status": "unavailable",
                                "reason": "source_reparse_mismatch",
                            },
                        }
                        session.add(inference)
                result.unavailable += 1
                print(f"unavailable inference={inference.id}: {error}")
            except Exception as error:
                result.failed += 1
                print(f"failed inference={inference.id}: {type(error).__name__}: {error}")
    finally:
        for store in stores.values():
            store.close()
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--statement-timeout-ms",
        type=int,
        default=DEFAULT_STATEMENT_TIMEOUT_MS,
        help="per-statement timeout for this offline maintenance run",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace existing anchors using the current MolOP pre/post-TS endpoints",
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if not 100 <= args.statement_timeout_ms <= DEFAULT_STATEMENT_TIMEOUT_MS:
        parser.error(
            f"--statement-timeout-ms must be between 100 and {DEFAULT_STATEMENT_TIMEOUT_MS}"
        )

    async with session_factory() as session:
        result = await session.run_sync(
            lambda sync_session: _backfill(
                sync_session,
                limit=args.limit,
                dry_run=args.dry_run,
                replace=args.replace,
                statement_timeout_ms=args.statement_timeout_ms,
            )
        )
        if args.dry_run:
            await session.rollback()
        else:
            await session.commit()
    print(
        "transition-state endpoint backfill: "
        f"scanned={result.scanned} completed={result.completed} "
        f"skipped={result.skipped} relinked_reactions={result.relinked_reactions} "
        f"removed_reactions={result.removed_reactions} unavailable={result.unavailable} "
        f"failed={result.failed}"
    )
    if result.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
