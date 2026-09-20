"""Persist mapped-reaction thermodynamic profiles after source facts change."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, delete, func, insert, or_
from sqlalchemy import select as sa_select
from sqlalchemy.orm import load_only
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.dtos import MappedReactionThermodynamics
from tricycle_reaction_db.application.services._persistence import (
    _attach_or_reuse_entity,
    _attach_pending_entities,
    _require_id,
    _uuid7,
)
from tricycle_reaction_db.application.services.geometry_energy import (
    GeometryEnergyComposite,
    geometry_energy_composites,
)
from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics import (
    MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION,
    EndpointComponentRequirement,
    GeometryThermodynamicCandidate,
    build_mapped_reaction_thermodynamics,
)
from tricycle_reaction_db.application.services.reaction_geometry_policy import (
    geometry_has_no_imaginary_frequency_predicate,
    geometry_has_thermodynamic_property_predicate,
)
from tricycle_reaction_db.application.services.topology_compatibility import (
    source_geometry_compatible_topology,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    ArtifactIngestion,
    CalculationFrame,
    CalculationProtocol,
    CalculationSegment,
    Geometry,
    LogicalParticipantConcreteTopology,
    LogicalReactionParticipant,
    MappedReaction,
    MappedReactionEdge,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionParticipant,
    MappedReactionThermodynamicProfile,
    MappedReactionThermodynamicProfileRefreshJob,
    MappedReactionThermodynamicProfileSource,
    MolecularTopology,
    ParseRevision,
    ThermochemistryResult,
)
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    MappedReactionNodeRole,
    OptimizationStatus,
    ParseCompleteness,
    ParseStatus,
    StorageStatus,
    ThermodynamicProfileRefreshJobStatus,
    ThermodynamicProfileSourceVisibility,
)


def _thermodynamic_source_ingestion_predicate(
    transition_state_geometry_ids: set[UUID],
) -> Any:
    """Allow complete frame evidence from a partially indexed artifact.

    Some legacy autode artifacts were marked ``partial`` because one parse
    revision in the artifact batch was incomplete, while a selected frame is
    complete and carries usable geometry/thermochemistry.  Keep the decision
    at frame granularity so a partial file does not discard every sibling
    frame.  Endpoint source selection remains restricted to successful
    artifacts; this narrow predicate only admits complete TS frames for
    TS-only profiles.
    """

    if not transition_state_geometry_ids:
        return col(ArtifactIngestion.status) == ArtifactIngestionStatus.SUCCEEDED
    return or_(
        col(ArtifactIngestion.status) == ArtifactIngestionStatus.SUCCEEDED,
        and_(
            col(ArtifactIngestion.status) == ArtifactIngestionStatus.PARTIAL,
            col(CalculationFrame.geometry_id).in_(transition_state_geometry_ids),
            col(CalculationFrame.parse_completeness) == ParseCompleteness.COMPLETE,
        ),
    )


@dataclass(slots=True)
class _MappedReactionThermodynamicsInput:
    """All shared source facts needed to build one reaction profile."""

    mapped_reaction: MappedReaction
    participant_rows: tuple[tuple[MappedReactionParticipant, LogicalReactionParticipant], ...]
    binding_rows: tuple[tuple[MappedReactionNode, MappedReactionNodeGeometry, Geometry], ...]
    endpoint_geometries_by_participant: dict[UUID, tuple[Geometry, ...]]
    transition_state_node_ids: frozenset[UUID]
    composites: dict[UUID, GeometryEnergyComposite]
    runtimes_by_geometry: dict[UUID, dict[UUID, tuple[int, float | None]]]
    eligible_source_frame_ids: frozenset[UUID]


@dataclass(frozen=True, slots=True)
class _ProfileSourceReference:
    """One selected calculation frame and its TS-only partial-ingestion rule."""

    calculation_frame_id: UUID
    allow_partial_ingestion: bool


@dataclass(frozen=True, slots=True)
class _TopologyCompatibilityMetadata:
    """Scalar topology identity used before the expensive RDKit fallback."""

    topology_id: UUID
    formula_id: UUID
    atom_count: int
    formal_charge: int
    fragment_count: int
    stereo_agnostic_graph_hash: str | None


def _graph_hash_proves_endpoint_compatibility(
    endpoint: _TopologyCompatibilityMetadata,
    candidate: _TopologyCompatibilityMetadata,
) -> bool:
    """Use canonical graph identity instead of re-matching an identical DAG graph.

    The stored hash includes atom/isotope identity, charge, bond order,
    connectivity, and hydrogen/radical state; it excludes only atom/bond
    stereochemistry and atom-map labels. Equal hashes therefore prove the
    stricter graph identity required by the source-geometry compatibility
    predicate, whose projection drops at least those stereo distinctions.
    """

    return (
        endpoint.stereo_agnostic_graph_hash is not None
        and endpoint.stereo_agnostic_graph_hash == candidate.stereo_agnostic_graph_hash
    )


def _load_topology_compatibility_metadata(
    session: Session,
    topology_ids: Sequence[UUID],
    *,
    project_id: UUID,
) -> dict[UUID, _TopologyCompatibilityMetadata]:
    """Load only scalar topology identity; never deserialize ``mol`` here."""

    if not topology_ids:
        return {}
    rows = session.exec(
        cast(
            Any,
            sa_select(
                col(MolecularTopology.id),
                col(MolecularTopology.formula_id),
                col(MolecularTopology.atom_count),
                col(MolecularTopology.formal_charge),
                col(MolecularTopology.fragment_count),
                col(MolecularTopology.stereo_agnostic_graph_hash),
            ),
        ).where(
            col(MolecularTopology.project_id) == project_id,
            col(MolecularTopology.id).in_(topology_ids),
        )
    ).all()
    metadata: dict[UUID, _TopologyCompatibilityMetadata] = {}
    for row in rows:
        topology_id = cast(UUID | None, row[0])
        formula_id = cast(UUID | None, row[1])
        if not isinstance(topology_id, UUID) or not isinstance(formula_id, UUID):
            continue
        metadata[topology_id] = _TopologyCompatibilityMetadata(
            topology_id=topology_id,
            formula_id=formula_id,
            atom_count=int(row[2]),
            formal_charge=int(row[3]),
            fragment_count=int(row[4]),
            stereo_agnostic_graph_hash=(str(row[5]) if row[5] is not None else None),
        )
    return metadata


def _load_topology_molecules(
    session: Session,
    topology_ids: Sequence[UUID],
    *,
    project_id: UUID,
) -> dict[UUID, Any]:
    """Deserialize molecules only for the bounded compatibility candidate set."""

    if not topology_ids:
        return {}
    rows = session.exec(
        cast(
            Any,
            sa_select(
                col(MolecularTopology.id),
                col(MolecularTopology.mol),
            ),
        ).where(
            col(MolecularTopology.project_id) == project_id,
            col(MolecularTopology.id).in_(topology_ids),
        )
    ).all()
    return {
        topology_id: molecule
        for topology_id, molecule in rows
        if isinstance(topology_id, UUID) and molecule is not None
    }


def _eligible_endpoint_candidate_geometries_statement(
    *,
    project_id: UUID,
    eligible_geometry_ids: Any,
    candidate_metadata_predicates: Sequence[Any],
) -> Any:
    """Select only eligible Geometry under hash-qualified DAG nodes.

    Keep the topology identity/hash and source-qualification predicates in
    the same query. This avoids materializing every eligible Geometry in a
    large endpoint component before applying the inexpensive topology filters.
    """

    return (
        cast(
            Any,
            sa_select(
                col(MolecularTopology.id),
                col(MolecularTopology.formula_id),
                col(MolecularTopology.atom_count),
                col(MolecularTopology.formal_charge),
                col(MolecularTopology.fragment_count),
                col(MolecularTopology.stereo_agnostic_graph_hash),
                Geometry,
            ),
        )
        .join(
            MolecularTopology,
            col(Geometry.topology_id) == col(MolecularTopology.id),
        )
        .options(load_only(cast(Any, Geometry.id), cast(Any, Geometry.topology_id)))
        .where(
            col(Geometry.project_id) == project_id,
            col(MolecularTopology.project_id) == project_id,
            col(Geometry.id).in_(eligible_geometry_ids),
            geometry_has_thermodynamic_property_predicate(col(Geometry.id)),
            geometry_has_no_imaginary_frequency_predicate(col(Geometry.id)),
            or_(*candidate_metadata_predicates),
        )
    )


def _index_calculation_source_rows(
    rows: Sequence[Any],
) -> tuple[
    list[tuple[CalculationFrame, CalculationProtocol | None, ThermochemistryResult | None]],
    dict[UUID, dict[UUID, tuple[int, float | None]]],
    frozenset[UUID],
]:
    """Split one source query into selection rows, runtimes, and frame ids.

    Profile refresh used to issue a second query over the same frame/revision/
    artifact joins solely to recover file runtimes.  Keeping the small scalar
    provenance projection in the source query removes that duplicate round
    trip while preserving the existing per-file/highest-revision aggregation.
    """

    calculation_rows: list[
        tuple[CalculationFrame, CalculationProtocol | None, ThermochemistryResult | None]
    ] = []
    runtimes_by_geometry: dict[UUID, dict[UUID, tuple[int, float | None]]] = {}
    eligible_source_frame_ids: set[UUID] = set()
    for row in rows:
        frame = cast(CalculationFrame, row[0])
        protocol = cast(CalculationProtocol | None, row[1])
        thermochemistry = cast(ThermochemistryResult | None, row[2])
        artifact_id = cast(UUID | None, row[3])
        revision_number = cast(int, row[4])
        running_time = cast(float | None, row[5])
        calculation_rows.append((frame, protocol, thermochemistry))
        if isinstance(frame.id, UUID):
            eligible_source_frame_ids.add(frame.id)
        geometry_id = frame.geometry_id
        if not isinstance(geometry_id, UUID) or not isinstance(artifact_id, UUID):
            continue
        by_file = runtimes_by_geometry.setdefault(geometry_id, {})
        candidate = (int(revision_number), running_time)
        previous = by_file.get(artifact_id)
        if previous is None or candidate[0] > previous[0]:
            by_file[artifact_id] = candidate
    return calculation_rows, runtimes_by_geometry, frozenset(eligible_source_frame_ids)


def _insert_profile_source_rows(
    session: Session,
    profile_rows: Sequence[MappedReactionThermodynamicProfile],
    source_references_by_profile: Sequence[tuple[_ProfileSourceReference, ...]],
) -> None:
    """Insert normalized profile evidence in one Core executemany operation."""

    created_at = datetime.now(UTC)
    rows = [
        {
            "id": _uuid7(),
            "created_at": created_at,
            "profile_id": _require_id(profile_row, label="MappedReactionThermodynamicProfile"),
            "calculation_frame_id": reference.calculation_frame_id,
            "allow_partial_ingestion": reference.allow_partial_ingestion,
        }
        for profile_row, references in zip(
            profile_rows,
            source_references_by_profile,
            strict=True,
        )
        for reference in references
    ]
    if rows:
        session.execute(insert(MappedReactionThermodynamicProfileSource), rows)


def _runtime_for_geometry_ids(
    geometry_ids: set[UUID],
    runtimes_by_geometry: dict[UUID, dict[UUID, tuple[int, float | None]]],
) -> float | None:
    """Sum distinct source-file runtimes for the selected geometry set.

    A file can contribute multiple frames (and therefore multiple geometries),
    so the artifact id is the deduplication key.  When reparses exist, the
    highest revision represented by the selected frames supplies the runtime.
    """

    files: dict[UUID, tuple[int, float | None]] = {}
    for geometry_id in geometry_ids:
        for artifact_id, candidate in runtimes_by_geometry.get(geometry_id, {}).items():
            previous = files.get(artifact_id)
            if previous is None or candidate[0] > previous[0]:
                files[artifact_id] = candidate
    if not files or any(runtime is None for _, runtime in files.values()):
        return None
    return round(sum(float(runtime) for _, runtime in files.values() if runtime is not None), 6)


def _endpoint_geometries_by_participant(
    session: Session,
    participant_rows: Sequence[tuple[MappedReactionParticipant, LogicalReactionParticipant]],
    *,
    project_id: UUID,
) -> dict[UUID, tuple[Geometry, ...]]:
    """Load eligible endpoint geometries, including audited concrete members.

    A mapped reaction can be created from a displaced TS endpoint before the
    corresponding standalone reactant/ene optimization is reconciled.  The TS
    endpoint's strict topology is then a valid reaction fact but has no
    endpoint ``Geometry`` of its own.  The logical participant membership table
    is the explicit bridge to already imported concrete members; this helper
    uses it only when the strict topology has no eligible Geometry.

    The returned mapping intentionally contains either all eligible geometries
    under the strict topology or, when that set is empty, all eligible
    geometries under the participant's concrete members.  It never crosses a
    logical participant or project boundary.
    """

    if not participant_rows:
        return {}
    participant_ids: list[UUID] = []
    logical_participant_ids: set[UUID] = set()
    strict_topology_ids: dict[UUID, UUID] = {}
    for mapped_participant, logical_participant in participant_rows:
        participant_id = _require_id(mapped_participant, label="MappedReactionParticipant")
        logical_participant_id = _require_id(
            logical_participant,
            label="LogicalReactionParticipant",
        )
        strict_topology_id = (
            mapped_participant.concrete_topology_id or logical_participant.topology_id
        )
        if not isinstance(strict_topology_id, UUID):
            continue
        participant_ids.append(participant_id)
        logical_participant_ids.add(logical_participant_id)
        strict_topology_ids[participant_id] = strict_topology_id

    concrete_topology_ids_by_logical: dict[UUID, set[UUID]] = {
        logical_participant_id: set() for logical_participant_id in logical_participant_ids
    }
    if logical_participant_ids:
        memberships = session.exec(
            select(LogicalParticipantConcreteTopology).where(
                col(LogicalParticipantConcreteTopology.logical_reaction_participant_id).in_(
                    logical_participant_ids
                )
            )
        ).all()
        for membership in memberships:
            logical_participant_id = membership.logical_reaction_participant_id
            concrete_topology_id = membership.concrete_topology_id
            if isinstance(logical_participant_id, UUID) and isinstance(concrete_topology_id, UUID):
                concrete_topology_ids_by_logical.setdefault(logical_participant_id, set()).add(
                    concrete_topology_id
                )

    allowed_topology_ids_by_participant: dict[UUID, set[UUID]] = {}
    logical_id_by_participant = {
        _require_id(mapped_participant, label="MappedReactionParticipant"): _require_id(
            logical_participant, label="LogicalReactionParticipant"
        )
        for mapped_participant, logical_participant in participant_rows
    }
    all_topology_ids: set[UUID] = set()
    for participant_id in participant_ids:
        strict_topology_id = strict_topology_ids[participant_id]
        logical_participant_id = logical_id_by_participant[participant_id]
        allowed = {
            strict_topology_id,
            *concrete_topology_ids_by_logical.get(logical_participant_id, set()),
        }
        allowed_topology_ids_by_participant[participant_id] = allowed
        all_topology_ids.update(allowed)

    if not all_topology_ids:
        return dict.fromkeys(participant_ids, ())
    eligible_geometry_ids = select(col(CalculationFrame.geometry_id)).where(
        col(CalculationFrame.optimization_status) == OptimizationStatus.CONVERGED,
    )
    geometries = session.exec(
        select(Geometry)
        .options(load_only(cast(Any, Geometry.id), cast(Any, Geometry.topology_id)))
        .where(
            col(Geometry.project_id) == project_id,
            col(Geometry.topology_id).in_(all_topology_ids),
            col(Geometry.id).in_(eligible_geometry_ids),
            geometry_has_thermodynamic_property_predicate(col(Geometry.id)),
            geometry_has_no_imaginary_frequency_predicate(col(Geometry.id)),
        )
    ).all()
    geometries_by_topology: dict[UUID, list[Geometry]] = {}
    for geometry in geometries:
        topology_id = geometry.topology_id
        if isinstance(topology_id, UUID):
            geometries_by_topology.setdefault(topology_id, []).append(geometry)

    # A displaced TS endpoint can have a topology that differs from the
    # isolated endpoint geometry by one retained bond/electronic assignment.
    # Preserve that audited compatibility fallback, but never search every
    # topology in the project. The candidate component must remain scoped to
    # its own endpoint root, even when this persistence batch contains many
    # unrelated reactions.
    endpoint_compatible_topology_ids_by_participant: dict[UUID, set[UUID]] = {}
    # Import lazily: topology_abstraction depends on the molecular-geometry
    # persistence layer, whose reconciliation hooks import this module.
    from tricycle_reaction_db.application.services.topology_abstraction import (
        topology_dag_components_by_root,
    )

    strict_metadata = _load_topology_compatibility_metadata(
        session,
        tuple(set(strict_topology_ids.values())),
        project_id=project_id,
    )
    dag_components_by_root = topology_dag_components_by_root(
        session,
        tuple(strict_metadata),
        project_id=project_id,
    )
    endpoint_compatible_search_specs: dict[UUID, _TopologyCompatibilityMetadata] = {}
    for participant_id in participant_ids:
        strict_topology_id = strict_topology_ids[participant_id]
        if geometries_by_topology.get(strict_topology_id):
            continue
        member_ids = allowed_topology_ids_by_participant[participant_id] - {strict_topology_id}
        if any(geometries_by_topology.get(topology_id) for topology_id in member_ids):
            continue
        strict_metadata_row = strict_metadata.get(strict_topology_id)
        if strict_metadata_row is not None:
            endpoint_compatible_search_specs[participant_id] = strict_metadata_row

    if endpoint_compatible_search_specs:
        candidate_scope_by_participant: dict[UUID, set[UUID]] = {}
        for participant_id, strict_topology in endpoint_compatible_search_specs.items():
            component_ids = set(dag_components_by_root.get(strict_topology.topology_id, ()))
            candidate_ids = component_ids - allowed_topology_ids_by_participant[participant_id]
            if candidate_ids:
                candidate_scope_by_participant[participant_id] = candidate_ids

        # Apply the indexed stereo-agnostic hash and cheap identity columns in
        # the same SQL query that checks Geometry eligibility. The scoped ID
        # predicate preserves each endpoint's DAG boundary, while the database
        # avoids materializing unrelated Geometry rows from a large component.
        candidate_metadata_predicates: list[Any] = []
        for participant_id, candidate_ids in candidate_scope_by_participant.items():
            strict_topology = endpoint_compatible_search_specs[participant_id]
            terms: list[Any] = [
                col(MolecularTopology.id).in_(candidate_ids),
                col(MolecularTopology.formula_id) == strict_topology.formula_id,
                col(MolecularTopology.atom_count) == strict_topology.atom_count,
                col(MolecularTopology.formal_charge) == strict_topology.formal_charge,
                col(MolecularTopology.fragment_count) == strict_topology.fragment_count,
            ]
            if strict_topology.stereo_agnostic_graph_hash is not None:
                terms.append(
                    col(MolecularTopology.stereo_agnostic_graph_hash)
                    == strict_topology.stereo_agnostic_graph_hash
                )
            candidate_metadata_predicates.append(and_(*terms))

        # Candidate topologies without a converged, thermochemistry-bearing,
        # non-imaginary Geometry cannot contribute to an endpoint profile. Do
        # that qualification in SQL before loading topology MOL blobs.
        candidate_metadata_by_id: dict[UUID, _TopologyCompatibilityMetadata] = {}
        candidate_geometries_by_topology: dict[UUID, list[Geometry]] = {}
        if candidate_metadata_predicates:
            candidate_rows = session.exec(
                _eligible_endpoint_candidate_geometries_statement(
                    project_id=project_id,
                    eligible_geometry_ids=eligible_geometry_ids,
                    candidate_metadata_predicates=candidate_metadata_predicates,
                )
            ).all()
            for row in candidate_rows:
                candidate_topology_id = cast(UUID | None, row[0])
                formula_id = cast(UUID | None, row[1])
                if not isinstance(candidate_topology_id, UUID) or not isinstance(formula_id, UUID):
                    continue
                candidate_metadata_by_id.setdefault(
                    candidate_topology_id,
                    _TopologyCompatibilityMetadata(
                        topology_id=candidate_topology_id,
                        formula_id=formula_id,
                        atom_count=int(row[2]),
                        formal_charge=int(row[3]),
                        fragment_count=int(row[4]),
                        stereo_agnostic_graph_hash=(str(row[5]) if row[5] is not None else None),
                    ),
                )
                geometry = cast(Geometry, row[6])
                geometry_topology_id = geometry.topology_id
                if isinstance(geometry_topology_id, UUID):
                    candidate_geometries_by_topology.setdefault(
                        geometry_topology_id,
                        [],
                    ).append(geometry)

        eligible_candidate_ids = set(candidate_geometries_by_topology)
        candidate_ids_by_participant: dict[UUID, set[UUID]] = {}
        for participant_id, strict_topology in endpoint_compatible_search_specs.items():
            matching_ids = {
                topology_id
                for topology_id in candidate_scope_by_participant.get(participant_id, set())
                if topology_id in eligible_candidate_ids
                and (candidate := candidate_metadata_by_id.get(topology_id)) is not None
                and candidate.formula_id == strict_topology.formula_id
                and candidate.atom_count == strict_topology.atom_count
                and candidate.formal_charge == strict_topology.formal_charge
                and candidate.fragment_count == strict_topology.fragment_count
                and (
                    strict_topology.stereo_agnostic_graph_hash is None
                    or candidate.stereo_agnostic_graph_hash
                    == strict_topology.stereo_agnostic_graph_hash
                )
            }
            if matching_ids:
                candidate_ids_by_participant[participant_id] = matching_ids

        # No eligible Geometry means no possible profile source, so avoid even
        # loading the endpoint MOL for participants with no surviving matches.
        participants_with_candidates = {
            participant_id
            for participant_id, candidate_ids in candidate_ids_by_participant.items()
            if candidate_ids
        }
        compatible_candidate_ids_by_participant: dict[UUID, set[UUID]] = {}
        candidates_requiring_graph_match: dict[UUID, set[UUID]] = {}
        for participant_id in participants_with_candidates:
            strict_topology = endpoint_compatible_search_specs[participant_id]
            candidate_ids = candidate_ids_by_participant[participant_id]
            hash_proven_ids = {
                topology_id
                for topology_id in candidate_ids
                if _graph_hash_proves_endpoint_compatibility(
                    strict_topology,
                    candidate_metadata_by_id[topology_id],
                )
            }
            if hash_proven_ids:
                compatible_candidate_ids_by_participant[participant_id] = hash_proven_ids
            unresolved_ids = candidate_ids - hash_proven_ids
            if unresolved_ids:
                candidates_requiring_graph_match[participant_id] = unresolved_ids

        # A persisted canonical hash is sufficient to establish graph
        # compatibility after dropping stereo, so only legacy/null-hash rows
        # need to deserialize molecules and invoke the bounded RDKit matcher.
        topology_molecules = (
            _load_topology_molecules(
                session,
                tuple(
                    {
                        endpoint_compatible_search_specs[participant_id].topology_id
                        for participant_id in candidates_requiring_graph_match
                    }
                    | {
                        topology_id
                        for candidate_ids in candidates_requiring_graph_match.values()
                        for topology_id in candidate_ids
                    }
                ),
                project_id=project_id,
            )
            if candidates_requiring_graph_match
            else {}
        )
        compatibility_cache: dict[tuple[UUID, UUID], bool] = {}
        for participant_id, candidate_ids in candidates_requiring_graph_match.items():
            strict_topology = endpoint_compatible_search_specs[participant_id]
            endpoint_molecule = topology_molecules.get(strict_topology.topology_id)
            if endpoint_molecule is None:
                continue
            compatible_ids = compatible_candidate_ids_by_participant.setdefault(
                participant_id,
                set(),
            )
            for candidate_id in candidate_ids:
                cache_key = (strict_topology.topology_id, candidate_id)
                compatible = compatibility_cache.get(cache_key)
                if compatible is None:
                    source_molecule = topology_molecules.get(candidate_id)
                    compatible = bool(
                        source_molecule is not None
                        and source_geometry_compatible_topology(
                            endpoint_molecule,
                            source_molecule,
                        )
                    )
                    compatibility_cache[cache_key] = compatible
                if compatible:
                    compatible_ids.add(candidate_id)

        endpoint_compatible_candidate_ids = {
            topology_id
            for candidate_ids in compatible_candidate_ids_by_participant.values()
            for topology_id in candidate_ids
        }
        for topology_id in endpoint_compatible_candidate_ids:
            geometries_by_topology[topology_id] = candidate_geometries_by_topology[topology_id]
        for participant_id, topology_ids in compatible_candidate_ids_by_participant.items():
            # More than one eligible endpoint-compatible topology is
            # ambiguous and remains unresolved instead of guessing.
            if len(topology_ids) == 1:
                endpoint_compatible_topology_ids_by_participant[participant_id] = topology_ids

    result: dict[UUID, tuple[Geometry, ...]] = {}
    for participant_id in participant_ids:
        strict_topology_id = strict_topology_ids[participant_id]
        strict_geometries = geometries_by_topology.get(strict_topology_id, [])
        selected = strict_geometries or [
            geometry
            for topology_id in sorted(allowed_topology_ids_by_participant[participant_id], key=str)
            if topology_id != strict_topology_id
            for geometry in geometries_by_topology.get(topology_id, [])
        ]
        if not selected:
            selected = [
                geometry
                for topology_id in sorted(
                    endpoint_compatible_topology_ids_by_participant.get(participant_id, set()),
                    key=str,
                )
                for geometry in geometries_by_topology.get(topology_id, [])
            ]
        result[participant_id] = tuple(
            sorted(
                {
                    geometry.id: geometry for geometry in selected if geometry.id is not None
                }.values(),
                key=lambda geometry: str(geometry.id),
            )
        )
    return result


def _load_mapped_reaction_thermodynamics_input(
    session: Session,
    mapped_reaction: MappedReaction,
    *,
    source_frame_ids_by_geometry: Mapping[UUID, tuple[UUID | None, UUID | None]] | None = None,
) -> _MappedReactionThermodynamicsInput:
    """Load the source facts required to refresh one mapped reaction."""

    mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
    participant_rows = session.exec(
        select(MappedReactionParticipant, LogicalReactionParticipant)
        .join(
            LogicalReactionParticipant,
            col(MappedReactionParticipant.logical_reaction_participant_id)
            == col(LogicalReactionParticipant.id),
        )
        .where(MappedReactionParticipant.mapped_reaction_id == mapped_reaction_id)
        .order_by(
            col(MappedReactionParticipant.side),
            col(MappedReactionParticipant.template_index),
        )
    ).all()
    binding_rows = session.exec(
        select(MappedReactionNode, MappedReactionNodeGeometry, Geometry)
        .options(load_only(cast(Any, Geometry.id), cast(Any, Geometry.topology_id)))
        .join(
            MappedReactionNodeGeometry,
            col(MappedReactionNodeGeometry.mapped_reaction_node_id) == col(MappedReactionNode.id),
        )
        .join(Geometry, col(MappedReactionNodeGeometry.geometry_id) == col(Geometry.id))
        .where(MappedReactionNode.mapped_reaction_id == mapped_reaction_id)
    ).all()
    transition_state_geometry_ids = {
        _require_id(geometry, label="Geometry")
        for node, _binding, geometry in binding_rows
        if node.role is MappedReactionNodeRole.TRANSITION_STATE
    }
    transition_state_node_ids = frozenset(
        node_id
        for node_id in session.exec(
            select(col(MappedReactionEdge.transition_state_node_id)).where(
                MappedReactionEdge.mapped_reaction_id == mapped_reaction_id,
                col(MappedReactionEdge.transition_state_node_id).is_not(None),
            )
        ).all()
        if isinstance(node_id, UUID)
    )
    project_id = mapped_reaction.project_id
    if not isinstance(project_id, UUID):
        raise ValueError("MappedReaction must have a project_id before thermodynamic refresh")
    endpoint_geometries_by_participant = _endpoint_geometries_by_participant(
        session,
        participant_rows,
        project_id=project_id,
    )
    geometries = {
        _require_id(geometry, label="Geometry"): geometry for _, _, geometry in binding_rows
    }
    for endpoint_geometries in endpoint_geometries_by_participant.values():
        for geometry in endpoint_geometries:
            geometries[_require_id(geometry, label="Geometry")] = geometry
    geometry_ids = list(geometries)
    calculation_rows: list[
        tuple[CalculationFrame, CalculationProtocol | None, ThermochemistryResult | None]
    ] = []
    runtimes_by_geometry: dict[UUID, dict[UUID, tuple[int, float | None]]] = {}
    eligible_source_frame_ids: frozenset[UUID] = frozenset()
    if geometry_ids:
        source_rows = session.exec(
            cast(
                Any,
                sa_select(
                    CalculationFrame,
                    CalculationProtocol,
                    ThermochemistryResult,
                    col(ArtifactFile.id),
                    col(ParseRevision.revision_number),
                    col(ParseRevision.running_time_seconds),
                ),
            )
            .join(
                CalculationSegment,
                col(CalculationFrame.segment_id) == col(CalculationSegment.id),
            )
            .join(
                ParseRevision,
                col(CalculationFrame.parse_revision_id) == col(ParseRevision.id),
            )
            .join(
                ArtifactFile,
                col(ParseRevision.artifact_file_id) == col(ArtifactFile.id),
            )
            .join(
                ArtifactIngestion,
                col(ArtifactIngestion.artifact_file_id) == col(ArtifactFile.id),
            )
            .outerjoin(
                CalculationProtocol,
                col(CalculationSegment.protocol_id) == col(CalculationProtocol.id),
            )
            .outerjoin(
                ThermochemistryResult,
                col(ThermochemistryResult.frame_id) == col(CalculationFrame.id),
            )
            .where(
                col(CalculationFrame.geometry_id).in_(geometry_ids),
                col(ParseRevision.status) == ParseStatus.SUCCEEDED,
                _thermodynamic_source_ingestion_predicate(transition_state_geometry_ids),
                col(ArtifactFile.storage_status) != StorageStatus.RETIRED,
            )
        ).all()
        (
            calculation_rows,
            runtimes_by_geometry,
            eligible_source_frame_ids,
        ) = _index_calculation_source_rows(source_rows)

    return _MappedReactionThermodynamicsInput(
        mapped_reaction=mapped_reaction,
        participant_rows=tuple(participant_rows),
        binding_rows=tuple(binding_rows),
        endpoint_geometries_by_participant=endpoint_geometries_by_participant,
        transition_state_node_ids=transition_state_node_ids,
        composites=geometry_energy_composites(
            geometry_ids,
            calculation_rows,
            source_frame_ids_by_geometry=source_frame_ids_by_geometry,
            thermodynamic_only_geometry_ids=transition_state_geometry_ids,
        ),
        runtimes_by_geometry=runtimes_by_geometry,
        eligible_source_frame_ids=eligible_source_frame_ids,
    )


def _build_mapped_reaction_thermodynamics(
    *,
    mapped_reaction_id: UUID,
    participant_rows: Sequence[tuple[MappedReactionParticipant, LogicalReactionParticipant]],
    binding_rows: Sequence[tuple[MappedReactionNode, MappedReactionNodeGeometry, Geometry]],
    endpoint_geometries_by_participant: Mapping[UUID, Sequence[Geometry]] | None = None,
    transition_state_node_ids: frozenset[UUID],
    composites: dict[UUID, GeometryEnergyComposite],
) -> MappedReactionThermodynamics:
    """Build one reaction's profiles from already-loaded source rows."""

    requirements: list[EndpointComponentRequirement] = []
    expected_roles: dict[UUID, MappedReactionNodeRole] = {}
    endpoint_topology_ids_by_participant: dict[UUID, set[UUID]] = {}
    for participant_id, geometries in (endpoint_geometries_by_participant or {}).items():
        endpoint_topology_ids_by_participant[participant_id] = {
            geometry.topology_id
            for geometry in geometries
            if isinstance(geometry.topology_id, UUID)
        }
    for _, binding, geometry in binding_rows:
        binding_participant_id = binding.mapped_reaction_participant_id
        if binding_participant_id is not None and isinstance(geometry.topology_id, UUID):
            endpoint_topology_ids_by_participant.setdefault(binding_participant_id, set()).add(
                geometry.topology_id
            )
    for mapped_participant, logical_participant in participant_rows:
        participant_id = _require_id(mapped_participant, label="MappedReactionParticipant")
        side = mapped_participant.side.value
        allowed_topology_ids = endpoint_topology_ids_by_participant.get(participant_id)
        requirements.append(
            EndpointComponentRequirement(
                side=side,
                mapped_reaction_participant_id=participant_id,
                topology_id=(
                    mapped_participant.concrete_topology_id or logical_participant.topology_id
                ),
                stoichiometric_coefficient=logical_participant.stoichiometric_coefficient,
                allowed_topology_ids=(
                    frozenset(allowed_topology_ids) if allowed_topology_ids else None
                ),
            )
        )
        expected_roles[participant_id] = (
            MappedReactionNodeRole.REACTANT
            if side == "reactant"
            else MappedReactionNodeRole.PRODUCT
        )

    candidates_by_component: dict[UUID, list[GeometryThermodynamicCandidate]] = {}
    transition_state_candidates: list[GeometryThermodynamicCandidate] = []
    seen_endpoints: set[tuple[UUID, UUID]] = set()
    seen_transition_states: set[UUID] = set()
    for node, binding, geometry in binding_rows:
        geometry_id = _require_id(geometry, label="Geometry")
        binding_participant_id = binding.mapped_reaction_participant_id
        if (
            binding_participant_id is not None
            and expected_roles.get(binding_participant_id) == node.role
            and (binding_participant_id, geometry_id) not in seen_endpoints
        ):
            seen_endpoints.add((binding_participant_id, geometry_id))
            candidates_by_component.setdefault(binding_participant_id, []).append(
                GeometryThermodynamicCandidate(
                    geometry_id=geometry_id,
                    topology_id=geometry.topology_id,
                    composite=composites[geometry_id],
                )
            )
        node_id = _require_id(node, label="MappedReactionNode")
        if (
            node.role is MappedReactionNodeRole.TRANSITION_STATE
            and node_id in transition_state_node_ids
            and geometry_id not in seen_transition_states
        ):
            seen_transition_states.add(geometry_id)
            transition_state_candidates.append(
                GeometryThermodynamicCandidate(
                    geometry_id=geometry_id,
                    topology_id=geometry.topology_id,
                    composite=composites[geometry_id],
                )
            )

    for participant_id, endpoint_geometries in (endpoint_geometries_by_participant or {}).items():
        for geometry in endpoint_geometries:
            geometry_id = _require_id(geometry, label="Geometry")
            if (participant_id, geometry_id) in seen_endpoints:
                continue
            seen_endpoints.add((participant_id, geometry_id))
            candidates_by_component.setdefault(participant_id, []).append(
                GeometryThermodynamicCandidate(
                    geometry_id=geometry_id,
                    topology_id=geometry.topology_id,
                    composite=composites[geometry_id],
                )
            )

    return build_mapped_reaction_thermodynamics(
        mapped_reaction_id=mapped_reaction_id,
        endpoint_requirements=requirements,
        candidates_by_component=candidates_by_component,
        transition_state_candidates=transition_state_candidates,
    )


