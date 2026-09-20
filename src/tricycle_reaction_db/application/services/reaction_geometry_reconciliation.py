"""Idempotent links between reusable geometries and mapped reaction nodes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import aliased, selectinload
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.dtos import (
    MappedReactionEdgeRecord,
    MappedReactionNodeGeometryMappingRecord,
    MappedReactionNodeGeometryRecord,
    MappedReactionNodeRecord,
)
from tricycle_reaction_db.application.services._persistence import (
    LEGACY_BULK_IMPORT_SESSION_INFO_KEY,
    _acquire_identity_locks,
    _require_id,
)
from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence import (
    mark_mapped_reactions_thermodynamics_dirty,
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
from tricycle_reaction_db.application.services.topology_compatibility import (
    source_geometry_compatible_topology,
)
from tricycle_reaction_db.core.chemistry_config import (
    REACTION_GEOMETRY_LINK_METHOD,
    REACTION_GEOMETRY_LINK_POLICY_VERSION,
    REACTION_TS_GEOMETRY_LINK_METHOD,
    REACTION_TS_GEOMETRY_LINK_POLICY_VERSION,
)
from tricycle_reaction_db.db.models import (
    CalculationFrame,
    Geometry,
    LogicalParticipantConcreteTopology,
    LogicalReaction,
    LogicalReactionParticipant,
    MappedReaction,
    MappedReactionEdge,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionNodeGeometryMapping,
    MappedReactionParticipant,
    MolecularTopology,
)
from tricycle_reaction_db.domain.enums import (
    LogicalReactionParticipantSide,
    MappedReactionEdgeKind,
    MappedReactionNodeRole,
    OptimizationStatus,
)
from tricycle_reaction_db.domain.reaction_frames import is_transition_state_frame_eligible


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
    # ``loaded_node_geometries`` may also describe a deliberately partial
    # cache restored around a savepoint retry.  Only this set authorizes
    # treating a cache miss as an authoritative negative lookup.
    complete_node_geometries: set[UUID] = field(default_factory=set)
    mappings_by_node_geometry_id: dict[UUID, MappedReactionNodeGeometryMapping] = field(
        default_factory=dict
    )
    loaded_mappings: set[UUID] = field(default_factory=set)
    # A TS inference may bind several frames to the same mapped reaction. Once
    # its path and thermodynamic profile have been checked in this batch, avoid
    # repeating the same SELECT/refresh work for the next frame.
    transition_state_paths_ready: set[UUID] = field(default_factory=set)
    thermodynamics_refreshed_reactions: set[UUID] = field(default_factory=set)
    new_node_geometry_ids: set[UUID] = field(default_factory=set)
    thermodynamic_property_geometry_ids: set[UUID] = field(default_factory=set)
    affected_reactions_by_id: dict[UUID, MappedReaction] = field(default_factory=dict)
    # These fallback lookups depend only on the source topology during one
    # reconciliation phase.  The preload barrier marks the topology set as
    # complete after all deferred reaction rows have been flushed.
    logical_member_reactions_by_topology: dict[UUID, tuple[MappedReaction, ...]] = field(
        default_factory=dict
    )
    endpoint_compatible_reactions_by_topology: dict[UUID, tuple[MappedReaction, ...]] = field(
        default_factory=dict
    )
    reaction_lookup_topologies_loaded: set[UUID] = field(default_factory=set)
    # A mapped reaction inserted in this transaction cannot have pre-existing
    # path rows.  This lets node/edge creation skip the idempotency read path
    # while retaining the lock-and-recheck path for durable identities loaded
    # from PostgreSQL.
    new_mapped_reaction_ids: set[UUID] = field(default_factory=set)


def _require_project_owner(entity: Any, *, label: str) -> UUID:
    project_id = getattr(entity, "project_id", None)
    if not isinstance(project_id, UUID):
        raise ValueError(f"{label} must have a project_id before reaction reconciliation")
    return project_id


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
        role_matches = list(
            session.exec(
                select(MappedReactionNode).where(
                    MappedReactionNode.mapped_reaction_id == mapped_reaction_id,
                    MappedReactionNode.role == role,
                )
            ).all()
        )
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
        assume_absent=(cache is not None and mapped_reaction_id in cache.new_mapped_reaction_ids),
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


def _cache_node_geometry_binding(
    cache: ReconciliationBatchCache,
    node_id: UUID,
    binding: MappedReactionNodeGeometry,
) -> None:
    """Add a binding to the positive cache without duplicating its identity."""

    bindings = cache.node_geometries_by_node.setdefault(node_id, [])
    if any(
        existing.geometry_id == binding.geometry_id
        and existing.mapped_reaction_participant_id == binding.mapped_reaction_participant_id
        for existing in bindings
    ):
        return
    bindings.append(binding)


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
    bindings: list[MappedReactionNodeGeometry] | None = None
    cache_node_geometries_complete = False
    node_belongs_to_new_reaction = False
    existing = None
    if cache is not None:
        bindings = cache.node_geometries_by_node.setdefault(node_id, [])
        node_belongs_to_new_reaction = node.mapped_reaction_id in cache.new_mapped_reaction_ids
        cache_node_geometries_complete = (
            node_id in cache.complete_node_geometries or node_belongs_to_new_reaction
        )
        existing = next(
            (
                binding
                for binding in bindings
                if binding.geometry_id == geometry_id
                and binding.mapped_reaction_participant_id == participant_id
            ),
            None,
        )

    # A cache miss is authoritative only after preload_reconciliation_context
    # has loaded the complete collection for this node.  A partial cache can
    # still be restored around a savepoint retry, so it must consult
    # PostgreSQL before allocating a new coordinate.
    if existing is None and not cache_node_geometries_complete:
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
        if existing is not None and cache is not None:
            _cache_node_geometry_binding(cache, node_id, existing)
    if existing is not None:
        return existing

    # A complete batch cache is authoritative for positive hits. Avoid an
    # advisory-lock round trip for every repeated conformer; only a miss can
    # allocate a new identity and therefore needs the lock before the database
    # recheck/coordinate allocation below.
    if not node_belongs_to_new_reaction:
        _acquire_identity_locks(
            session,
            (
                "reaction_geometry_reconciliation",
                node_id,
                participant_id or "unassigned",
                geometry_id,
            ),
        )

    if cache is not None and bindings is not None and not cache_node_geometries_complete:
        # Keep unflushed fast-path bindings from the cache, and merge the
        # authoritative rows that are already visible in PostgreSQL.  This is
        # needed for coordinate allocation as well as identity lookup: the
        # cache may contain a newly created row that the database cannot see
        # until the batch flush.
        for persisted_binding in session.exec(
            select(MappedReactionNodeGeometry).where(
                MappedReactionNodeGeometry.mapped_reaction_node_id == node_id
            )
        ).all():
            _cache_node_geometry_binding(cache, node_id, persisted_binding)
        bindings = cache.node_geometries_by_node[node_id]

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

    if not node_belongs_to_new_reaction:
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
        assume_absent=(
            cache is not None and node.mapped_reaction_id in cache.new_mapped_reaction_ids
        ),
    )
    if cache is not None:
        _cache_node_geometry_binding(cache, node_id, binding)
        # A node loaded from PostgreSQL is complete.  A node first observed
        # through an existing row is not: its cache contains only rows touched
        # by this context, so do not promote that partial list to a complete
        # cache merely because one new row was inserted.
        if cache_node_geometries_complete:
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
    transition_state_mapping = (
        node_geometry.mapped_reaction_node.role is MappedReactionNodeRole.TRANSITION_STATE
    )
    if transition_state_mapping:
        # The Geometry/Topology retains its own stereo evidence. This mapping
        # record binds source atom identity to TS coordinates; TS E/Z must not
        # constrain endpoint mappings and metal-controlled E/Z may not have a
        # lossless SMILES representation.
        mapped_smiles = mapped_smiles_for_topology(
            node_geometry.geometry.topology,
            topology_atom_maps,
            include_stereochemistry=False,
        )
        mapping_method = REACTION_TS_GEOMETRY_LINK_METHOD
        mapping_version = REACTION_TS_GEOMETRY_LINK_POLICY_VERSION
    else:
        mapping_method = REACTION_GEOMETRY_LINK_METHOD
        mapping_version = REACTION_GEOMETRY_LINK_POLICY_VERSION
    existing = (
        cache.mappings_by_node_geometry_id.get(node_geometry_id)
        if cache is not None and node_geometry_id in cache.loaded_mappings
        else None
    )
    if existing is not None:
        existing_mapped_smiles = (
            mapped_smiles_for_topology(
                node_geometry.geometry.topology,
                existing.geometry_atom_map_numbers,
                include_stereochemistry=False,
            )
            if transition_state_mapping
            else existing.mapped_smiles
        )
        if not _reaction_mapping_isomorphic(
            expected_atom_map_numbers=existing.geometry_atom_map_numbers,
            expected_mapped_smiles=existing_mapped_smiles,
            observed_atom_map_numbers=topology_atom_maps,
            observed_mapped_smiles=mapped_smiles,
        ):
            raise ValueError("existing node Geometry has an incompatible reaction mapping")
        if transition_state_mapping and (
            existing.mapped_smiles != existing_mapped_smiles
            or existing.mapping_method != mapping_method
            or existing.mapping_version != mapping_version
        ):
            existing.mapped_smiles = existing_mapped_smiles
            existing.mapping_method = mapping_method
            existing.mapping_version = mapping_version
            session.add(existing)
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
            mapping_method=mapping_method,
            mapping_version=mapping_version,
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


def _cache_reaction_node(
    cache: ReconciliationBatchCache | None,
    mapped_reaction: MappedReaction,
    node: MappedReactionNode,
) -> None:
    """Register a newly resolved path node in the reconciliation cache."""

    if cache is None:
        return
    mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
    node_id = _require_id(node, label="MappedReactionNode")
    current = cache.nodes_by_reaction.setdefault(mapped_reaction_id, ())
    if all(existing.id != node_id for existing in current):
        cache.nodes_by_reaction[mapped_reaction_id] = (*current, node)
    cache.nodes_by_key[(mapped_reaction_id, node.node_key)] = node
    cache.loaded_reaction_nodes.add(mapped_reaction_id)
    cache.node_geometries_by_node.setdefault(node_id, [])
    cache.loaded_node_geometries.add(node_id)


def _target_node_for_source_node(
    session: Session,
    *,
    source_node: MappedReactionNode,
    target_mapped_reaction: MappedReaction,
    cache: ReconciliationBatchCache | None,
) -> MappedReactionNode:
    """Resolve the target path node corresponding to one source path node.

    The normal generated path uses the same ``reactants``, ``products`` and
    ``transition-state`` keys for every mapping.  Keeping the node-key lookup
    here also makes evidence sharing safe for a curated path with more than
    one node of the same role: a source binding is copied to its matching
    target node rather than to every node of that role.
    """

    target_id = _require_id(target_mapped_reaction, label="MappedReaction")
    if cache is not None and target_id in cache.loaded_reaction_nodes:
        existing = cache.nodes_by_key.get((target_id, source_node.node_key))
    else:
        existing = session.exec(
            select(MappedReactionNode).where(
                MappedReactionNode.mapped_reaction_id == target_id,
                MappedReactionNode.node_key == source_node.node_key,
            )
        ).first()
    if existing is not None:
        if existing.role is not source_node.role:
            raise ValueError("shared evidence path node has an incompatible role")
        _cache_reaction_node(cache, target_mapped_reaction, existing)
        return existing

    if source_node.role is MappedReactionNodeRole.TRANSITION_STATE:
        if source_node.node_key == "transition-state":
            return ensure_transition_state_path(
                session,
                mapped_reaction=target_mapped_reaction,
                cache=cache,
            )
        role_matches = session.exec(
            select(MappedReactionNode).where(
                MappedReactionNode.mapped_reaction_id == target_id,
                MappedReactionNode.role == MappedReactionNodeRole.TRANSITION_STATE,
            )
        ).all()
    else:
        _, role, _ = _endpoint_spec(
            LogicalReactionParticipantSide.REACTANT
            if source_node.role is MappedReactionNodeRole.REACTANT
            else LogicalReactionParticipantSide.PRODUCT
        )
        role_matches = _endpoint_nodes(
            session,
            target_mapped_reaction,
            LogicalReactionParticipantSide.REACTANT
            if role is MappedReactionNodeRole.REACTANT
            else LogicalReactionParticipantSide.PRODUCT,
            cache=cache,
        )

    if len(role_matches) == 1:
        target_node = role_matches[0]
        _cache_reaction_node(cache, target_mapped_reaction, target_node)
        return target_node

    target_node = persist_mapped_reaction_node(
        session,
        target_mapped_reaction,
        MappedReactionNodeRecord(
            node_key=source_node.node_key,
            node_index=source_node.node_index,
            role=source_node.role,
        ),
        assume_absent=(cache is not None and target_id in cache.new_mapped_reaction_ids),
    )
    _cache_reaction_node(cache, target_mapped_reaction, target_node)
    return target_node


def _concrete_participant_topology_id(
    session: Session,
    participant: MappedReactionParticipant,
) -> UUID:
    """Resolve a participant's strict topology, including legacy rows."""

    if participant.concrete_topology_id is not None:
        return participant.concrete_topology_id
    logical_participant = session.get(
        LogicalReactionParticipant,
        participant.logical_reaction_participant_id,
    )
    if logical_participant is None:  # pragma: no cover - protected by the FK
        raise RuntimeError("MappedReactionParticipant references a missing logical participant")
    return logical_participant.topology_id


