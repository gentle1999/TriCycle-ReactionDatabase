"""Maintain signed imaginary-mode endpoint evidence for persisted TS frames."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, text
from sqlmodel import col, select

from tricycle_reaction_db.application.services.artifact_uploads import (
    _FailedInference,
    _parse_calculation_output,
    _persist_transition_state_endpoints,
    _resolve_and_bind_transition_state_reaction,
    _SuccessfulInference,
    infer_transition_states_from_calculation_output,
)
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    ArtifactIngestion,
    CalculationFrame,
    LogicalReaction,
    MappedReaction,
    ParseRevision,
    TransitionStateEndpoint,
    TransitionStateInference,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ParseCompleteness,
    TransitionStateInferenceStatus,
)
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
    recovered: int = 0
    invalidated: int = 0


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
    current_logical_reaction_id: UUID | None,
    current_mapped_reaction_id: UUID | None,
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


def _reinference_settings(inferred: _SuccessfulInference | _FailedInference) -> dict[str, Any]:
    """Record the MolOP endpoint policy used for an endpoint re-inference."""

    settings: dict[str, Any] = {
        "endpoint_selection": "molop.possible_pre_post_ts",
    }
    if isinstance(inferred, _SuccessfulInference):
        settings.update(
            {
                "side_topology": "most frequent side topology per signed side",
                "reaction_side_semantics": "fragment-rich endpoint first",
                "direction_semantics": (
                    "measured signed displacement along the imaginary mode; "
                    "negative side displaces along +mode"
                ),
                "imaginary_mode_index": inferred.imaginary_mode_index,
            }
        )
    return settings


def _source_inference_for(
    source_inferences: tuple[_SuccessfulInference | _FailedInference, ...],
    *,
    file_frame_index: int,
    fallback: TransitionStateInference,
) -> _SuccessfulInference | _FailedInference:
    """Return the re-evaluated TS result, including an explicit absent-frame error."""

    inferred = next(
        (item for item in source_inferences if item.file_frame_index == file_frame_index),
        None,
    )
    if isinstance(inferred, (_SuccessfulInference, _FailedInference)):
        return inferred
    return _FailedInference(
        file_frame_index=file_frame_index,
        imaginary_mode_index=fallback.imaginary_mode_index,
        imaginary_frequency_cm1=fallback.imaginary_frequency_cm1,
        error_code="ts_endpoint_not_reproduced",
        error_message="reparsed artifact did not produce a TS inference for this frame",
    )


def _calculation_frame_for_reinference(
    session: Any,
    *,
    inference: TransitionStateInference,
) -> CalculationFrame:
    frame = session.exec(
        select(CalculationFrame).where(
            CalculationFrame.parse_revision_id == inference.parse_revision_id,
            CalculationFrame.file_frame_index == inference.file_frame_index,
        )
    ).first()
    if frame is None:
        raise RuntimeError("persisted calculation is missing the MolOP TS frame")
    return cast(CalculationFrame, frame)


def _mark_reinference_failed(
    session: Any,
    *,
    inference: TransitionStateInference,
    inferred: _FailedInference,
) -> int:
    """Replace one TS inference with its current failed outcome."""

    old_logical_reaction_id = inference.logical_reaction_id
    old_mapped_reaction_id = inference.mapped_reaction_id
    if inference.calculation_frame_id is not None:
        session.exec(
            delete(TransitionStateEndpoint).where(
                col(TransitionStateEndpoint.calculation_frame_id) == inference.calculation_frame_id
            )
        )
    inference.imaginary_mode_index = inferred.imaginary_mode_index
    inference.imaginary_frequency_cm1 = inferred.imaginary_frequency_cm1
    inference.status = TransitionStateInferenceStatus.FAILED
    inference.inference_method = "molop/possible_pre_post_ts"
    inference.inference_settings = _reinference_settings(inferred)
    inference.logical_reaction_id = None
    inference.mapped_reaction_id = None
    inference.calculation_frame_id = None
    inference.error_code = inferred.error_code
    inference.error_message = inferred.error_message
    session.add(inference)
    session.flush()
    return _remove_unreferenced_reaction(
        session,
        old_logical_reaction_id=old_logical_reaction_id,
        old_mapped_reaction_id=old_mapped_reaction_id,
        current_logical_reaction_id=None,
        current_mapped_reaction_id=None,
    )


def _mark_reinference_succeeded(
    session: Any,
    *,
    inference: TransitionStateInference,
    inferred: _SuccessfulInference,
) -> int:
    """Replace endpoint evidence and reaction links with a new TS outcome."""

    frame = _calculation_frame_for_reinference(session, inference=inference)
    old_logical_reaction_id = inference.logical_reaction_id
    old_mapped_reaction_id = inference.mapped_reaction_id
    if inference.calculation_frame_id is not None:
        session.exec(
            delete(TransitionStateEndpoint).where(
                col(TransitionStateEndpoint.calculation_frame_id) == inference.calculation_frame_id
            )
        )
        session.flush()
    logical_reaction_id, mapped_reaction_id = _resolve_and_bind_transition_state_reaction(
        session,
        inferred=inferred,
        calculation_frame=frame,
    )
    _persist_transition_state_endpoints(
        session,
        calculation_frame=frame,
        inferred=inferred,
    )
    inference.imaginary_mode_index = inferred.imaginary_mode_index
    inference.imaginary_frequency_cm1 = inferred.imaginary_frequency_cm1
    inference.status = TransitionStateInferenceStatus.SUCCEEDED
    inference.inference_method = "molop/possible_pre_post_ts"
    inference.inference_settings = _reinference_settings(inferred)
    inference.logical_reaction_id = logical_reaction_id
    inference.mapped_reaction_id = mapped_reaction_id
    inference.calculation_frame_id = frame.id
    inference.error_code = None
    inference.error_message = None
    session.add(inference)
    session.flush()
    return _remove_unreferenced_reaction(
        session,
        old_logical_reaction_id=old_logical_reaction_id,
        old_mapped_reaction_id=old_mapped_reaction_id,
        current_logical_reaction_id=logical_reaction_id,
        current_mapped_reaction_id=mapped_reaction_id,
    )


def _refresh_latest_ingestion_status(
    session: Any,
    *,
    ingestion_id: UUID,
    parse_revision_id: UUID,
) -> None:
    """Refresh only the ingestion status exposed for its latest parse revision."""

    ingestion = session.get(ArtifactIngestion, ingestion_id)
    if ingestion is None:
        raise RuntimeError("TS inference references a missing artifact ingestion")
    latest_revision = session.exec(
        select(ParseRevision)
        .where(ParseRevision.artifact_file_id == ingestion.artifact_file_id)
        .order_by(col(ParseRevision.revision_number).desc())
    ).first()
    if latest_revision is None or latest_revision.id != parse_revision_id:
        return
    has_failed_inference = (
        session.exec(
            select(TransitionStateInference.id).where(
                TransitionStateInference.parse_revision_id == parse_revision_id,
                TransitionStateInference.status == TransitionStateInferenceStatus.FAILED,
            )
        ).first()
        is not None
    )
    ingestion.status = (
        ArtifactIngestionStatus.PARTIAL
        if has_failed_inference or latest_revision.parse_completeness is ParseCompleteness.PARTIAL
        else ArtifactIngestionStatus.SUCCEEDED
    )
    session.add(ingestion)


def _reinfer_all(
    session: Any,
    *,
    limit: int | None,
    inference_id: UUID | None,
    dry_run: bool,
    statement_timeout_ms: int,
) -> BackfillResult:
    """Re-evaluate every persisted TS inference from its immutable raw artifact."""

    result = BackfillResult()
    session.exec(text(f"SET LOCAL statement_timeout = {statement_timeout_ms}"))
    statement = (
        select(TransitionStateInference, ArtifactFile)
        .join(
            ParseRevision,
            col(TransitionStateInference.parse_revision_id) == col(ParseRevision.id),
        )
        .join(ArtifactFile, col(ParseRevision.artifact_file_id) == col(ArtifactFile.id))
        .order_by(col(ArtifactFile.id), col(TransitionStateInference.file_frame_index))
    )
    if inference_id is not None:
        statement = statement.where(col(TransitionStateInference.id) == inference_id)
    if limit is not None:
        statement = statement.limit(limit)

    stores: dict[str, RustFSObjectStore] = {}
    settings = RustFSSettings()
    cached_key: tuple[str, str] | None = None
    parsed: Any | None = None
    touched_ingestions: set[tuple[UUID, UUID]] = set()
    try:
        for inference, artifact in session.exec(statement).all():
            result.scanned += 1
            cache_key = (artifact.bucket, artifact.object_key)
            try:
                if cache_key != cached_key:
                    store = stores.setdefault(
                        artifact.bucket,
                        RustFSObjectStore(settings.model_copy(update={"bucket": artifact.bucket})),
                    )
                    parsed = infer_transition_states_from_calculation_output(
                        store.get_bytes(artifact.object_key),
                        artifact.original_filename,
                    )
                    cached_key = cache_key
                if parsed is None:
                    raise RuntimeError("TS re-inference source cache was not initialized")
                inferred = _source_inference_for(
                    parsed,
                    file_frame_index=inference.file_frame_index,
                    fallback=inference,
                )
                prior_status = inference.status
                if not dry_run:
                    with session.begin_nested():
                        if isinstance(inferred, _SuccessfulInference):
                            result.removed_reactions += _mark_reinference_succeeded(
                                session,
                                inference=inference,
                                inferred=inferred,
                            )
                            if prior_status is TransitionStateInferenceStatus.FAILED:
                                result.recovered += 1
                        else:
                            result.removed_reactions += _mark_reinference_failed(
                                session,
                                inference=inference,
                                inferred=inferred,
                            )
                            if prior_status is TransitionStateInferenceStatus.SUCCEEDED:
                                result.invalidated += 1
                    touched_ingestions.add(
                        (inference.artifact_ingestion_id, inference.parse_revision_id)
                    )
                result.completed += 1
            except Exception as error:
                result.failed += 1
                print(f"failed inference={inference.id}: {type(error).__name__}: {error}")
        if not dry_run:
            for ingestion_id, parse_revision_id in touched_ingestions:
                _refresh_latest_ingestion_status(
                    session,
                    ingestion_id=ingestion_id,
                    parse_revision_id=parse_revision_id,
                )
            session.flush()
    finally:
        for store in stores.values():
            store.close()
    return result


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
                        }
                        for key in ("sampling_min_ratio", "sampling_max_ratio", "sampling_steps"):
                            inference_settings.pop(key, None)
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
                            "endpoint_backfill": {
                                "status": "unavailable",
                                "reason": "source_reparse_mismatch",
                            },
                        }
                        for key in ("sampling_min_ratio", "sampling_max_ratio", "sampling_steps"):
                            inference.inference_settings.pop(key, None)
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
    parser.add_argument(
        "--inference-id",
        type=UUID,
        default=None,
        help="restrict re-inference to one TransitionStateInference UUID",
    )
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
    parser.add_argument(
        "--reinfer-all",
        action="store_true",
        help=(
            "recompute every persisted TS inference, including failed rows, and replace "
            "their endpoint and reaction evidence"
        ),
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if not 100 <= args.statement_timeout_ms <= DEFAULT_STATEMENT_TIMEOUT_MS:
        parser.error(
            f"--statement-timeout-ms must be between 100 and {DEFAULT_STATEMENT_TIMEOUT_MS}"
        )
    if args.reinfer_all and not args.replace:
        parser.error("--reinfer-all requires --replace")

    async with session_factory() as session:
        result = await session.run_sync(
            lambda sync_session: (
                _reinfer_all(
                    sync_session,
                    limit=args.limit,
                    inference_id=args.inference_id,
                    dry_run=args.dry_run,
                    statement_timeout_ms=args.statement_timeout_ms,
                )
                if args.reinfer_all
                else _backfill(
                    sync_session,
                    limit=args.limit,
                    dry_run=args.dry_run,
                    replace=args.replace,
                    statement_timeout_ms=args.statement_timeout_ms,
                )
            )
        )
        if args.dry_run:
            await session.rollback()
        else:
            await session.commit()
    operation = (
        "transition-state re-inference"
        if args.reinfer_all
        else "transition-state endpoint backfill"
    )
    print(
        f"{operation}: "
        f"scanned={result.scanned} completed={result.completed} "
        f"skipped={result.skipped} relinked_reactions={result.relinked_reactions} "
        f"removed_reactions={result.removed_reactions} unavailable={result.unavailable} "
        f"recovered={result.recovered} invalidated={result.invalidated} failed={result.failed}"
    )
    if result.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