def _profile_source_references(
    profile: Any,
) -> tuple[bool, tuple[_ProfileSourceReference, ...]]:
    """Extract and validate the frame dependencies of one profile DTO."""

    references: dict[UUID, bool] = {}
    selection_count = 0
    complete = True
    states = (
        (profile.reactants, False),
        (profile.transition_state, profile.reactants is None),
        (profile.products, False),
    )
    for state, allow_partial_ingestion in states:
        if state is None:
            continue
        for selection in state.topologies:
            selection_count += 1
            electronic_frame_id = selection.electronic_source_frame_id
            thermochemistry_frame_id = selection.thermochemistry_source_frame_id
            if not isinstance(selection.geometry_id, UUID):
                complete = False
            if not isinstance(electronic_frame_id, UUID):
                complete = False
            if not isinstance(thermochemistry_frame_id, UUID):
                complete = False
            for frame_id in (electronic_frame_id, thermochemistry_frame_id):
                if not isinstance(frame_id, UUID):
                    continue
                previous_allow_partial = references.get(frame_id)
                references[frame_id] = (
                    allow_partial_ingestion
                    if previous_allow_partial is None
                    else previous_allow_partial and allow_partial_ingestion
                )
    return (
        selection_count > 0 and complete,
        tuple(
            _ProfileSourceReference(frame_id, allow_partial)
            for frame_id, allow_partial in sorted(references.items(), key=lambda item: str(item[0]))
        ),
    )