def share_mapped_reaction_evidence(
    session: Session,
    *,
    source_mapped_reaction: MappedReaction,
    target_mapped_reaction: MappedReaction,
    cache: ReconciliationBatchCache | None = None,
    refresh_thermodynamics: bool = False,
) -> tuple[UUID, ...]:
    """Copy reusable source evidence to a concrete mapping instance.

    A concrete mapping changes only the participant topology selected during
    transfer.  Its transition-state geometry and every endpoint geometry whose
    concrete topology is unchanged are therefore facts about the same physical
    calculation and must be reused, not rediscovered from the target topology.
    Geometry rows are project-local derived data; only the physical raw
    ArtifactFile object may be reused across projects. The mapping-specific
    node and node-geometry bindings are created inside the target project.
    """

    source_id = _require_id(source_mapped_reaction, label="source MappedReaction")
    target_id = _require_id(target_mapped_reaction, label="target MappedReaction")
    if source_id == target_id:
        return ()
    source_project_id = _require_project_owner(
        source_mapped_reaction,
        label="source MappedReaction",
    )
    target_project_id = _require_project_owner(
        target_mapped_reaction,
        label="target MappedReaction",
    )
    if source_project_id != target_project_id:
        raise ValueError("reaction evidence cannot cross project boundaries")

    target_participants = {
        (participant.side, participant.template_index): participant
        for participant in session.exec(
            select(MappedReactionParticipant)
            .join(
                MappedReaction,
                col(MappedReactionParticipant.mapped_reaction_id) == col(MappedReaction.id),
            )
            .where(
                col(MappedReactionParticipant.mapped_reaction_id) == target_id,
                col(MappedReaction.project_id) == target_project_id,
            )
        ).all()
    }
    source_rows = session.exec(
        select(MappedReactionNode, MappedReactionNodeGeometry, Geometry)
        .join(
            MappedReaction,
            col(MappedReactionNode.mapped_reaction_id) == col(MappedReaction.id),
        )
        .join(
            MappedReactionNodeGeometry,
            col(MappedReactionNodeGeometry.mapped_reaction_node_id) == col(MappedReactionNode.id),
        )
        .join(Geometry, col(MappedReactionNodeGeometry.geometry_id) == col(Geometry.id))
        .where(
            col(MappedReactionNode.mapped_reaction_id) == source_id,
            col(MappedReaction.project_id) == source_project_id,
            col(Geometry.project_id) == source_project_id,
        )
        .order_by(
            col(MappedReactionNode.node_index),
            col(MappedReactionNodeGeometry.component_key),
            col(MappedReactionNodeGeometry.coordinate_index),
        )
    ).all()
    source_transition_state_exists = bool(
        session.exec(
            select(MappedReactionNode.id)
            .join(
                MappedReaction,
                col(MappedReactionNode.mapped_reaction_id) == col(MappedReaction.id),
            )
            .where(
                MappedReactionNode.mapped_reaction_id == source_id,
                MappedReactionNode.role == MappedReactionNodeRole.TRANSITION_STATE,
                MappedReaction.project_id == source_project_id,
            )
        ).first()
    )
    if source_transition_state_exists:
        ensure_transition_state_path(
            session,
            mapped_reaction=target_mapped_reaction,
            cache=cache,
        )

    copied_geometry_ids: set[UUID] = set()
    copied_endpoint_bindings: set[tuple[UUID, UUID, UUID]] = set()
    for source_node, source_binding, geometry in source_rows:
        source_participant_id = source_binding.mapped_reaction_participant_id
        target_participant: MappedReactionParticipant | None = None
        if source_participant_id is not None:
            source_participant = session.get(MappedReactionParticipant, source_participant_id)
            if source_participant is None:  # pragma: no cover - protected by the FK
                continue
            participant_key = (source_participant.side, source_participant.template_index)
            target_participant = target_participants.get(participant_key)
            if target_participant is None:
                continue
            if _concrete_participant_topology_id(session, source_participant) != (
                _concrete_participant_topology_id(session, target_participant)
            ):
                # This is the selected concrete variant: it must be resolved
                # against its own endpoint geometries, not copied from source.
                continue
            geometry_id = _require_id(geometry, label="Geometry")
            target_participant_topology_id = _concrete_participant_topology_id(
                session,
                target_participant,
            )
            if geometry.topology_id != target_participant_topology_id:
                continue
            target_node = _target_node_for_source_node(
                session,
                source_node=source_node,
                target_mapped_reaction=target_mapped_reaction,
                cache=cache,
            )
            identity = (
                _require_id(target_node, label="MappedReactionNode"),
                geometry_id,
                _require_id(target_participant, label="MappedReactionParticipant"),
            )
            if identity in copied_endpoint_bindings:
                continue
            copied_endpoint_bindings.add(identity)
            node_geometry = _find_or_create_node_geometry(
                session,
                node=target_node,
                geometry=geometry,
                component_key=source_binding.component_key,
                component_index=source_binding.component_index,
                participant=target_participant,
                prefer_primary=source_binding.is_primary,
                cache=cache,
                # The source binding has already passed endpoint eligibility.
                thermodynamic_property_verified=True,
            )
            _ensure_mapping(
                session,
                node_geometry=node_geometry,
                topology_atom_maps=list(target_participant.atom_map_numbers),
                mapped_smiles=target_participant.mapped_smiles,
                cache=cache,
            )
            copied_geometry_ids.add(geometry_id)
            continue

        if source_node.role is not MappedReactionNodeRole.TRANSITION_STATE:
            continue
        target_node = _target_node_for_source_node(
            session,
            source_node=source_node,
            target_mapped_reaction=target_mapped_reaction,
            cache=cache,
        )
        node_geometry = _find_or_create_node_geometry(
            session,
            node=target_node,
            geometry=geometry,
            component_key=source_binding.component_key,
            component_index=source_binding.component_index,
            participant=None,
            prefer_primary=source_binding.is_primary,
            cache=cache,
            # The source TS binding already passed thermodynamic eligibility.
            thermodynamic_property_verified=True,
        )
        source_mapping = session.exec(
            select(MappedReactionNodeGeometryMapping).where(
                MappedReactionNodeGeometryMapping.mapped_reaction_node_geometry_id
                == _require_id(source_binding, label="source MappedReactionNodeGeometry")
            )
        ).first()
        if source_mapping is not None:
            _ensure_mapping(
                session,
                node_geometry=node_geometry,
                topology_atom_maps=list(source_mapping.geometry_atom_map_numbers),
                mapped_smiles=source_mapping.mapped_smiles,
                cache=cache,
            )
        geometry_id = _require_id(geometry, label="Geometry")
        copied_geometry_ids.add(geometry_id)

    if cache is not None:
        cache.affected_reactions_by_id[target_id] = target_mapped_reaction
    elif refresh_thermodynamics:
        refresh_mapped_reaction_thermodynamics(session, target_mapped_reaction)
    return tuple(sorted(copied_geometry_ids, key=str))


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


