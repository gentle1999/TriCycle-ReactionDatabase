"""Idempotent links between reusable geometries and mapped reaction nodes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import and_
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.dtos import (
    MappedReactionEdgeRecord,
    MappedReactionNodeGeometryMappingRecord,
    MappedReactionNodeGeometryRecord,
    MappedReactionNodeRecord,
)
from tricycle_reaction_db.application.services._persistence import (
    _acquire_identity_locks,
    _require_id,
)
from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence import (
    refresh_mapped_reaction_thermodynamics,
)
from tricycle_reaction_db.application.services.reaction_geometry_policy import (
    geometry_has_thermodynamic_property,
    geometry_has_thermodynamic_property_predicate,
)
from tricycle_reaction_db.application.services.reactions import (
    _reaction_mapping_isomorphic,
    atom_maps_from_source_order,
    mapped_smiles_for_topology,
    persist_mapped_reaction_edge,
    persist_mapped_reaction_node,
    persist_mapped_reaction_node_geometry,
    persist_mapped_reaction_node_geometry_mapping,
)
from tricycle_reaction_db.db.models import (
    CalculationFrame,
    Geometry,
    LogicalReactionParticipant,
    MappedReaction,
    MappedReactionEdge,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionNodeGeometryMapping,
    MappedReactionParticipant,
)
from tricycle_reaction_db.domain.enums import (
    LogicalReactionParticipantSide,
    MappedReactionEdgeKind,
    MappedReactionNodeRole,
    OptimizationStatus,
)
from tricycle_reaction_db.domain.reaction_frames import is_transition_state_frame_eligible

_MAPPING_METHOD = "topology-identity"
_MAPPING_VERSION = "reaction-geometry-link-v1"


@dataclass(frozen=True, slots=True)
class ReactionGeometryReconciliationResult:
    node_geometry_ids: tuple[UUID, ...]


def _endpoint_spec(
    side: LogicalReactionParticipantSide,
) -> tuple[str, MappedReactionNodeRole, int]:
    if side is LogicalReactionParticipantSide.REACTANT:
        return "reactants", MappedReactionNodeRole.REACTANT, 0
    return "products", MappedReactionNodeRole.PRODUCT, 1


def resolve_endpoint_node(
    session: Session,
    mapped_reaction: MappedReaction,
    side: LogicalReactionParticipantSide,
) -> MappedReactionNode:
    """Resolve the conventional endpoint node without excluding additional same-role nodes."""

    node_key, role, preferred_index = _endpoint_spec(side)
    mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
    existing = session.exec(
        select(MappedReactionNode).where(
            MappedReactionNode.mapped_reaction_id == mapped_reaction_id,
            MappedReactionNode.node_key == node_key,
        )
    ).first()
    if existing is not None:
        if existing.role is not role:
            raise ValueError(f"mapped reaction node {node_key!r} has incompatible role")
        return existing

    role_matches = session.exec(
        select(MappedReactionNode).where(
            MappedReactionNode.mapped_reaction_id == mapped_reaction_id,
            MappedReactionNode.role == role,
        )
    ).all()
    if len(role_matches) == 1:
        return role_matches[0]

    used_indices = set(
        session.exec(
            select(MappedReactionNode.node_index).where(
                MappedReactionNode.mapped_reaction_id == mapped_reaction_id
            )
        ).all()
    )
    node_index = preferred_index
    while node_index in used_indices:
        node_index += 1
    return persist_mapped_reaction_node(
        session,
        mapped_reaction,
        MappedReactionNodeRecord(node_key=node_key, node_index=node_index, role=role),
    )


def _endpoint_nodes(
    session: Session,
    mapped_reaction: MappedReaction,
    side: LogicalReactionParticipantSide,
) -> list[MappedReactionNode]:
    primary = resolve_endpoint_node(session, mapped_reaction, side)
    _, role, _ = _endpoint_spec(side)
    nodes = list(
        session.exec(
            select(MappedReactionNode).where(
                MappedReactionNode.mapped_reaction_id == mapped_reaction.id,
                MappedReactionNode.role == role,
            )
        ).all()
    )
    if not nodes:
        return [primary]
    return nodes


def _find_or_create_node_geometry(
    session: Session,
    *,
    node: MappedReactionNode,
    geometry: Geometry,
    component_key: str,
    component_index: int,
    participant: MappedReactionParticipant | None,
    prefer_primary: bool,
) -> MappedReactionNodeGeometry:
    node_id = _require_id(node, label="MappedReactionNode")
    geometry_id = _require_id(geometry, label="Geometry")
    participant_id = (
        _require_id(participant, label="MappedReactionParticipant")
        if participant is not None
        else None
    )
    _acquire_identity_locks(
        session,
        (
            "reaction_geometry_reconciliation",
            node_id,
            participant_id or "unassigned",
            geometry_id,
        ),
    )

    statement = select(MappedReactionNodeGeometry).where(
        MappedReactionNodeGeometry.mapped_reaction_node_id == node_id,
        MappedReactionNodeGeometry.geometry_id == geometry_id,
    )
    if participant_id is None:
        statement = statement.where(
            col(MappedReactionNodeGeometry.mapped_reaction_participant_id).is_(None)
        )
    else:
        statement = statement.where(
            MappedReactionNodeGeometry.mapped_reaction_participant_id == participant_id
        )
    existing = session.exec(statement).first()
    if existing is not None:
        return existing

    if participant_id is not None:
        participant_bindings = session.exec(
            select(MappedReactionNodeGeometry).where(
                MappedReactionNodeGeometry.mapped_reaction_node_id == node_id,
                MappedReactionNodeGeometry.mapped_reaction_participant_id == participant_id,
            )
        ).all()
        component_identities = {
            (binding.component_key, binding.component_index) for binding in participant_bindings
        }
        if len(component_identities) > 1:
            raise ValueError("reaction participant has inconsistent node component identities")
        if component_identities:
            component_key, component_index = next(iter(component_identities))

    _acquire_identity_locks(
        session,
        ("reaction_geometry_coordinate_allocation", node_id, component_key),
    )

    component_bindings = session.exec(
        select(MappedReactionNodeGeometry).where(
            MappedReactionNodeGeometry.mapped_reaction_node_id == node_id,
            MappedReactionNodeGeometry.component_key == component_key,
        )
    ).all()
    coordinate_index = (
        max(
            (binding.coordinate_index for binding in component_bindings),
            default=-1,
        )
        + 1
    )
    is_primary = prefer_primary and not any(binding.is_primary for binding in component_bindings)
    return persist_mapped_reaction_node_geometry(
        session,
        node,
        geometry,
        MappedReactionNodeGeometryRecord(
            component_key=component_key,
            component_index=component_index,
            coordinate_index=coordinate_index,
            is_primary=is_primary,
        ),
        mapped_reaction_participant=participant,
    )


def _ensure_mapping(
    session: Session,
    *,
    node_geometry: MappedReactionNodeGeometry,
    topology_atom_maps: list[int],
    mapped_smiles: str,
) -> MappedReactionNodeGeometryMapping:
    node_geometry_id = _require_id(node_geometry, label="MappedReactionNodeGeometry")
    existing = session.exec(
        select(MappedReactionNodeGeometryMapping).where(
            MappedReactionNodeGeometryMapping.mapped_reaction_node_geometry_id == node_geometry_id
        )
    ).first()
    if existing is not None:
        if not _reaction_mapping_isomorphic(
            expected_atom_map_numbers=existing.geometry_atom_map_numbers,
            expected_mapped_smiles=existing.mapped_smiles,
            observed_atom_map_numbers=topology_atom_maps,
            observed_mapped_smiles=mapped_smiles,
        ):
            raise ValueError("existing node Geometry has an incompatible reaction mapping")
        # Source atom order belongs to each CalculationFrame.  A Geometry-level
        # reaction mapping is reusable when its Geometry-order map is equivalent,
        # even if another software/frame reports a different source permutation.
        return existing
    return persist_mapped_reaction_node_geometry_mapping(
        session,
        node_geometry,
        MappedReactionNodeGeometryMappingRecord(
            geometry_atom_map_numbers=topology_atom_maps,
            mapped_smiles=mapped_smiles,
            mapping_method=_MAPPING_METHOD,
            mapping_version=_MAPPING_VERSION,
            verified=True,
        ),
    )


def _bind_participant_geometry(
    session: Session,
    *,
    participant: MappedReactionParticipant,
    geometry: Geometry,
) -> list[MappedReactionNodeGeometry]:
    mapped_reaction = session.get(MappedReaction, participant.mapped_reaction_id)
    if mapped_reaction is None:
        raise RuntimeError("MappedReactionParticipant references a missing MappedReaction")
    topology_atom_maps = list(participant.atom_map_numbers)
    component_key = f"{participant.side.value}:{participant.template_index}"
    node_geometries: list[MappedReactionNodeGeometry] = []
    for node in _endpoint_nodes(session, mapped_reaction, participant.side):
        node_geometry = _find_or_create_node_geometry(
            session,
            node=node,
            geometry=geometry,
            component_key=component_key,
            component_index=participant.template_index,
            participant=participant,
            prefer_primary=False,
        )
        _ensure_mapping(
            session,
            node_geometry=node_geometry,
            topology_atom_maps=topology_atom_maps,
            mapped_smiles=participant.mapped_smiles,
        )
        node_geometries.append(node_geometry)
    return node_geometries


def _geometry_has_converged_optimization_frame(
    session: Session,
    geometry: Geometry,
) -> bool:
    """Return whether Geometry has evidence from at least one converged optimization."""

    geometry_id = _require_id(geometry, label="Geometry")
    return (
        session.exec(
            select(CalculationFrame.id)
            .where(
                CalculationFrame.geometry_id == geometry_id,
                CalculationFrame.optimization_status == OptimizationStatus.CONVERGED,
            )
            .limit(1)
        ).first()
        is not None
    )


def _reaction_geometry_predicate() -> Any:
    """SQL predicate for converged geometries carrying thermodynamic evidence."""

    return and_(
        col(Geometry.id).in_(
            select(CalculationFrame.geometry_id).where(
                CalculationFrame.optimization_status == OptimizationStatus.CONVERGED
            )
        ),
        geometry_has_thermodynamic_property_predicate(col(Geometry.id)),
    )


def reconcile_geometry_with_reactions(
    session: Session,
    geometry: Geometry,
) -> ReactionGeometryReconciliationResult:
    """Bind a converged Geometry to every matching reaction endpoint."""

    if not _geometry_has_converged_optimization_frame(
        session, geometry
    ) or not geometry_has_thermodynamic_property(session, geometry):
        return ReactionGeometryReconciliationResult(node_geometry_ids=())

    participants = session.exec(
        select(MappedReactionParticipant)
        .join(LogicalReactionParticipant)
        .where(LogicalReactionParticipant.topology_id == geometry.topology_id)
    ).all()
    node_geometries: list[MappedReactionNodeGeometry] = []
    affected_reactions: dict[UUID, MappedReaction] = {}
    for participant in participants:
        bindings = _bind_participant_geometry(
            session,
            participant=participant,
            geometry=geometry,
        )
        node_geometries.extend(bindings)
        mapped_reaction = session.get(MappedReaction, participant.mapped_reaction_id)
        if mapped_reaction is not None:
            affected_reactions[participant.mapped_reaction_id] = mapped_reaction
    for mapped_reaction in affected_reactions.values():
        refresh_mapped_reaction_thermodynamics(session, mapped_reaction)
    result = ReactionGeometryReconciliationResult(
        node_geometry_ids=tuple(
            _require_id(binding, label="MappedReactionNodeGeometry") for binding in node_geometries
        ),
    )
    return result


def reconcile_mapped_reaction_with_geometries(
    session: Session,
    mapped_reaction: MappedReaction,
) -> ReactionGeometryReconciliationResult:
    """Backfill participant Geometries backed by converged optimizations."""

    mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
    participants = session.exec(
        select(MappedReactionParticipant).where(
            MappedReactionParticipant.mapped_reaction_id == mapped_reaction_id
        )
    ).all()
    node_geometries: list[MappedReactionNodeGeometry] = []
    for participant in participants:
        topology_id = participant.logical_reaction_participant.topology_id
        geometries = session.exec(
            select(Geometry).where(
                Geometry.topology_id == topology_id,
                _reaction_geometry_predicate(),
            )
        ).all()
        for geometry in geometries:
            bindings = _bind_participant_geometry(
                session,
                participant=participant,
                geometry=geometry,
            )
            node_geometries.extend(bindings)
    refresh_mapped_reaction_thermodynamics(session, mapped_reaction)
    return ReactionGeometryReconciliationResult(
        node_geometry_ids=tuple(
            _require_id(binding, label="MappedReactionNodeGeometry") for binding in node_geometries
        ),
    )


def _resolve_transition_state_node(
    session: Session,
    mapped_reaction: MappedReaction,
) -> MappedReactionNode:
    mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
    existing = session.exec(
        select(MappedReactionNode).where(
            MappedReactionNode.mapped_reaction_id == mapped_reaction_id,
            MappedReactionNode.node_key == "transition-state",
        )
    ).first()
    if existing is not None:
        if existing.role is not MappedReactionNodeRole.TRANSITION_STATE:
            raise ValueError("transition-state node key has an incompatible role")
        return existing
    role_matches = session.exec(
        select(MappedReactionNode).where(
            MappedReactionNode.mapped_reaction_id == mapped_reaction_id,
            MappedReactionNode.role == MappedReactionNodeRole.TRANSITION_STATE,
        )
    ).all()
    if len(role_matches) == 1:
        return role_matches[0]
    used_indices = set(
        session.exec(
            select(MappedReactionNode.node_index).where(
                MappedReactionNode.mapped_reaction_id == mapped_reaction_id
            )
        ).all()
    )
    node_index = 2
    while node_index in used_indices:
        node_index += 1
    return persist_mapped_reaction_node(
        session,
        mapped_reaction,
        MappedReactionNodeRecord(
            node_key="transition-state",
            node_index=node_index,
            role=MappedReactionNodeRole.TRANSITION_STATE,
        ),
    )


def _ensure_elementary_edge(
    session: Session,
    *,
    mapped_reaction: MappedReaction,
    reactant_node: MappedReactionNode,
    product_node: MappedReactionNode,
    transition_state_node: MappedReactionNode,
) -> MappedReactionEdge:
    mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
    reactant_node_id = _require_id(reactant_node, label="reactant MappedReactionNode")
    product_node_id = _require_id(product_node, label="product MappedReactionNode")
    transition_state_node_id = _require_id(
        transition_state_node,
        label="transition-state MappedReactionNode",
    )
    _acquire_identity_locks(session, ("automatic_elementary_edge", mapped_reaction_id))
    matching = session.exec(
        select(MappedReactionEdge).where(
            MappedReactionEdge.mapped_reaction_id == mapped_reaction_id,
            MappedReactionEdge.source_node_id == reactant_node_id,
            MappedReactionEdge.target_node_id == product_node_id,
            MappedReactionEdge.transition_state_node_id == transition_state_node_id,
        )
    ).first()
    if matching is not None:
        return matching

    existing_keys = set(
        session.exec(
            select(MappedReactionEdge.edge_key).where(
                MappedReactionEdge.mapped_reaction_id == mapped_reaction_id
            )
        ).all()
    )
    edge_key = "automatic-elementary-step"
    suffix = 2
    while edge_key in existing_keys:
        edge_key = f"automatic-elementary-step-{suffix}"
        suffix += 1
    return persist_mapped_reaction_edge(
        session,
        mapped_reaction,
        reactant_node,
        product_node,
        MappedReactionEdgeRecord(
            edge_key=edge_key,
            edge_kind=MappedReactionEdgeKind.ELEMENTARY_STEP,
        ),
        transition_state_node=transition_state_node,
    )


def ensure_transition_state_path(
    session: Session,
    *,
    mapped_reaction: MappedReaction,
) -> MappedReactionNode:
    """Ensure an elementary TS path without requiring a calculation Geometry."""

    reactant_node = resolve_endpoint_node(
        session,
        mapped_reaction,
        LogicalReactionParticipantSide.REACTANT,
    )
    product_node = resolve_endpoint_node(
        session,
        mapped_reaction,
        LogicalReactionParticipantSide.PRODUCT,
    )
    transition_state_node = _resolve_transition_state_node(session, mapped_reaction)
    _ensure_elementary_edge(
        session,
        mapped_reaction=mapped_reaction,
        reactant_node=reactant_node,
        product_node=product_node,
        transition_state_node=transition_state_node,
    )
    return transition_state_node


def bind_transition_state_frame(
    session: Session,
    *,
    mapped_reaction: MappedReaction,
    calculation_frame: CalculationFrame,
) -> MappedReactionNodeGeometry:
    """Bind one TS conformer; distinct Geometry identities remain distinct candidates."""

    if not is_transition_state_frame_eligible(calculation_frame.frame_role):
        raise ValueError("TS calculations require a single-point or terminal frame")

    transition_state_node = ensure_transition_state_path(
        session,
        mapped_reaction=mapped_reaction,
    )
    geometry = calculation_frame.geometry
    frame_source_atom_maps = list(range(1, geometry.atom_count + 1))
    source_to_geometry_atom_indices = list(calculation_frame.observed_to_geometry_atom_indices)
    topology_atom_maps = atom_maps_from_source_order(
        geometry,
        frame_source_atom_maps,
        source_to_geometry_atom_indices,
    )
    node_geometry = _find_or_create_node_geometry(
        session,
        node=transition_state_node,
        geometry=geometry,
        component_key="transition-state",
        component_index=0,
        participant=None,
        prefer_primary=True,
    )
    _ensure_mapping(
        session,
        node_geometry=node_geometry,
        topology_atom_maps=topology_atom_maps,
        mapped_smiles=mapped_smiles_for_topology(geometry.topology, topology_atom_maps),
    )
    refresh_mapped_reaction_thermodynamics(session, mapped_reaction)
    return node_geometry


__all__ = [
    "ReactionGeometryReconciliationResult",
    "bind_transition_state_frame",
    "ensure_transition_state_path",
    "reconcile_geometry_with_reactions",
    "reconcile_mapped_reaction_with_geometries",
    "resolve_endpoint_node",
]
