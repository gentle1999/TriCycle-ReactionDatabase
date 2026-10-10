"""File-level cost of all recorded candidates belonging to a mapped reaction."""

from collections.abc import Iterable, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import case, literal
from sqlmodel import Session, col, select

from tricycle_reaction_db.db.models import (
    ArtifactFile,
    CalculationFrame,
    Geometry,
    MappedReaction,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionParticipant,
    ParseRevision,
    TransitionStateInference,
)
from tricycle_reaction_db.domain.enums import (
    LogicalReactionParticipantSide,
    MappedReactionNodeRole,
    StorageStatus,
)

RUNTIME_COLUMNS = (
    "reactants_running_time_seconds",
    "transition_state_running_time_seconds",
    "products_running_time_seconds",
    "total_running_time_seconds",
)


def aggregate_mapped_reaction_runtimes(
    mapped_reaction_ids: Sequence[UUID], rows: Iterable[Any]
) -> dict[UUID, dict[str, float | None]]:
    """Deduplicate files per stage and across the complete mapping's search."""

    files: dict[UUID, dict[str, dict[UUID, tuple[int, float | None]]]] = {
        mid: {role: {} for role in ("reactants", "transition_state", "products", "total")}
        for mid in mapped_reaction_ids
    }
    for mid, role, artifact_id, revision, runtime in rows:
        for stage in (role, "total"):
            previous = files[mid][stage].get(artifact_id)
            if previous is None or int(revision) > previous[0]:
                files[mid][stage][artifact_id] = (int(revision), runtime)

    def total(entries: dict[UUID, tuple[int, float | None]]) -> float | None:
        if not entries or any(value is None for _, value in entries.values()):
            return None
        return round(sum(float(value) for _, value in entries.values() if value is not None), 6)

    return {
        mid: {f"{stage}_running_time_seconds": total(entries) for stage, entries in stages.items()}
        for mid, stages in files.items()
    }


def load_mapped_reaction_runtimes(
    session: Session, mapped_reaction_ids: Sequence[UUID]
) -> dict[UUID, dict[str, float | None]]:
    """Count all attributable logs, without energy or convergence screening.

    Endpoint candidates belong to the exact concrete participant topologies;
    TS candidates come from mapping bindings and inference source frames.
    Failed/non-minimum candidates with persisted source evidence still cost
    time. Source ownership is enforced independently of profile selection.
    """

    if not mapped_reaction_ids:
        return {}

    def sources(mapped_id: Any, role: Any) -> tuple[Any, ...]:
        return (
            mapped_id,
            role,
            col(ArtifactFile.id),
            col(ParseRevision.revision_number),
            col(ParseRevision.running_time_seconds),
        )

    def file_sources(statement: Any) -> Any:
        return (
            statement.join(
                ParseRevision, col(CalculationFrame.parse_revision_id) == col(ParseRevision.id)
            )
            .join(ArtifactFile, col(ParseRevision.artifact_file_id) == col(ArtifactFile.id))
            .where(
                col(MappedReaction.id).in_(mapped_reaction_ids),
                col(ArtifactFile.project_id) == col(MappedReaction.project_id),
                col(ArtifactFile.storage_status) != StorageStatus.RETIRED,
            )
        )

    endpoint_role = case(
        (
            col(MappedReactionParticipant.side) == LogicalReactionParticipantSide.REACTANT,
            literal("reactants"),
        ),
        else_=literal("products"),
    )
    endpoints = file_sources(
        select(*sources(col(MappedReaction.id), endpoint_role))
        .select_from(MappedReactionParticipant)
        .join(
            MappedReaction,
            col(MappedReactionParticipant.mapped_reaction_id) == col(MappedReaction.id),
        )
        .join(
            Geometry,
            col(Geometry.topology_id) == col(MappedReactionParticipant.concrete_topology_id),
        )
        .join(CalculationFrame, col(CalculationFrame.geometry_id) == col(Geometry.id))
        .where(col(Geometry.project_id) == col(MappedReaction.project_id))
    )
    binding_role = case(
        (
            col(MappedReactionNode.role).in_(
                [
                    MappedReactionNodeRole.REACTANT,
                    MappedReactionNodeRole.REACTANT_COMPLEX,
                ]
            ),
            literal("reactants"),
        ),
        (
            col(MappedReactionNode.role).in_(
                [
                    MappedReactionNodeRole.PRODUCT,
                    MappedReactionNodeRole.PRODUCT_COMPLEX,
                ]
            ),
            literal("products"),
        ),
        else_=literal("transition_state"),
    )
    bindings = file_sources(
        select(*sources(col(MappedReaction.id), binding_role))
        .select_from(MappedReactionNode)
        .join(MappedReaction, col(MappedReactionNode.mapped_reaction_id) == col(MappedReaction.id))
        .join(
            MappedReactionNodeGeometry,
            col(MappedReactionNodeGeometry.mapped_reaction_node_id) == col(MappedReactionNode.id),
        )
        .join(
            CalculationFrame,
            col(CalculationFrame.geometry_id) == col(MappedReactionNodeGeometry.geometry_id),
        )
        .where(
            col(MappedReactionNode.role).in_(
                [
                    MappedReactionNodeRole.REACTANT,
                    MappedReactionNodeRole.REACTANT_COMPLEX,
                    MappedReactionNodeRole.TRANSITION_STATE,
                    MappedReactionNodeRole.PRODUCT,
                    MappedReactionNodeRole.PRODUCT_COMPLEX,
                ]
            )
        )
    )
    inferences = file_sources(
        select(*sources(col(MappedReaction.id), literal("transition_state")))
        .select_from(TransitionStateInference)
        .join(
            MappedReaction,
            col(TransitionStateInference.mapped_reaction_id) == col(MappedReaction.id),
        )
        .join(
            CalculationFrame,
            col(TransitionStateInference.calculation_frame_id) == col(CalculationFrame.id),
        )
    )
    # UNION eliminates repeated frame/Geometry witnesses of the same file.
    rows = session.exec(endpoints.union(bindings, inferences)).all()
    return aggregate_mapped_reaction_runtimes(mapped_reaction_ids, rows)