def _endpoint_compatible_mapped_reactions(
    session: Session,
    geometry: Geometry,
    *,
    project_id: UUID,
) -> tuple[MappedReaction, ...]:
    """Find mappings that can consume a unique endpoint-compatible Geometry.

    This lookup is intentionally refresh-only.  It does not bind a Geometry
    whose strict graph differs from the mapped participant; the thermodynamic
    persistence layer performs the same uniqueness check before selecting it
    as a source-compatible fallback.
    """

    source_topology = session.get(MolecularTopology, geometry.topology_id)
    if source_topology is None:
        return ()
    source_topology_id = _require_id(source_topology, label="MolecularTopology")
    # A source-compatible endpoint is meaningful only inside the persisted
    # stereo-abstraction component of the endpoint topology.  The old query
    # searched every same-formula topology in the project and then paid for a
    # graph match on each row.  Resolve the bounded DAG component first; the
    # following SQL query and the final graph predicate now operate on that
    # small, indexed candidate set only.
    from tricycle_reaction_db.application.services.topology_abstraction import (
        topology_dag_component_ids,
    )

    dag_topology_ids = topology_dag_component_ids(
        session,
        (source_topology_id,),
        project_id=project_id,
    )
    if len(dag_topology_ids) <= 1:
        return ()
    strict_topology = aliased(MolecularTopology)
    rows = session.exec(
        select(MappedReaction, strict_topology)
        .join(
            MappedReactionParticipant,
            col(MappedReactionParticipant.mapped_reaction_id) == col(MappedReaction.id),
        )
        .join(
            LogicalReactionParticipant,
            col(MappedReactionParticipant.logical_reaction_participant_id)
            == col(LogicalReactionParticipant.id),
        )
        .join(
            strict_topology,
            col(strict_topology.id)
            == func.coalesce(
                col(MappedReactionParticipant.concrete_topology_id),
                col(LogicalReactionParticipant.topology_id),
            ),
        )
        .where(
            col(MappedReaction.project_id) == project_id,
            col(strict_topology.project_id) == project_id,
            col(strict_topology.formula_id) == source_topology.formula_id,
            col(strict_topology.atom_count) == source_topology.atom_count,
            col(strict_topology.formal_charge) == source_topology.formal_charge,
            col(strict_topology.fragment_count) == source_topology.fragment_count,
            col(strict_topology.id).in_(dag_topology_ids),
            col(strict_topology.id) != source_topology_id,
        )
    ).all()
    reaction_ids: set[UUID] = set()
    reactions: list[MappedReaction] = []
    for mapped_reaction, candidate_topology in rows:
        mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
        if mapped_reaction_id in reaction_ids:
            continue
        if source_geometry_compatible_topology(candidate_topology.mol, source_topology.mol):
            reaction_ids.add(mapped_reaction_id)
            reactions.append(mapped_reaction)
    return tuple(reactions)