def _materialize_profile_rows(
    result: MappedReactionThermodynamics,
    runtimes_by_geometry: dict[UUID, dict[UUID, tuple[int, float | None]]],
    eligible_source_frame_ids: frozenset[UUID],
) -> tuple[
    MappedReactionThermodynamics,
    list[MappedReactionThermodynamicProfile],
    list[tuple[_ProfileSourceReference, ...]],
]:
    """Convert DTO profiles to persisted rows without database round trips."""

    profile_rows: list[MappedReactionThermodynamicProfile] = []
    source_references_by_profile: list[tuple[_ProfileSourceReference, ...]] = []
    profiles_with_runtime = []
    for profile in result.profiles:
        transition_state = profile.transition_state
        products = profile.products
        reactant_geometry_ids = (
            {selection.geometry_id for selection in profile.reactants.topologies}
            if profile.reactants is not None
            else set()
        )
        transition_state_geometry_ids = (
            {selection.geometry_id for selection in transition_state.topologies}
            if transition_state is not None
            else set()
        )
        product_geometry_ids = (
            {selection.geometry_id for selection in products.topologies}
            if products is not None
            else set()
        )
        all_geometry_ids = (
            reactant_geometry_ids | transition_state_geometry_ids | product_geometry_ids
        )
        runtime_values = {
            "reactants_running_time_seconds": _runtime_for_geometry_ids(
                reactant_geometry_ids,
                runtimes_by_geometry,
            ),
            "transition_state_running_time_seconds": _runtime_for_geometry_ids(
                transition_state_geometry_ids,
                runtimes_by_geometry,
            ),
            "products_running_time_seconds": _runtime_for_geometry_ids(
                product_geometry_ids,
                runtimes_by_geometry,
            ),
            "total_running_time_seconds": _runtime_for_geometry_ids(
                all_geometry_ids,
                runtimes_by_geometry,
            ),
        }
        profile = profile.model_copy(update=runtime_values)
        profiles_with_runtime.append(profile)
        source_evidence_complete, source_references = _profile_source_references(profile)
        source_references_by_profile.append(source_references)
        source_key = {
            "electronic_level": profile.electronic_level,
            "thermochemistry_level": profile.thermochemistry_level,
            "temperature_kelvin": profile.temperature_kelvin,
            "pressure_atm": profile.pressure_atm,
        }
        source_key_hash = hashlib.sha256(
            json.dumps(
                source_key,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        profile_rows.append(
            cast(Any, MappedReactionThermodynamicProfile)(
                id=_uuid7(),
                mapped_reaction_id=profile.mapped_reaction_id,
                policy_version=profile.policy_version,
                source_key_hash=source_key_hash,
                electronic_level=list(profile.electronic_level),
                thermochemistry_level=list(profile.thermochemistry_level),
                temperature_kelvin=profile.temperature_kelvin,
                pressure_atm=profile.pressure_atm,
                source_visibility_status=(
                    ThermodynamicProfileSourceVisibility.VISIBLE
                    if source_evidence_complete
                    and source_references
                    and all(
                        reference.calculation_frame_id in eligible_source_frame_ids
                        for reference in source_references
                    )
                    else ThermodynamicProfileSourceVisibility.HIDDEN
                ),
                source_evidence_complete=source_evidence_complete,
                reactants=(
                    profile.reactants.model_dump(mode="json")
                    if profile.reactants is not None
                    else None
                ),
                transition_state=(
                    transition_state.model_dump(mode="json")
                    if transition_state is not None
                    else None
                ),
                products=products.model_dump(mode="json") if products is not None else None,
                reactants_enthalpy_hartree=(
                    float(profile.reactants.enthalpy_hartree)
                    if profile.reactants is not None
                    else None
                ),
                reactants_gibbs_free_energy_hartree=(
                    float(profile.reactants.gibbs_free_energy_hartree)
                    if profile.reactants is not None
                    else None
                ),
                reactants_entropy_cal_mol_k=(
                    profile.reactants.entropy_cal_mol_k if profile.reactants is not None else None
                ),
                transition_state_enthalpy_hartree=(
                    float(transition_state.enthalpy_hartree)
                    if transition_state is not None
                    else None
                ),
                transition_state_gibbs_free_energy_hartree=(
                    float(transition_state.gibbs_free_energy_hartree)
                    if transition_state is not None
                    else None
                ),
                transition_state_entropy_cal_mol_k=(
                    transition_state.entropy_cal_mol_k if transition_state is not None else None
                ),
                products_enthalpy_hartree=(
                    float(products.enthalpy_hartree) if products is not None else None
                ),
                products_gibbs_free_energy_hartree=(
                    float(products.gibbs_free_energy_hartree) if products is not None else None
                ),
                products_entropy_cal_mol_k=(
                    products.entropy_cal_mol_k if products is not None else None
                ),
                **runtime_values,
            )
        )
    return (
        result.model_copy(update={"profiles": profiles_with_runtime}),
        profile_rows,
        source_references_by_profile,
    )


def _persist_profile_bounds(
    session: Session,
    mapped_reaction_ids: Sequence[UUID],
) -> dict[UUID, tuple[float | None, float | None, float | None, float | None]]:
    """Read generated profile bounds for a batch with one grouped query."""

    rows = session.exec(
        cast(Any, select)(
            col(MappedReactionThermodynamicProfile.mapped_reaction_id),
            func.min(MappedReactionThermodynamicProfile.activation_gibbs_free_energy_kcal_mol),
            func.max(MappedReactionThermodynamicProfile.activation_gibbs_free_energy_kcal_mol),
            func.min(MappedReactionThermodynamicProfile.reaction_gibbs_free_energy_kcal_mol),
            func.max(MappedReactionThermodynamicProfile.reaction_gibbs_free_energy_kcal_mol),
        )
        .where(col(MappedReactionThermodynamicProfile.mapped_reaction_id).in_(mapped_reaction_ids))
        .group_by(col(MappedReactionThermodynamicProfile.mapped_reaction_id))
    ).all()
    return {
        mapped_reaction_id: (
            float(minimum_activation) if minimum_activation is not None else None,
            float(maximum_activation) if maximum_activation is not None else None,
            float(minimum_reaction) if minimum_reaction is not None else None,
            float(maximum_reaction) if maximum_reaction is not None else None,
        )
        for (
            mapped_reaction_id,
            minimum_activation,
            maximum_activation,
            minimum_reaction,
            maximum_reaction,
        ) in rows
        if isinstance(mapped_reaction_id, UUID)
    }


def refresh_mapped_reaction_thermodynamics(
    session: Session,
    mapped_reaction: MappedReaction,
    *,
    _input: _MappedReactionThermodynamicsInput | None = None,
    source_frame_ids_by_geometry: Mapping[UUID, tuple[UUID | None, UUID | None]] | None = None,
    clear_refresh_job: bool = True,
) -> MappedReactionThermodynamics:
    """Recompute and persist one mapping's profile after source facts change.

    This is deliberately called from write workflows. Read queries consume the
    JSON profile, indexed screening bounds, and distinct source-file runtime
    aggregates already stored on MappedReactionThermodynamicProfile.
    """

    mapped_reaction = _attach_or_reuse_entity(session, mapped_reaction)
    _attach_pending_entities(session)
    mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
    if _input is not None and source_frame_ids_by_geometry is not None:
        raise ValueError("cannot combine a prepared thermodynamics input with source frame IDs")
    refresh_input = _input or _load_mapped_reaction_thermodynamics_input(
        session,
        mapped_reaction,
        source_frame_ids_by_geometry=source_frame_ids_by_geometry,
    )
    participant_rows = refresh_input.participant_rows
    binding_rows = refresh_input.binding_rows
    transition_state_node_ids = refresh_input.transition_state_node_ids
    composites = refresh_input.composites
    runtimes_by_geometry = refresh_input.runtimes_by_geometry
    result = _build_mapped_reaction_thermodynamics(
        mapped_reaction_id=mapped_reaction_id,
        participant_rows=participant_rows,
        binding_rows=binding_rows,
        endpoint_geometries_by_participant=refresh_input.endpoint_geometries_by_participant,
        transition_state_node_ids=transition_state_node_ids,
        composites=composites,
    )
    session.exec(
        delete(MappedReactionThermodynamicProfile).where(
            col(MappedReactionThermodynamicProfile.mapped_reaction_id) == mapped_reaction_id
        )
    )
    result, profile_rows, source_references_by_profile = _materialize_profile_rows(
        result,
        runtimes_by_geometry,
        refresh_input.eligible_source_frame_ids,
    )
    session.add_all(profile_rows)
    mapped_reaction.thermodynamic_profile_policy_version = (
        MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION
    )
    session.add(mapped_reaction)
    session.flush()
    _insert_profile_source_rows(session, profile_rows, source_references_by_profile)
    bounds = session.exec(
        select(
            func.min(MappedReactionThermodynamicProfile.activation_gibbs_free_energy_kcal_mol),
            func.max(MappedReactionThermodynamicProfile.activation_gibbs_free_energy_kcal_mol),
            func.min(MappedReactionThermodynamicProfile.reaction_gibbs_free_energy_kcal_mol),
            func.max(MappedReactionThermodynamicProfile.reaction_gibbs_free_energy_kcal_mol),
        ).where(MappedReactionThermodynamicProfile.mapped_reaction_id == mapped_reaction_id)
    ).one()
    mapped_reaction.minimum_activation_gibbs_free_energy_kcal_mol = (
        float(bounds[0]) if bounds[0] is not None else None
    )
    mapped_reaction.maximum_activation_gibbs_free_energy_kcal_mol = (
        float(bounds[1]) if bounds[1] is not None else None
    )
    mapped_reaction.minimum_reaction_gibbs_free_energy_kcal_mol = (
        float(bounds[2]) if bounds[2] is not None else None
    )
    mapped_reaction.maximum_reaction_gibbs_free_energy_kcal_mol = (
        float(bounds[3]) if bounds[3] is not None else None
    )
    session.add(mapped_reaction)
    mapped_reaction.thermodynamic_profile_materialized_generation = (
        mapped_reaction.thermodynamic_profile_generation
    )
    if clear_refresh_job:
        session.exec(
            delete(MappedReactionThermodynamicProfileRefreshJob).where(
                col(MappedReactionThermodynamicProfileRefreshJob.mapped_reaction_id)
                == mapped_reaction_id
            )
        )
    session.flush()
    return result


def enqueue_mapped_reaction_profile_refresh(
    session: Session,
    mapped_reactions: Sequence[MappedReaction],
    *,
    immediate: bool = False,
    priority: int = 0,
) -> tuple[UUID, ...]:
    """Advance profile generations and coalesce durable refresh work.

    This function deliberately runs in the caller's write transaction.  The
    source rows and the refresh request therefore become visible atomically:
    a worker can never observe a generation without its queue row, and a
    failed source write cannot leave a phantom refresh request.
    """

    if not mapped_reactions:
        return ()
    if priority < 0 or priority > 100:
        raise ValueError("profile refresh priority must be between 0 and 100")
    canonical_by_id: dict[UUID, MappedReaction] = {}
    for mapped_reaction in mapped_reactions:
        canonical = _attach_or_reuse_entity(session, mapped_reaction)
        mapped_reaction_id = _require_id(canonical, label="MappedReaction")
        canonical_by_id.setdefault(mapped_reaction_id, canonical)

    mapped_reaction_ids = tuple(canonical_by_id)
    # Generation increments are part of the source-write transaction, but must
    # still be serialized per reaction. This lock is held only for the
    # increment/queue upsert; the expensive profile calculation never runs
    # while it is held.
    persisted_reactions = session.exec(
        select(MappedReaction)
        .where(col(MappedReaction.id).in_(mapped_reaction_ids))
        .with_for_update()
    ).all()
    persisted_by_id = {
        mapped_reaction.id: mapped_reaction
        for mapped_reaction in persisted_reactions
        if isinstance(mapped_reaction.id, UUID)
    }
    for mapped_reaction_id, canonical in tuple(canonical_by_id.items()):
        managed = persisted_by_id.get(mapped_reaction_id, canonical)
        managed.thermodynamic_profile_generation += 1
        canonical_by_id[mapped_reaction_id] = managed
    now = datetime.now(UTC)
    available_at = now
    if not immediate:
        available_at += timedelta(
            seconds=get_settings().upload_worker_profile_refresh_debounce_seconds
        )
    session.add_all(tuple(canonical_by_id.values()))
    session.flush()

    existing_jobs = {
        job.mapped_reaction_id: job
        for job in session.exec(
            select(MappedReactionThermodynamicProfileRefreshJob)
            .where(
                col(MappedReactionThermodynamicProfileRefreshJob.mapped_reaction_id).in_(
                    mapped_reaction_ids
                )
            )
            .with_for_update()
        ).all()
    }
    new_jobs: list[MappedReactionThermodynamicProfileRefreshJob] = []
    for mapped_reaction_id, mapped_reaction in canonical_by_id.items():
        requested_generation = mapped_reaction.thermodynamic_profile_generation
        job = existing_jobs.get(mapped_reaction_id)
        if job is None:
            new_jobs.append(
                MappedReactionThermodynamicProfileRefreshJob(
                    mapped_reaction_id=mapped_reaction_id,
                    requested_generation=requested_generation,
                    status=ThermodynamicProfileRefreshJobStatus.PENDING,
                    priority=priority,
                    requested_at=now,
                    available_at=available_at,
                )
            )
            continue
        job.requested_generation = max(job.requested_generation, requested_generation)
        job.priority = max(job.priority, priority)
        job.requested_at = now
        job.last_error = None
        if job.status == ThermodynamicProfileRefreshJobStatus.PENDING:
            job.available_at = min(job.available_at or available_at, available_at)
            job.lease_id = None
            job.lease_expires_at = None
        session.add(job)
    if new_jobs:
        session.add_all(new_jobs)
    return tuple(canonical_by_id)


def mark_mapped_reactions_thermodynamics_dirty(
    session: Session,
    mapped_reactions: Sequence[MappedReaction],
) -> tuple[UUID, ...]:
    """Compatibility name for callers that defer thermodynamic refresh."""

    return enqueue_mapped_reaction_profile_refresh(session, mapped_reactions)


def refresh_mapped_reactions_thermodynamics(
    session: Session,
    mapped_reactions: Sequence[MappedReaction],
    *,
    clear_refresh_jobs: bool = True,
) -> tuple[MappedReactionThermodynamics, ...]:
    """Refresh several reaction profiles while sharing all source reads.

    A persistence microbatch often dirties many mapped reactions.  The profile
    calculation is reaction-specific, but its participant, binding, frame,
    protocol, thermochemistry, and runtime rows can be loaded with one query
    per relation instead of repeating the same round trips for every reaction.
    Source reads are shared across the batch, and profile replacement plus
    generated bounds are written with one delete, one flush, and one grouped
    aggregate query instead of repeating those operations per reaction.
    """

    if not mapped_reactions:
        return ()
    mapped_reactions_by_id: dict[UUID, MappedReaction] = {}
    for mapped_reaction in mapped_reactions:
        canonical = _attach_or_reuse_entity(session, mapped_reaction)
        mapped_reaction_id = _require_id(canonical, label="MappedReaction")
        mapped_reactions_by_id.setdefault(mapped_reaction_id, canonical)
    mapped_reaction_ids = tuple(mapped_reactions_by_id)
    _attach_pending_entities(session)

    participant_rows = session.exec(
        select(MappedReactionParticipant, LogicalReactionParticipant)
        .join(
            LogicalReactionParticipant,
            col(MappedReactionParticipant.logical_reaction_participant_id)
            == col(LogicalReactionParticipant.id),
        )
        .where(col(MappedReactionParticipant.mapped_reaction_id).in_(mapped_reaction_ids))
        .order_by(
            col(MappedReactionParticipant.mapped_reaction_id),
            col(MappedReactionParticipant.side),
            col(MappedReactionParticipant.template_index),
        )
    ).all()
    participants_by_reaction: dict[
        UUID,
        list[tuple[MappedReactionParticipant, LogicalReactionParticipant]],
    ] = {mapped_reaction_id: [] for mapped_reaction_id in mapped_reaction_ids}
    for participant, logical_participant in participant_rows:
        mapped_reaction_id = participant.mapped_reaction_id
        if isinstance(mapped_reaction_id, UUID):
            participants_by_reaction[mapped_reaction_id].append((participant, logical_participant))

    endpoint_geometries_by_participant: dict[UUID, tuple[Geometry, ...]] = {}
    mapped_reactions_by_project: dict[
        UUID,
        list[tuple[MappedReactionParticipant, LogicalReactionParticipant]],
    ] = {}
    for mapped_reaction_id, rows in participants_by_reaction.items():
        mapped_reaction = mapped_reactions_by_id[mapped_reaction_id]
        if isinstance(mapped_reaction.project_id, UUID):
            mapped_reactions_by_project.setdefault(mapped_reaction.project_id, []).extend(rows)
    for project_id, project_participant_rows in mapped_reactions_by_project.items():
        endpoint_geometries_by_participant.update(
            _endpoint_geometries_by_participant(
                session,
                project_participant_rows,
                project_id=project_id,
            )
        )

    binding_rows = session.exec(
        select(MappedReactionNode, MappedReactionNodeGeometry, Geometry)
        .options(load_only(cast(Any, Geometry.id), cast(Any, Geometry.topology_id)))
        .join(
            MappedReactionNodeGeometry,
            col(MappedReactionNodeGeometry.mapped_reaction_node_id) == col(MappedReactionNode.id),
        )
        .join(Geometry, col(MappedReactionNodeGeometry.geometry_id) == col(Geometry.id))
        .where(col(MappedReactionNode.mapped_reaction_id).in_(mapped_reaction_ids))
    ).all()
    bindings_by_reaction: dict[
        UUID,
        list[tuple[MappedReactionNode, MappedReactionNodeGeometry, Geometry]],
    ] = {mapped_reaction_id: [] for mapped_reaction_id in mapped_reaction_ids}
    geometries: dict[UUID, Geometry] = {}
    for node, binding, geometry in binding_rows:
        mapped_reaction_id = node.mapped_reaction_id
        if isinstance(mapped_reaction_id, UUID):
            bindings_by_reaction[mapped_reaction_id].append((node, binding, geometry))
        geometry_id = _require_id(geometry, label="Geometry")
        geometries[geometry_id] = geometry
    for endpoint_geometries in endpoint_geometries_by_participant.values():
        for geometry in endpoint_geometries:
            geometries[_require_id(geometry, label="Geometry")] = geometry

    endpoint_geometries_by_reaction: dict[UUID, dict[UUID, tuple[Geometry, ...]]] = {
        mapped_reaction_id: {
            _require_id(mapped_participant, label="MappedReactionParticipant"): (
                endpoint_geometries_by_participant.get(
                    _require_id(mapped_participant, label="MappedReactionParticipant"),
                    (),
                )
            )
            for mapped_participant, _logical_participant in participants_by_reaction[
                mapped_reaction_id
            ]
        }
        for mapped_reaction_id in mapped_reaction_ids
    }

    transition_state_node_ids_by_reaction: dict[UUID, set[UUID]] = {
        mapped_reaction_id: set() for mapped_reaction_id in mapped_reaction_ids
    }
    for mapped_reaction_id, node_id in session.exec(
        select(
            col(MappedReactionEdge.mapped_reaction_id),
            col(MappedReactionEdge.transition_state_node_id),
        ).where(
            col(MappedReactionEdge.mapped_reaction_id).in_(mapped_reaction_ids),
            col(MappedReactionEdge.transition_state_node_id).is_not(None),
        )
    ).all():
        if isinstance(mapped_reaction_id, UUID) and isinstance(node_id, UUID):
            transition_state_node_ids_by_reaction[mapped_reaction_id].add(node_id)

    transition_state_geometry_ids = {
        _require_id(geometry, label="Geometry")
        for node, _binding, geometry in binding_rows
        if node.role is MappedReactionNodeRole.TRANSITION_STATE
    }
    geometry_ids = list(geometries)
    calculation_rows: list[
        tuple[CalculationFrame, CalculationProtocol | None, ThermochemistryResult | None]
    ] = []
    runtimes_by_geometry: dict[UUID, dict[UUID, tuple[int, float | None]]] = {}
    eligible_source_frame_ids: frozenset[UUID] = frozenset()
    if geometry_ids:
        source_rows = session.exec(
            cast(
                Any,
                sa_select(
                    CalculationFrame,
                    CalculationProtocol,
                    ThermochemistryResult,
                    col(ArtifactFile.id),
                    col(ParseRevision.revision_number),
                    col(ParseRevision.running_time_seconds),
                ),
            )
            .join(
                CalculationSegment,
                col(CalculationFrame.segment_id) == col(CalculationSegment.id),
            )
            .join(
                ParseRevision,
                col(CalculationFrame.parse_revision_id) == col(ParseRevision.id),
            )
            .join(
                ArtifactFile,
                col(ParseRevision.artifact_file_id) == col(ArtifactFile.id),
            )
            .join(
                ArtifactIngestion,
                col(ArtifactIngestion.artifact_file_id) == col(ArtifactFile.id),
            )
            .outerjoin(
                CalculationProtocol,
                col(CalculationSegment.protocol_id) == col(CalculationProtocol.id),
            )
            .outerjoin(
                ThermochemistryResult,
                col(ThermochemistryResult.frame_id) == col(CalculationFrame.id),
            )
            .where(
                col(CalculationFrame.geometry_id).in_(geometry_ids),
                col(ParseRevision.status) == ParseStatus.SUCCEEDED,
                _thermodynamic_source_ingestion_predicate(transition_state_geometry_ids),
                col(ArtifactFile.storage_status) != StorageStatus.RETIRED,
            )
        ).all()
        (
            calculation_rows,
            runtimes_by_geometry,
            eligible_source_frame_ids,
        ) = _index_calculation_source_rows(source_rows)
    composites = geometry_energy_composites(
        geometry_ids,
        calculation_rows,
        thermodynamic_only_geometry_ids=transition_state_geometry_ids,
    )
    results: list[MappedReactionThermodynamics] = []
    profile_rows: list[MappedReactionThermodynamicProfile] = []
    source_references_by_profile: list[tuple[_ProfileSourceReference, ...]] = []
    for mapped_reaction_id, _mapped_reaction in mapped_reactions_by_id.items():
        result = _build_mapped_reaction_thermodynamics(
            mapped_reaction_id=mapped_reaction_id,
            participant_rows=participants_by_reaction[mapped_reaction_id],
            binding_rows=bindings_by_reaction[mapped_reaction_id],
            endpoint_geometries_by_participant=endpoint_geometries_by_reaction[mapped_reaction_id],
            transition_state_node_ids=frozenset(
                transition_state_node_ids_by_reaction[mapped_reaction_id]
            ),
            composites=composites,
        )
        result, reaction_profile_rows, reaction_source_references = _materialize_profile_rows(
            result,
            runtimes_by_geometry,
            eligible_source_frame_ids,
        )
        results.append(result)
        profile_rows.extend(reaction_profile_rows)
        source_references_by_profile.extend(reaction_source_references)

    session.exec(
        delete(MappedReactionThermodynamicProfile).where(
            col(MappedReactionThermodynamicProfile.mapped_reaction_id).in_(mapped_reaction_ids)
        )
    )
    session.add_all(profile_rows)
    mapped_reaction_values = tuple(mapped_reactions_by_id.values())
    for mapped_reaction in mapped_reaction_values:
        mapped_reaction.thermodynamic_profile_policy_version = (
            MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION
        )
        mapped_reaction.thermodynamic_profile_materialized_generation = (
            mapped_reaction.thermodynamic_profile_generation
        )
    session.add_all(mapped_reaction_values)
    session.flush()
    _insert_profile_source_rows(session, profile_rows, source_references_by_profile)

    bounds_by_reaction_id = _persist_profile_bounds(session, mapped_reaction_ids)
    for mapped_reaction in mapped_reaction_values:
        mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
        bounds = bounds_by_reaction_id.get(mapped_reaction_id, (None, None, None, None))
        mapped_reaction.minimum_activation_gibbs_free_energy_kcal_mol = bounds[0]
        mapped_reaction.maximum_activation_gibbs_free_energy_kcal_mol = bounds[1]
        mapped_reaction.minimum_reaction_gibbs_free_energy_kcal_mol = bounds[2]
        mapped_reaction.maximum_reaction_gibbs_free_energy_kcal_mol = bounds[3]
    session.add_all(mapped_reaction_values)
    if clear_refresh_jobs:
        session.exec(
            delete(MappedReactionThermodynamicProfileRefreshJob).where(
                col(MappedReactionThermodynamicProfileRefreshJob.mapped_reaction_id).in_(
                    mapped_reaction_ids
                )
            )
        )
    session.flush()
    return tuple(results)


__all__ = [
    "enqueue_mapped_reaction_profile_refresh",
    "mark_mapped_reactions_thermodynamics_dirty",
    "refresh_mapped_reaction_thermodynamics",
    "refresh_mapped_reactions_thermodynamics",
]
