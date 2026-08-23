"""Idempotent links between reusable geometries and mapped reaction nodes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import and_
from sqlalchemy.orm import selectinload
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
    geometry_has_no_imaginary_frequency,
    geometry_has_no_imaginary_frequency_predicate,
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


@dataclass(slots=True)
class ReconciliationBatchCache:
    """In-memory indexes for one flushed reconciliation batch."""

    nodes_by_reaction: dict[UUID, tuple[MappedReactionNode, ...]] = field(default_factory=dict)
    nodes_by_key: dict[tuple[UUID, str], MappedReactionNode] = field(default_factory=dict)
    loaded_reaction_nodes: set[UUID] = field(default_factory=set)
    node_geometries_by_node: dict[UUID, list[MappedReactionNodeGeometry]] = field(
        default_factory=dict
    )
    loaded_node_geometries: set[UUID] = field(default_factory=set)
    mappings_by_node_geometry_id: dict[UUID, MappedReactionNodeGeometryMapping] = field(
        default_factory=dict
    )
    loaded_mappings: set[UUID] = field(default_factory=set)
    new_node_geometry_ids: set[UUID] = field(default_factory=set)
    thermodynamic_property_geometry_ids: set[UUID] = field(default_factory=set)
    affected_reactions_by_id: dict[UUID, MappedReaction] = field(default_factory=dict)


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
    *,
    cache: ReconciliationBatchCache | None = None,
) -> MappedReactionNode:
    """Resolve the conventional endpoint node without excluding additional same-role nodes."""

    node_key, role, preferred_index = _endpoint_spec(side)
    mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
    if cache is not None and mapped_reaction_id in cache.loaded_reaction_nodes:
        existing = cache.nodes_by_key.get((mapped_reaction_id, node_key))
    else:
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

    if cache is not None and mapped_reaction_id in cache.loaded_reaction_nodes:
        role_matches = [
            node
            for node in cache.nodes_by_reaction.get(mapped_reaction_id, ())
            if node.role is role
        ]
    else:
        role_matches = session.exec(
            select(MappedReactionNode).where(
                MappedReactionNode.mapped_reaction_id == mapped_reaction_id,
                MappedReactionNode.role == role,
            )
        ).all()
    if len(role_matches) == 1:
        return role_matches[0]

    if cache is not None and mapped_reaction_id in cache.loaded_reaction_nodes:
        used_indices = {
            node.node_index for node in cache.nodes_by_reaction.get(mapped_reaction_id, ())
        }
    else:
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
    node = persist_mapped_reaction_node(
        session,
        mapped_reaction,
        MappedReactionNodeRecord(node_key=node_key, node_index=node_index, role=role),
    )
    if cache is not None:
        cache.nodes_by_reaction[mapped_reaction_id] = (
            *cache.nodes_by_reaction.get(mapped_reaction_id, ()),
            node,
        )
        cache.nodes_by_key[(mapped_reaction_id, node_key)] = node
        cache.loaded_reaction_nodes.add(mapped_reaction_id)
        if isinstance(node.id, UUID):
            cache.node_geometries_by_node[node.id] = []
            cache.loaded_node_geometries.add(node.id)
    return node


def _endpoint_nodes(
    session: Session,
    mapped_reaction: MappedReaction,
    side: LogicalReactionParticipantSide,
    *,
    cache: ReconciliationBatchCache | None = None,
) -> list[MappedReactionNode]:
    primary = resolve_endpoint_node(session, mapped_reaction, side, cache=cache)
    _, role, _ = _endpoint_spec(side)
    mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
    if cache is not None and mapped_reaction_id in cache.loaded_reaction_nodes:
        nodes = [
            node
            for node in cache.nodes_by_reaction.get(mapped_reaction_id, ())
            if node.role is role
        ]
    else:
        nodes = list(
            session.exec(
                select(MappedReactionNode).where(
                    MappedReactionNode.mapped_reaction_id == mapped_reaction_id,
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
    cache: ReconciliationBatchCache | None = None,
    thermodynamic_property_verified: bool = False,
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

    if cache is not None and node_id in cache.loaded_node_geometries:
        bindings = cache.node_geometries_by_node.setdefault(node_id, [])
        existing = next(
            (
                binding
                for binding in bindings
                if binding.geometry_id == geometry_id
                and binding.mapped_reaction_participant_id == participant_id
            ),
            None,
        )
    else:
        bindings = None
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
        participant_bindings = (
            [
                binding
                for binding in bindings
                if binding.mapped_reaction_participant_id == participant_id
            ]
            if bindings is not None
            else session.exec(
                select(MappedReactionNodeGeometry).where(
                    MappedReactionNodeGeometry.mapped_reaction_node_id == node_id,
                    MappedReactionNodeGeometry.mapped_reaction_participant_id == participant_id,
                )
            ).all()
        )
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

    component_bindings = (
        [binding for binding in bindings if binding.component_key == component_key]
        if bindings is not None
        else session.exec(
            select(MappedReactionNodeGeometry).where(
                MappedReactionNodeGeometry.mapped_reaction_node_id == node_id,
                MappedReactionNodeGeometry.component_key == component_key,
            )
        ).all()
    )
    coordinate_index = (
        max(
            (binding.coordinate_index for binding in component_bindings),
            default=-1,
        )
        + 1
    )
    is_primary = prefer_primary and not any(binding.is_primary for binding in component_bindings)
    binding = persist_mapped_reaction_node_geometry(
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
        preloaded_bindings=bindings,
        thermodynamic_property_verified=thermodynamic_property_verified,
    )
    if cache is not None:
        cache.node_geometries_by_node.setdefault(node_id, []).append(binding)
        cache.loaded_node_geometries.add(node_id)
        cache.new_node_geometry_ids.add(_require_id(binding, label="MappedReactionNodeGeometry"))
    return binding


def _ensure_mapping(
    session: Session,
    *,
    node_geometry: MappedReactionNodeGeometry,
    topology_atom_maps: list[int],
    mapped_smiles: str,
    cache: ReconciliationBatchCache | None = None,
) -> MappedReactionNodeGeometryMapping:
    node_geometry_id = _require_id(node_geometry, label="MappedReactionNodeGeometry")
    existing = (
        cache.mappings_by_node_geometry_id.get(node_geometry_id)
        if cache is not None and node_geometry_id in cache.loaded_mappings
        else None
    )
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
    mapping = persist_mapped_reaction_node_geometry_mapping(
        session,
        node_geometry,
        MappedReactionNodeGeometryMappingRecord(
            geometry_atom_map_numbers=topology_atom_maps,
            mapped_smiles=mapped_smiles,
            mapping_method=_MAPPING_METHOD,
            mapping_version=_MAPPING_VERSION,
            verified=True,
        ),
        identity_is_new=(cache is not None and node_geometry_id in cache.new_node_geometry_ids),
    )
    if cache is not None:
        cache.loaded_mappings.add(node_geometry_id)
        cache.mappings_by_node_geometry_id[node_geometry_id] = mapping
    return mapping


def _bind_participant_geometry(
    session: Session,
    *,
    participant: MappedReactionParticipant,
    geometry: Geometry,
    mapped_reaction: MappedReaction | None = None,
    cache: ReconciliationBatchCache | None = None,
    thermodynamic_property_verified: bool = False,
) -> list[MappedReactionNodeGeometry]:
    mapped_reaction = mapped_reaction or session.get(MappedReaction, participant.mapped_reaction_id)
    if mapped_reaction is None:
        raise RuntimeError("MappedReactionParticipant references a missing MappedReaction")
    topology_atom_maps = list(participant.atom_map_numbers)
    component_key = f"{participant.side.value}:{participant.template_index}"
    node_geometries: list[MappedReactionNodeGeometry] = []
    for node in _endpoint_nodes(session, mapped_reaction, participant.side, cache=cache):
        node_geometry = _find_or_create_node_geometry(
            session,
            node=node,
            geometry=geometry,
            component_key=component_key,
            component_index=participant.template_index,
            participant=participant,
            prefer_primary=False,
            cache=cache,
            thermodynamic_property_verified=thermodynamic_property_verified,
        )
        _ensure_mapping(
            session,
            node_geometry=node_geometry,
            topology_atom_maps=topology_atom_maps,
            mapped_smiles=participant.mapped_smiles,
            cache=cache,
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
        geometry_has_no_imaginary_frequency_predicate(col(Geometry.id)),
    )


def reconcile_geometry_with_reactions(
    session: Session,
    geometry: Geometry,
    *,
    eligibility: bool | None = None,
    participants_by_topology: dict[UUID, tuple[MappedReactionParticipant, ...]] | None = None,
    mapped_reactions_by_id: dict[UUID, MappedReaction] | None = None,
    cache: ReconciliationBatchCache | None = None,
) -> ReactionGeometryReconciliationResult:
    """Bind a converged Geometry to every matching reaction endpoint."""

    thermodynamic_property_verified = False
    if eligibility is None:
        eligibility = (
            _geometry_has_converged_optimization_frame(session, geometry)
            and geometry_has_thermodynamic_property(session, geometry)
            and geometry_has_no_imaginary_frequency(session, geometry)
        )
        thermodynamic_property_verified = eligibility
    if not eligibility:
        return ReactionGeometryReconciliationResult(node_geometry_ids=())

    if participants_by_topology is not None:
        participants = participants_by_topology.get(geometry.topology_id)
        if participants is None:
            participants = tuple(
                session.exec(
                    select(MappedReactionParticipant)
                    .join(LogicalReactionParticipant)
                    .where(LogicalReactionParticipant.topology_id == geometry.topology_id)
                ).all()
            )
            participants_by_topology[geometry.topology_id] = participants
    else:
        participants = tuple(
            session.exec(
                select(MappedReactionParticipant)
                .join(LogicalReactionParticipant)
                .where(LogicalReactionParticipant.topology_id == geometry.topology_id)
            ).all()
        )
    node_geometries: list[MappedReactionNodeGeometry] = []
    affected_reactions: dict[UUID, MappedReaction] = {}
    for participant in participants:
        mapped_reaction = (
            mapped_reactions_by_id.get(participant.mapped_reaction_id)
            if mapped_reactions_by_id is not None
            else None
        )
        if mapped_reaction is None:
            mapped_reaction = session.get(MappedReaction, participant.mapped_reaction_id)
            if mapped_reactions_by_id is not None and mapped_reaction is not None:
                mapped_reactions_by_id[participant.mapped_reaction_id] = mapped_reaction
        bindings = _bind_participant_geometry(
            session,
            participant=participant,
            geometry=geometry,
            mapped_reaction=mapped_reaction,
            cache=cache,
            thermodynamic_property_verified=(
                thermodynamic_property_verified
                or (
                    cache is not None
                    and _require_id(geometry, label="Geometry")
                    in cache.thermodynamic_property_geometry_ids
                )
            ),
        )
        node_geometries.extend(bindings)
        if mapped_reaction is not None:
            affected_reactions[participant.mapped_reaction_id] = mapped_reaction
    if cache is None:
        for mapped_reaction in affected_reactions.values():
            refresh_mapped_reaction_thermodynamics(session, mapped_reaction)
    else:
        cache.affected_reactions_by_id.update(affected_reactions)
    result = ReactionGeometryReconciliationResult(
        node_geometry_ids=tuple(
            _require_id(binding, label="MappedReactionNodeGeometry") for binding in node_geometries
        ),
    )
    return result


def reconcilable_geometry_ids(
    session: Session,
    geometry_ids: set[UUID],
) -> set[UUID]:
    """Ask PostgreSQL which flushed Geometry rows need reaction reconciliation."""

    if not geometry_ids:
        return set()
    rows = session.exec(
        select(Geometry.id).where(
            col(Geometry.id).in_(geometry_ids),
            _reaction_geometry_predicate(),
        )
    ).all()
    return {geometry_id for geometry_id in rows if isinstance(geometry_id, UUID)}


def preload_reconciliation_context(
    session: Session,
    topology_ids: set[UUID],
    *,
    participants_by_topology: dict[UUID, tuple[MappedReactionParticipant, ...]],
    mapped_reactions_by_id: dict[UUID, MappedReaction],
    cache: ReconciliationBatchCache | None = None,
) -> None:
    """Load all reaction identities and bindings needed by a geometry batch."""

    if not topology_ids:
        return
    participant_rows = session.exec(
        select(MappedReactionParticipant, LogicalReactionParticipant.topology_id)
        .options(selectinload(MappedReactionParticipant.logical_reaction_participant))
        .join(LogicalReactionParticipant)
        .where(col(LogicalReactionParticipant.topology_id).in_(topology_ids))
    ).all()
    for topology_id in topology_ids:
        participants_by_topology[topology_id] = ()
    reaction_ids: set[UUID] = set()
    for participant, topology_id in participant_rows:
        if not isinstance(topology_id, UUID):
            continue
        participants_by_topology[topology_id] = (
            *participants_by_topology[topology_id],
            participant,
        )
        reaction_ids.add(participant.mapped_reaction_id)
    if reaction_ids:
        mapped_reactions = session.exec(
            select(MappedReaction).where(col(MappedReaction.id).in_(reaction_ids))
        ).all()
        mapped_reactions_by_id.update(
            {
                reaction_id: reaction
                for reaction in mapped_reactions
                if isinstance((reaction_id := reaction.id), UUID)
            }
        )
    if cache is None or not reaction_ids:
        return

    nodes = session.exec(
        select(MappedReactionNode)
        .options(selectinload(MappedReactionNode.mapped_reaction))
        .where(col(MappedReactionNode.mapped_reaction_id).in_(reaction_ids))
    ).all()
    nodes_by_reaction: dict[UUID, list[MappedReactionNode]] = {
        reaction_id: [] for reaction_id in reaction_ids
    }
    for node in nodes:
        if isinstance(node.id, UUID) and isinstance(node.mapped_reaction_id, UUID):
            nodes_by_reaction.setdefault(node.mapped_reaction_id, []).append(node)
            cache.nodes_by_key[(node.mapped_reaction_id, node.node_key)] = node
    for reaction_id, reaction_nodes in nodes_by_reaction.items():
        cache.nodes_by_reaction[reaction_id] = tuple(reaction_nodes)
    cache.loaded_reaction_nodes.update(reaction_ids)

    node_ids = {node.id for node in nodes if isinstance(node.id, UUID)}
    for node_id in node_ids:
        cache.node_geometries_by_node[node_id] = []
    if not node_ids:
        return
    node_geometries = session.exec(
        select(MappedReactionNodeGeometry).where(
            col(MappedReactionNodeGeometry.mapped_reaction_node_id).in_(node_ids)
        )
    ).all()
    node_geometry_ids: set[UUID] = set()
    for node_geometry in node_geometries:
        node_id = node_geometry.mapped_reaction_node_id
        if isinstance(node_id, UUID):
            cache.node_geometries_by_node.setdefault(node_id, []).append(node_geometry)
        if isinstance(node_geometry.id, UUID):
            node_geometry_ids.add(node_geometry.id)
    cache.loaded_node_geometries.update(node_ids)
    for node_geometry_id in node_geometry_ids:
        cache.loaded_mappings.add(node_geometry_id)
    if node_geometry_ids:
        mappings = session.exec(
            select(MappedReactionNodeGeometryMapping).where(
                col(MappedReactionNodeGeometryMapping.mapped_reaction_node_geometry_id).in_(
                    node_geometry_ids
                )
            )
        ).all()
        cache.mappings_by_node_geometry_id.update(
            {
                mapping.mapped_reaction_node_geometry_id: mapping
                for mapping in mappings
                if isinstance(mapping.mapped_reaction_node_geometry_id, UUID)
            }
        )


def reconcile_mapped_reaction_with_geometries(
    session: Session,
    mapped_reaction: MappedReaction,
    *,
    refresh_thermodynamics: bool = True,
    cache: ReconciliationBatchCache | None = None,
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
                mapped_reaction=mapped_reaction,
                cache=cache,
            )
            node_geometries.extend(bindings)
    if refresh_thermodynamics:
        refresh_mapped_reaction_thermodynamics(session, mapped_reaction)
    return ReactionGeometryReconciliationResult(
        node_geometry_ids=tuple(
            _require_id(binding, label="MappedReactionNodeGeometry") for binding in node_geometries
        ),
    )


def _resolve_transition_state_node(
    session: Session,
    mapped_reaction: MappedReaction,
    *,
    cache: ReconciliationBatchCache | None = None,
) -> MappedReactionNode:
    mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
    if cache is not None and mapped_reaction_id in cache.loaded_reaction_nodes:
        existing = cache.nodes_by_key.get((mapped_reaction_id, "transition-state"))
    else:
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
    if cache is not None and mapped_reaction_id in cache.loaded_reaction_nodes:
        role_matches = [
            node
            for node in cache.nodes_by_reaction.get(mapped_reaction_id, ())
            if node.role is MappedReactionNodeRole.TRANSITION_STATE
        ]
    else:
        role_matches = session.exec(
            select(MappedReactionNode).where(
                MappedReactionNode.mapped_reaction_id == mapped_reaction_id,
                MappedReactionNode.role == MappedReactionNodeRole.TRANSITION_STATE,
            )
        ).all()
    if len(role_matches) == 1:
        return role_matches[0]
    if cache is not None and mapped_reaction_id in cache.loaded_reaction_nodes:
        used_indices = {
            node.node_index for node in cache.nodes_by_reaction.get(mapped_reaction_id, ())
        }
    else:
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
    node = persist_mapped_reaction_node(
        session,
        mapped_reaction,
        MappedReactionNodeRecord(
            node_key="transition-state",
            node_index=node_index,
            role=MappedReactionNodeRole.TRANSITION_STATE,
        ),
    )
    if cache is not None:
        cache.nodes_by_reaction[mapped_reaction_id] = (
            *cache.nodes_by_reaction.get(mapped_reaction_id, ()),
            node,
        )
        cache.nodes_by_key[(mapped_reaction_id, "transition-state")] = node
        cache.loaded_reaction_nodes.add(mapped_reaction_id)
        if isinstance(node.id, UUID):
            cache.node_geometries_by_node[node.id] = []
            cache.loaded_node_geometries.add(node.id)
    return node


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
    cache: ReconciliationBatchCache | None = None,
) -> MappedReactionNode:
    """Ensure an elementary TS path without requiring a calculation Geometry."""

    reactant_node = resolve_endpoint_node(
        session,
        mapped_reaction,
        LogicalReactionParticipantSide.REACTANT,
        cache=cache,
    )
    product_node = resolve_endpoint_node(
        session,
        mapped_reaction,
        LogicalReactionParticipantSide.PRODUCT,
        cache=cache,
    )
    transition_state_node = _resolve_transition_state_node(
        session,
        mapped_reaction,
        cache=cache,
    )
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
    cache: ReconciliationBatchCache | None = None,
) -> MappedReactionNodeGeometry:
    """Bind one TS conformer; distinct Geometry identities remain distinct candidates."""

    if not is_transition_state_frame_eligible(calculation_frame.frame_role):
        raise ValueError("TS calculations require a single-point or terminal frame")

    transition_state_node = ensure_transition_state_path(
        session,
        mapped_reaction=mapped_reaction,
        cache=cache,
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
        cache=cache,
    )
    _ensure_mapping(
        session,
        node_geometry=node_geometry,
        topology_atom_maps=topology_atom_maps,
        mapped_smiles=mapped_smiles_for_topology(geometry.topology, topology_atom_maps),
        cache=cache,
    )
    refresh_mapped_reaction_thermodynamics(session, mapped_reaction)
    return node_geometry


__all__ = [
    "ReconciliationBatchCache",
    "ReactionGeometryReconciliationResult",
    "bind_transition_state_frame",
    "ensure_transition_state_path",
    "reconcile_geometry_with_reactions",
    "reconcilable_geometry_ids",
    "preload_reconciliation_context",
    "reconcile_mapped_reaction_with_geometries",
    "resolve_endpoint_node",
]