def reconcile_geometry_with_reactions(
    session: Session,
    geometry: Geometry,
    *,
    eligibility: bool | None = None,
    participants_by_topology: dict[UUID, tuple[MappedReactionParticipant, ...]] | None = None,
    mapped_reactions_by_id: dict[UUID, MappedReaction] | None = None,
    cache: ReconciliationBatchCache | None = None,
    refresh_thermodynamics: bool = True,
) -> ReactionGeometryReconciliationResult:
    """Bind a converged Geometry to every matching reaction endpoint.

    A caller that owns a later reconciliation barrier can disable the derived
    profile rebuild.  This is deliberately independent of ``cache``: a
    savepoint retry or a direct caller may not have a batch cache, but must
    still be able to defer the expensive refresh without silently rebuilding a
    profile one reaction at a time.
    """

    project_id = _require_project_owner(geometry, label="Geometry")
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
                    .join(
                        LogicalReactionParticipant,
                        col(MappedReactionParticipant.logical_reaction_participant_id)
                        == col(LogicalReactionParticipant.id),
                    )
                    .join(
                        LogicalReaction,
                        col(LogicalReactionParticipant.logical_reaction_id)
                        == col(LogicalReaction.id),
                    )
                    .join(
                        MappedReaction,
                        col(MappedReactionParticipant.mapped_reaction_id) == col(MappedReaction.id),
                    )
                    .where(
                        col(MappedReaction.project_id) == project_id,
                        col(LogicalReaction.project_id) == project_id,
                        or_(
                            col(MappedReactionParticipant.concrete_topology_id)
                            == geometry.topology_id,
                            and_(
                                col(MappedReactionParticipant.concrete_topology_id).is_(None),
                                col(LogicalReactionParticipant.topology_id) == geometry.topology_id,
                            ),
                        ),
                    )
                ).all()
            )
            participants_by_topology[geometry.topology_id] = participants
    else:
        participants = tuple(
            session.exec(
                select(MappedReactionParticipant)
                .join(
                    LogicalReactionParticipant,
                    col(MappedReactionParticipant.logical_reaction_participant_id)
                    == col(LogicalReactionParticipant.id),
                )
                .join(
                    LogicalReaction,
                    col(LogicalReactionParticipant.logical_reaction_id) == col(LogicalReaction.id),
                )
                .join(
                    MappedReaction,
                    col(MappedReactionParticipant.mapped_reaction_id) == col(MappedReaction.id),
                )
                .where(
                    col(MappedReaction.project_id) == project_id,
                    col(LogicalReaction.project_id) == project_id,
                    or_(
                        col(MappedReactionParticipant.concrete_topology_id) == geometry.topology_id,
                        and_(
                            col(MappedReactionParticipant.concrete_topology_id).is_(None),
                            col(LogicalReactionParticipant.topology_id) == geometry.topology_id,
                        ),
                    ),
                )
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
        if mapped_reaction is None or mapped_reaction.project_id != project_id:
            continue
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

    if not session.info.get(LEGACY_BULK_IMPORT_SESSION_INFO_KEY, False):
        # A Geometry may be the source-compatible concrete member for a mapped
        # participant whose strict topology was reconstructed only from a TS
        # endpoint.  Such a mapping has no exact participant binding by design,
        # so the topology-indexed reverse lookup above cannot mark it dirty.
        # The membership relation is the audited bridge for the thermodynamic
        # fallback loader; refresh every mapped reaction that can consume this
        # member.
        if (
            cache is not None
            and geometry.topology_id in cache.reaction_lookup_topologies_loaded
            and geometry.topology_id in cache.logical_member_reactions_by_topology
        ):
            logical_member_reactions = cache.logical_member_reactions_by_topology.get(
                geometry.topology_id,
                (),
            )
        else:
            logical_member_reactions = tuple(
                session.exec(
                    select(MappedReaction)
                    .join(
                        MappedReactionParticipant,
                        col(MappedReactionParticipant.mapped_reaction_id) == col(MappedReaction.id),
                    )
                    .join(
                        LogicalParticipantConcreteTopology,
                        col(LogicalParticipantConcreteTopology.logical_reaction_participant_id)
                        == col(MappedReactionParticipant.logical_reaction_participant_id),
                    )
                    .where(
                        col(MappedReaction.project_id) == project_id,
                        col(LogicalParticipantConcreteTopology.concrete_topology_id)
                        == geometry.topology_id,
                    )
                    # ``mapped_reaction.reaction`` is a PostgreSQL custom type without
                    # an equality operator, so a full-row DISTINCT cannot be planned.
                    # The joins can produce several rows for one mapping; PostgreSQL
                    # DISTINCT ON the UUID primary key removes only that join
                    # multiplicity without comparing the custom reaction column.
                    .distinct(col(MappedReaction.id))
                ).all()
            )
            if (
                cache is not None
                and geometry.topology_id in cache.reaction_lookup_topologies_loaded
            ):
                cache.logical_member_reactions_by_topology[geometry.topology_id] = (
                    logical_member_reactions
                )
        for mapped_reaction in logical_member_reactions:
            mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
            affected_reactions[mapped_reaction_id] = mapped_reaction

        # An endpoint/source topology mismatch cannot be bound as a node
        # Geometry, but a newly eligible source Geometry must still invalidate
        # the derived thermodynamic profile.  The persistence loader will
        # accept it only if it is the unique eligible endpoint-compatible source
        # for that participant.
        if (
            cache is not None
            and geometry.topology_id in cache.reaction_lookup_topologies_loaded
            and geometry.topology_id in cache.endpoint_compatible_reactions_by_topology
        ):
            endpoint_compatible_reactions = cache.endpoint_compatible_reactions_by_topology.get(
                geometry.topology_id,
                (),
            )
        else:
            endpoint_compatible_reactions = _endpoint_compatible_mapped_reactions(
                session,
                geometry,
                project_id=project_id,
            )
            if (
                cache is not None
                and geometry.topology_id in cache.reaction_lookup_topologies_loaded
            ):
                cache.endpoint_compatible_reactions_by_topology[geometry.topology_id] = (
                    endpoint_compatible_reactions
                )
        for mapped_reaction in endpoint_compatible_reactions:
            mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
            affected_reactions[mapped_reaction_id] = mapped_reaction
    if cache is None and refresh_thermodynamics:
        for mapped_reaction in affected_reactions.values():
            refresh_mapped_reaction_thermodynamics(session, mapped_reaction)
    elif cache is None:
        mark_mapped_reactions_thermodynamics_dirty(
            session,
            tuple(affected_reactions.values()),
        )
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
    *,
    project_id: UUID,
) -> set[UUID]:
    """Ask PostgreSQL which flushed Geometry rows need reaction reconciliation."""

    if not geometry_ids:
        return set()
    rows = session.exec(
        select(Geometry.id).where(
            col(Geometry.id).in_(geometry_ids),
            col(Geometry.project_id) == project_id,
            _reaction_geometry_predicate(),
        )
    ).all()
    return {geometry_id for geometry_id in rows if isinstance(geometry_id, UUID)}


def preload_reconciliation_context(
    session: Session,
    topology_ids: set[UUID],
    *,
    project_id: UUID,
    participants_by_topology: dict[UUID, tuple[MappedReactionParticipant, ...]],
    mapped_reactions_by_id: dict[UUID, MappedReaction],
    cache: ReconciliationBatchCache | None = None,
) -> None:
    """Load all reaction identities and bindings needed by a geometry batch."""

    if not topology_ids:
        return
    if cache is not None:
        # Reaction participants/mappings are populated before this barrier.
        # Invalidate only the topology keys being reloaded so a savepoint retry
        # cannot reuse a lookup from an earlier, incomplete view.
        cache.logical_member_reactions_by_topology = {
            topology_id: reactions
            for topology_id, reactions in cache.logical_member_reactions_by_topology.items()
            if topology_id not in topology_ids
        }
        cache.endpoint_compatible_reactions_by_topology = {
            topology_id: reactions
            for topology_id, reactions in cache.endpoint_compatible_reactions_by_topology.items()
            if topology_id not in topology_ids
        }
        cache.reaction_lookup_topologies_loaded.difference_update(topology_ids)
    participant_rows = session.exec(
        select(
            MappedReactionParticipant,
            MappedReactionParticipant.concrete_topology_id,
            LogicalReactionParticipant.topology_id,
        )
        .options(selectinload(cast(Any, MappedReactionParticipant.logical_reaction_participant)))
        .join(
            LogicalReactionParticipant,
            col(MappedReactionParticipant.logical_reaction_participant_id)
            == col(LogicalReactionParticipant.id),
        )
        .join(
            LogicalReaction,
            col(LogicalReactionParticipant.logical_reaction_id) == col(LogicalReaction.id),
        )
        .join(
            MappedReaction,
            col(MappedReactionParticipant.mapped_reaction_id) == col(MappedReaction.id),
        )
        .where(
            col(MappedReaction.project_id) == project_id,
            col(LogicalReaction.project_id) == project_id,
            or_(
                col(MappedReactionParticipant.concrete_topology_id).in_(topology_ids),
                and_(
                    col(MappedReactionParticipant.concrete_topology_id).is_(None),
                    col(LogicalReactionParticipant.topology_id).in_(topology_ids),
                ),
            ),
        )
    ).all()
    participants_by_topology.update(dict.fromkeys(topology_ids, ()))
    participants_by_topology_lists: dict[UUID, list[MappedReactionParticipant]] = {
        topology_id: [] for topology_id in topology_ids
    }
    reaction_ids: set[UUID] = set()
    for participant, concrete_topology_id, logical_topology_id in participant_rows:
        topology_id = concrete_topology_id or logical_topology_id
        if not isinstance(topology_id, UUID):
            continue
        participants_by_topology_lists.setdefault(topology_id, []).append(participant)
        reaction_ids.add(participant.mapped_reaction_id)
    participants_by_topology.update(
        {
            topology_id: tuple(participants)
            for topology_id, participants in participants_by_topology_lists.items()
        }
    )
    if cache is not None:
        cache.reaction_lookup_topologies_loaded.update(topology_ids)
    if reaction_ids:
        mapped_reactions = session.exec(
            select(MappedReaction).where(
                col(MappedReaction.id).in_(reaction_ids),
                col(MappedReaction.project_id) == project_id,
            )
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
        .options(selectinload(cast(Any, MappedReactionNode.mapped_reaction)))
        .join(
            MappedReaction,
            col(MappedReactionNode.mapped_reaction_id) == col(MappedReaction.id),
        )
        .where(
            col(MappedReactionNode.mapped_reaction_id).in_(reaction_ids),
            col(MappedReaction.project_id) == project_id,
        )
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
    cache.complete_node_geometries.update(node_ids)
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
    project_id = _require_project_owner(mapped_reaction, label="MappedReaction")
    participants = session.exec(
        select(MappedReactionParticipant)
        .join(
            MappedReaction,
            col(MappedReactionParticipant.mapped_reaction_id) == col(MappedReaction.id),
        )
        .where(
            col(MappedReactionParticipant.mapped_reaction_id) == mapped_reaction_id,
            col(MappedReaction.project_id) == project_id,
        )
    ).all()
    node_geometries: list[MappedReactionNodeGeometry] = []
    for participant in participants:
        topology_id = participant.concrete_topology_id
        if topology_id is None:
            # Rows inserted before the concrete-topology split are retained by
            # the migration and are still readable until their next rewrite.
            topology_id = participant.logical_reaction_participant.topology_id
        if topology_id is None:
            continue
        geometries = session.exec(
            select(Geometry).where(
                Geometry.topology_id == topology_id,
                col(Geometry.project_id) == project_id,
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
    elif cache is None:
        mark_mapped_reactions_thermodynamics_dirty(session, (mapped_reaction,))
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
        role_matches = list(
            session.exec(
                select(MappedReactionNode).where(
                    MappedReactionNode.mapped_reaction_id == mapped_reaction_id,
                    MappedReactionNode.role == MappedReactionNodeRole.TRANSITION_STATE,
                )
            ).all()
        )
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
        assume_absent=(cache is not None and mapped_reaction_id in cache.new_mapped_reaction_ids),
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
    cache: ReconciliationBatchCache | None = None,
) -> MappedReactionEdge:
    mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
    reactant_node_id = _require_id(reactant_node, label="reactant MappedReactionNode")
    product_node_id = _require_id(product_node, label="product MappedReactionNode")
    transition_state_node_id = _require_id(
        transition_state_node,
        label="transition-state MappedReactionNode",
    )
    if cache is not None and mapped_reaction_id in cache.new_mapped_reaction_ids:
        return persist_mapped_reaction_edge(
            session,
            mapped_reaction,
            reactant_node,
            product_node,
            MappedReactionEdgeRecord(
                edge_key="automatic-elementary-step",
                edge_kind=MappedReactionEdgeKind.ELEMENTARY_STEP,
            ),
            transition_state_node=transition_state_node,
            assume_absent=True,
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

    mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
    if cache is not None and mapped_reaction_id in cache.transition_state_paths_ready:
        transition_state_node = cache.nodes_by_key.get((mapped_reaction_id, "transition-state"))
        if transition_state_node is not None:
            return transition_state_node

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
        cache=cache,
    )
    if cache is not None:
        cache.transition_state_paths_ready.add(mapped_reaction_id)
    return transition_state_node


def bind_transition_state_frame(
    session: Session,
    *,
    mapped_reaction: MappedReaction,
    calculation_frame: CalculationFrame,
    cache: ReconciliationBatchCache | None = None,
    refresh_thermodynamics: bool = True,
) -> MappedReactionNodeGeometry:
    """Bind one TS conformer; distinct Geometry identities remain distinct candidates."""

    if not is_transition_state_frame_eligible(calculation_frame.frame_role):
        raise ValueError("TS calculations require a single-point or terminal frame")
    project_id = _require_project_owner(mapped_reaction, label="MappedReaction")
    if calculation_frame.geometry.project_id != project_id:
        raise ValueError("TS calculation Geometry crosses the mapped reaction project boundary")

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
        mapped_smiles=mapped_smiles_for_topology(
            geometry.topology,
            topology_atom_maps,
            include_stereochemistry=False,
        ),
        cache=cache,
    )
    mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
    if cache is not None:
        cache.affected_reactions_by_id[mapped_reaction_id] = mapped_reaction
    if not session.info.get(LEGACY_BULK_IMPORT_SESSION_INFO_KEY, False):
        sibling_reactions = session.exec(
            select(MappedReaction).where(
                MappedReaction.logical_reaction_id == mapped_reaction.logical_reaction_id,
                MappedReaction.id != mapped_reaction_id,
                MappedReaction.project_id == project_id,
            )
        ).all()
        for sibling_reaction in sibling_reactions:
            share_mapped_reaction_evidence(
                session,
                source_mapped_reaction=mapped_reaction,
                target_mapped_reaction=sibling_reaction,
                cache=cache,
            )
            if cache is None and refresh_thermodynamics:
                refresh_mapped_reaction_thermodynamics(session, sibling_reaction)
    if refresh_thermodynamics:
        refresh_mapped_reaction_thermodynamics(session, mapped_reaction)
        if cache is not None:
            cache.thermodynamics_refreshed_reactions.add(mapped_reaction_id)
    elif cache is None:
        mark_mapped_reactions_thermodynamics_dirty(session, (mapped_reaction,))
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
    "share_mapped_reaction_evidence",
]
