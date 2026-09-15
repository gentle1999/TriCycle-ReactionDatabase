"""Clear materialized parse state before a clean artifact reparse.

RustFS keeps the immutable source bytes. Parsed rows are disposable
materialization, so a reparse starts by removing every previous revision and
its children. This deliberately does not retain ``ParseRevision`` rows:
there is no useful public state in an obsolete interpretation, and keeping it
would allow stale revision joins to be mistaken for current data.

The cleanup is database-only. Parsing, storage, and reaction-profile
calculation stay in their respective services.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, func, update
from sqlmodel import Session, col, select

from tricycle_reaction_db.db.models import (
    CalculationFrame,
    CalculationSegment,
    MappedReaction,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    ParseRevision,
    TransitionStateInference,
)


@dataclass(frozen=True, slots=True)
class ParseCleanupSummary:
    """Rows removed while resetting one or more artifacts for reparse."""

    deleted_revision_ids: tuple[UUID, ...] = ()
    affected_mapped_reaction_ids: tuple[UUID, ...] = ()
    deleted_revision_count: int = 0
    deleted_frame_count: int = 0
    deleted_inference_count: int = 0
    deleted_segment_count: int = 0
    deleted_node_geometry_count: int = 0


def clear_previous_parse_results_batch(
    session: Session,
    *,
    artifact_file_ids: Collection[UUID],
) -> ParseCleanupSummary:
    """Delete all old parse materialization for an artifact set.

    The caller owns the transaction and must commit only after the cleanup is
    complete. Source ``ArtifactFile`` rows and RustFS objects are untouched.
    ``ParseRevision.reparse_of_id`` is detached first so the old revision
    chain can be deleted even though that foreign key is restrictive.
    """

    artifact_ids = set(artifact_file_ids)
    if not artifact_ids:
        return ParseCleanupSummary()

    old_revision_ids = {
        revision_id
        for revision_id in session.exec(
            select(col(ParseRevision.id)).where(
                col(ParseRevision.artifact_file_id).in_(artifact_ids)
            )
        ).all()
        if isinstance(revision_id, UUID)
    }
    if not old_revision_ids:
        return ParseCleanupSummary()

    old_geometry_ids = {
        geometry_id
        for geometry_id in session.exec(
            select(CalculationFrame.geometry_id).where(
                col(CalculationFrame.parse_revision_id).in_(old_revision_ids)
            )
        ).all()
        if isinstance(geometry_id, UUID)
    }
    deleted_frame_count = int(
        session.exec(
            select(func.count())
            .select_from(CalculationFrame)
            .where(col(CalculationFrame.parse_revision_id).in_(old_revision_ids))
        ).one()
    )

    old_logical_reaction_ids = {
        logical_reaction_id
        for logical_reaction_id in session.exec(
            select(TransitionStateInference.logical_reaction_id).where(
                col(TransitionStateInference.parse_revision_id).in_(old_revision_ids)
            )
        ).all()
        if isinstance(logical_reaction_id, UUID)
    }
    old_mapped_reaction_ids = {
        mapped_reaction_id
        for mapped_reaction_id in session.exec(
            select(TransitionStateInference.mapped_reaction_id).where(
                col(TransitionStateInference.parse_revision_id).in_(old_revision_ids)
            )
        ).all()
        if isinstance(mapped_reaction_id, UUID)
    }
    if old_logical_reaction_ids:
        old_mapped_reaction_ids.update(
            mapped_reaction_id
            for mapped_reaction_id in session.exec(
                select(MappedReaction.id).where(
                    col(MappedReaction.logical_reaction_id).in_(old_logical_reaction_ids)
                )
            ).all()
            if isinstance(mapped_reaction_id, UUID)
        )

    inference_delete_result = session.execute(
        delete(TransitionStateInference)
        .where(col(TransitionStateInference.parse_revision_id).in_(old_revision_ids))
        .execution_options(synchronize_session=False)
    )
    deleted_inference_count = int(cast(Any, inference_delete_result).rowcount or 0)

    deleted_segment_count = int(
        session.exec(
            select(func.count())
            .select_from(CalculationSegment)
            .where(col(CalculationSegment.parse_revision_id).in_(old_revision_ids))
        ).one()
    )

    # A Geometry may be shared by another artifact. Only remove bindings for
    # geometries that no longer have any frame source outside this reset set.
    active_geometry_ids = {
        geometry_id
        for geometry_id in session.exec(
            select(CalculationFrame.geometry_id).where(
                col(CalculationFrame.geometry_id).in_(old_geometry_ids),
                col(CalculationFrame.parse_revision_id).not_in(old_revision_ids),
            )
        ).all()
        if isinstance(geometry_id, UUID)
    }
    stale_geometry_ids = old_geometry_ids - active_geometry_ids
    deleted_node_geometry_count = 0
    if stale_geometry_ids and old_mapped_reaction_ids:
        binding_delete_result = session.execute(
            delete(MappedReactionNodeGeometry)
            .where(
                col(MappedReactionNodeGeometry.geometry_id).in_(stale_geometry_ids),
                col(MappedReactionNodeGeometry.mapped_reaction_node_id).in_(
                    select(MappedReactionNode.id).where(
                        col(MappedReactionNode.mapped_reaction_id).in_(old_mapped_reaction_ids)
                    )
                ),
            )
            .execution_options(synchronize_session=False)
        )
        deleted_node_geometry_count = int(cast(Any, binding_delete_result).rowcount or 0)

    # Detach the self-referential chain, including a defensive cross-artifact
    # reference if old data was created before the project boundary trigger.
    # Use one set-based UPDATE instead of loading every dependent revision.
    session.execute(
        update(ParseRevision)
        .where(col(ParseRevision.reparse_of_id).in_(old_revision_ids))
        .values(reparse_of_id=None)
        .execution_options(synchronize_session=False)
    )

    # CalculationSegment owns the frame cascade, and all frame-owned
    # scientific results/endpoints have CASCADE foreign keys. Inferences were
    # deleted first because their frame link is intentionally RESTRICT. A
    # single parent delete lets PostgreSQL perform the whole cascade in one
    # set-based statement; deleting each revision in Python made large reset
    # batches needlessly slow and prone to the normal 15-second query guard.
    session.execute(
        delete(ParseRevision)
        .where(col(ParseRevision.id).in_(old_revision_ids))
        .execution_options(synchronize_session=False)
    )
    session.expire_all()

    return ParseCleanupSummary(
        deleted_revision_ids=tuple(sorted(old_revision_ids, key=str)),
        affected_mapped_reaction_ids=tuple(sorted(old_mapped_reaction_ids, key=str)),
        deleted_revision_count=len(old_revision_ids),
        deleted_frame_count=deleted_frame_count,
        deleted_inference_count=deleted_inference_count,
        deleted_segment_count=deleted_segment_count,
        deleted_node_geometry_count=deleted_node_geometry_count,
    )


__all__ = [
    "ParseCleanupSummary",
    "clear_previous_parse_results_batch",
]
