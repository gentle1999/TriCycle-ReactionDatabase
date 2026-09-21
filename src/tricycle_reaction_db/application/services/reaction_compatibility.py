"""Reaction-level summaries of explicitly unverified TS endpoint evidence."""

from typing import Any, cast

from sqlalchemy import and_, func, or_
from sqlmodel import col, select

from tricycle_reaction_db.application.dtos.query_views import (
    LogicalReactionDetail,
    LogicalReactionPage,
    LogicalReactionSummary,
    MappedReactionPage,
    MappedReactionSummary,
)
from tricycle_reaction_db.db.models import LogicalReaction, MappedReaction, TransitionStateInference
from tricycle_reaction_db.db.session import session_factory

FALLBACK_FILTERS = frozenset(
    {
        "has_compatibility_endpoints",
        "has_single_endpoint_fallback",
        "has_dual_endpoint_fallback",
    }
)


def fallback_sides() -> tuple[Any, Any]:
    settings = col(TransitionStateInference.inference_settings)
    negative, positive = (
        func.coalesce(
            settings["endpoint_validation"][side]["validation_status"].as_string() == "unverified",
            False,
        )
        for side in ("negative", "positive")
    )
    return negative, positive


def fallback_predicate(field: str, *, mapped: bool = False) -> Any:
    negative, positive = fallback_sides()
    condition = {
        "has_compatibility_endpoints": or_(negative, positive),
        "has_single_endpoint_fallback": negative != positive,
        "has_dual_endpoint_fallback": and_(negative, positive),
    }[field]
    source = (
        col(TransitionStateInference.mapped_reaction_id)
        if mapped
        else col(TransitionStateInference.logical_reaction_id)
    )
    target = col(MappedReaction.id) if mapped else col(LogicalReaction.id)
    return select(TransitionStateInference.id).where(source == target, condition).exists()


async def annotate_reaction_compatibility[
    ResultT: LogicalReactionPage
    | MappedReactionPage
    | LogicalReactionSummary
    | MappedReactionSummary
](result: ResultT) -> ResultT:
    """One batched lookup for already-authorized result IDs, never an N+1 query.

    Mixed evidence keeps the warning: a strict source must not hide an unverified
    source. Dual means both sides of the SAME inference, not two different files.
    Missing historical evidence is not inferred to be strict.
    """
    summaries: list[LogicalReactionSummary | MappedReactionSummary] = (
        list(result.items)
        if isinstance(result, (LogicalReactionPage, MappedReactionPage))
        else [result]
    )
    for summary in list(summaries):
        summaries.extend(getattr(summary, "mapped_reactions", []))
    logical_ids = {s.id for s in summaries if isinstance(s, LogicalReactionSummary)}
    mapped_ids = {s.id for s in summaries if not isinstance(s, LogicalReactionSummary)}
    if not summaries:
        return result
    negative, positive = fallback_sides()
    async with session_factory() as session:
        rows = (
            await session.exec(
                select(
                    col(TransitionStateInference.logical_reaction_id),
                    col(TransitionStateInference.mapped_reaction_id),
                    negative,
                    positive,
                ).where(
                    or_(
                        col(TransitionStateInference.logical_reaction_id).in_(logical_ids),
                        col(TransitionStateInference.mapped_reaction_id).in_(mapped_ids),
                    ),
                    or_(negative, positive),
                )
            )
        ).all()
    evidence: dict[tuple[bool, Any], set[int]] = {}
    for logical_id, mapped_id, neg, pos in rows:
        count = int(neg) + int(pos)
        evidence.setdefault((True, logical_id), set()).add(count)
        evidence.setdefault((False, mapped_id), set()).add(count)

    def annotated[SummaryT: LogicalReactionSummary | MappedReactionSummary](
        summary: SummaryT,
    ) -> SummaryT:
        counts = evidence.get((isinstance(summary, LogicalReactionSummary), summary.id), set())
        updates: dict[str, Any] = {
            "has_compatibility_endpoints": bool(counts),
            "has_single_endpoint_fallback": 1 in counts,
            "has_dual_endpoint_fallback": 2 in counts,
        }
        if isinstance(summary, LogicalReactionDetail):
            updates["mapped_reactions"] = [annotated(item) for item in summary.mapped_reactions]
        return cast(SummaryT, summary.model_copy(update=updates))

    if isinstance(result, (LogicalReactionPage, MappedReactionPage)):
        return cast(
            ResultT,
            result.model_copy(update={"items": [annotated(item) for item in result.items]}),
        )
    return annotated(result)
