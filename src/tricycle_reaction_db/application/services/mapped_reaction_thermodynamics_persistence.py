"""Persist mapped-reaction thermodynamic profiles after source facts change."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, delete, func, or_
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.dtos import MappedReactionThermodynamics
from tricycle_reaction_db.application.services._persistence import (
    _attach_pending_entities,
    _require_id,
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
)


def _thermodynamic_source_ingestion_predicate(
    transition_state_geometry_ids: set[UUID],
) -> Any:
    """Allow complete TS evidence from a partially indexed artifact.

    Some legacy autode artifacts were marked ``partial`` because one parse
    revision in the artifact batch was incomplete, while the selected TS
    frequency frame itself is complete and carries H/G/S.  Endpoint source
    selection remains restricted to successful artifacts; this narrow
    predicate only admits complete TS frames for TS-only profiles.
    """

    if not transition_state_geometry_ids:
        return col(ArtifactIngestion.status) == ArtifactIngestionStatus.SUCCEEDED
    return or_(
        col(ArtifactIngestion.status) == ArtifactIngestionStatus.SUCCEEDED,
        and_(
            col(ArtifactIngestion.status) == ArtifactIngestionStatus.PARTIAL,
            col(CalculationFrame.geometry_id).in_(transition_state_geometry_ids),
            col(ParseRevision.parse_completeness) == ParseCompleteness.COMPLETE,
            col(ParseRevision.source_complete).is_(True),
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
        logical_participant_id: set()
        for logical_participant_id in logical_participant_ids
    }
    if logical_participant_ids:
        memberships = session.exec(
            select(LogicalParticipantConcreteTopology).where(
                col(
                    LogicalParticipantConcreteTopology.logical_reaction_participant_id
                ).in_(logical_participant_ids)
            )
        ).all()
        for membership in memberships:
            logical_participant_id = membership.logical_reaction_participant_id
            concrete_topology_id = membership.concrete_topology_id
            if (
                isinstance(logical_participant_id, UUID)
                and isinstance(concrete_topology_id, UUID)
            ):
                concrete_topology_ids_by_logical.setdefault(logical_participant_id, set()).add(
                    concrete_topology_id
                )

    allowed_topology_ids_by_participant: dict[UUID, set[UUID]] = {}
    logical_id_by_participant = {
        _require_id(mapped_participant, label="MappedReactionParticipant"):
        _require_id(logical_participant, label="LogicalReactionParticipant")
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
        select(Geometry).where(
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

    # MolOP endpoint reconstruction can retain an endpoint-only bond and a
    # different electronic form from the corresponding isolated optimization.
    # That is not a normal topology membership and must not be treated as one:
    # only search for it after every strict/member source has failed, and
    # accept it only when exactly one compatible topology has eligible
    # thermochemistry in this project.
    endpoint_compatible_topology_ids_by_participant: dict[UUID, set[UUID]] = {}
    strict_topologies = session.exec(
        select(MolecularTopology).where(
            col(MolecularTopology.id).in_(set(strict_topology_ids.values()))
        )
    ).all()
    strict_topologies_by_id = {
        topology.id: topology
        for topology in strict_topologies
        if isinstance(topology.id, UUID)
    }
    endpoint_compatible_search_specs: dict[UUID, MolecularTopology] = {}
    for participant_id in participant_ids:
        strict_topology_id = strict_topology_ids[participant_id]
        if geometries_by_topology.get(strict_topology_id):
            continue
        member_ids = allowed_topology_ids_by_participant[participant_id] - {strict_topology_id}
        if any(geometries_by_topology.get(topology_id) for topology_id in member_ids):
            continue
        strict_topology = strict_topologies_by_id.get(strict_topology_id)
        if strict_topology is not None:
            endpoint_compatible_search_specs[participant_id] = strict_topology

    if endpoint_compatible_search_specs:
        candidate_topologies = session.exec(
            select(MolecularTopology).where(
                col(MolecularTopology.project_id) == project_id,
                col(MolecularTopology.formula_id).in_(
                    {
                        topology.formula_id
                        for topology in endpoint_compatible_search_specs.values()
                    }
                ),
            )
        ).all()
        endpoint_compatible_topologies_by_participant: dict[UUID, set[UUID]] = {}
        endpoint_compatible_candidate_ids: set[UUID] = set()
        for participant_id, strict_topology in endpoint_compatible_search_specs.items():
            allowed_ids = allowed_topology_ids_by_participant[participant_id]
            matching_ids = {
                topology.id
                for topology in candidate_topologies
                if isinstance(topology.id, UUID)
                and topology.id not in allowed_ids
                and topology.formula_id == strict_topology.formula_id
                and topology.atom_count == strict_topology.atom_count
                and topology.formal_charge == strict_topology.formal_charge
                and topology.fragment_count == strict_topology.fragment_count
                and source_geometry_compatible_topology(strict_topology.mol, topology.mol)
            }
            if matching_ids:
                endpoint_compatible_topologies_by_participant[participant_id] = matching_ids
                endpoint_compatible_candidate_ids.update(matching_ids)
        if endpoint_compatible_candidate_ids:
            endpoint_compatible_geometries = session.exec(
                select(Geometry).where(
                    col(Geometry.project_id) == project_id,
                    col(Geometry.topology_id).in_(endpoint_compatible_candidate_ids),
                    col(Geometry.id).in_(eligible_geometry_ids),
                    geometry_has_thermodynamic_property_predicate(col(Geometry.id)),
                    geometry_has_no_imaginary_frequency_predicate(col(Geometry.id)),
                )
            ).all()
            for geometry in endpoint_compatible_geometries:
                topology_id = geometry.topology_id
                if isinstance(topology_id, UUID):
                    geometries_by_topology.setdefault(topology_id, []).append(geometry)
            for (
                participant_id,
                topology_ids,
            ) in endpoint_compatible_topologies_by_participant.items():
                eligible_ids = {
                    topology_id
                    for topology_id in topology_ids
                    if geometries_by_topology.get(topology_id)
                }
                # More than one eligible endpoint-compatible topology is ambiguous and
                # remains intentionally unresolved instead of guessing.
                if len(eligible_ids) == 1:
                    endpoint_compatible_topology_ids_by_participant[participant_id] = eligible_ids

    result: dict[UUID, tuple[Geometry, ...]] = {}
    for participant_id in participant_ids:
        strict_topology_id = strict_topology_ids[participant_id]
        strict_geometries = geometries_by_topology.get(strict_topology_id, [])
        selected = strict_geometries or [
            geometry
            for topology_id in sorted(
                allowed_topology_ids_by_participant[participant_id], key=str
            )
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
                    geometry.id: geometry
                    for geometry in selected
                    if geometry.id is not None
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
    calculation_rows: Sequence[Any] = ()
    if geometry_ids:
        calculation_rows = session.exec(
            select(CalculationFrame, CalculationProtocol, ThermochemistryResult)
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

    runtimes_by_geometry: dict[UUID, dict[UUID, tuple[int, float | None]]] = {}
    if geometry_ids:
        runtime_rows = session.exec(
            select(
                col(CalculationFrame.geometry_id),
                col(ArtifactFile.id),
                col(ParseRevision.revision_number),
                col(ParseRevision.running_time_seconds),
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
            .where(
                col(CalculationFrame.geometry_id).in_(geometry_ids),
                col(ParseRevision.status) == ParseStatus.SUCCEEDED,
                _thermodynamic_source_ingestion_predicate(transition_state_geometry_ids),
                col(ArtifactFile.storage_status) != StorageStatus.RETIRED,
            )
        ).all()
        for geometry_id, artifact_id, revision_number, running_time in runtime_rows:
            if geometry_id is None or artifact_id is None:
                continue
            by_file = runtimes_by_geometry.setdefault(geometry_id, {})
            previous = by_file.get(artifact_id)
            candidate = (int(revision_number), running_time)
            if previous is None or candidate[0] > previous[0]:
                by_file[artifact_id] = candidate

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
        participant_id = binding.mapped_reaction_participant_id
        if participant_id is not None and isinstance(geometry.topology_id, UUID):
            endpoint_topology_ids_by_participant.setdefault(participant_id, set()).add(
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

    for participant_id, endpoint_geometries in (
        endpoint_geometries_by_participant or {}
    ).items():
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


def _materialize_profile_rows(
    result: MappedReactionThermodynamics,
    runtimes_by_geometry: dict[UUID, dict[UUID, tuple[int, float | None]]],
) -> tuple[MappedReactionThermodynamics, list[MappedReactionThermodynamicProfile]]:
    """Convert DTO profiles to persisted rows without database round trips."""

    profile_rows: list[MappedReactionThermodynamicProfile] = []
    profiles_with_runtime = []
    for profile in result.profiles:
        transition_state = profile.transition_state
        products = profile.products
        reactant_geometry_ids = {
            selection.geometry_id for selection in profile.reactants.topologies
        } if profile.reactants is not None else set()
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
                mapped_reaction_id=profile.mapped_reaction_id,
                policy_version=profile.policy_version,
                source_key_hash=source_key_hash,
                electronic_level=list(profile.electronic_level),
                thermochemistry_level=list(profile.thermochemistry_level),
                temperature_kelvin=profile.temperature_kelvin,
                pressure_atm=profile.pressure_atm,
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
                    profile.reactants.entropy_cal_mol_k
                    if profile.reactants is not None
                    else None
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
    return result.model_copy(update={"profiles": profiles_with_runtime}), profile_rows


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
) -> MappedReactionThermodynamics:
    """Recompute and persist one mapping's profile after source facts change.

    This is deliberately called from write workflows. Read queries consume the
    JSON profile, indexed screening bounds, and distinct source-file runtime
    aggregates already stored on MappedReactionThermodynamicProfile.
    """

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
    profile_rows: list[MappedReactionThermodynamicProfile] = []
    profiles_with_runtime = []
    for profile in result.profiles:
        transition_state = profile.transition_state
        products = profile.products
        reactant_geometry_ids = {
            selection.geometry_id for selection in profile.reactants.topologies
        } if profile.reactants is not None else set()
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
                mapped_reaction_id=mapped_reaction_id,
                policy_version=profile.policy_version,
                source_key_hash=source_key_hash,
                electronic_level=list(profile.electronic_level),
                thermochemistry_level=list(profile.thermochemistry_level),
                temperature_kelvin=profile.temperature_kelvin,
                pressure_atm=profile.pressure_atm,
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
                    profile.reactants.entropy_cal_mol_k
                    if profile.reactants is not None
                    else None
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
    result = result.model_copy(update={"profiles": profiles_with_runtime})
    session.add_all(profile_rows)
    mapped_reaction.thermodynamic_profile_policy_version = (
        MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION
    )
    session.add(mapped_reaction)
    session.flush()
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
    session.flush()
    return result


def refresh_mapped_reactions_thermodynamics(
    session: Session,
    mapped_reactions: Sequence[MappedReaction],
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
        mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
        mapped_reactions_by_id.setdefault(mapped_reaction_id, mapped_reaction)
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
    calculation_rows: Sequence[Any] = ()
    if geometry_ids:
        calculation_rows = session.exec(
            select(CalculationFrame, CalculationProtocol, ThermochemistryResult)
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

    runtimes_by_geometry: dict[UUID, dict[UUID, tuple[int, float | None]]] = {}
    if geometry_ids:
        runtime_rows = session.exec(
            select(
                col(CalculationFrame.geometry_id),
                col(ArtifactFile.id),
                col(ParseRevision.revision_number),
                col(ParseRevision.running_time_seconds),
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
            .where(
                col(CalculationFrame.geometry_id).in_(geometry_ids),
                col(ParseRevision.status) == ParseStatus.SUCCEEDED,
                _thermodynamic_source_ingestion_predicate(transition_state_geometry_ids),
                col(ArtifactFile.storage_status) != StorageStatus.RETIRED,
            )
        ).all()
        for geometry_id, artifact_id, revision_number, running_time in runtime_rows:
            if geometry_id is None or artifact_id is None:
                continue
            by_file = runtimes_by_geometry.setdefault(geometry_id, {})
            previous = by_file.get(artifact_id)
            candidate = (int(revision_number), running_time)
            if previous is None or candidate[0] > previous[0]:
                by_file[artifact_id] = candidate

    composites = geometry_energy_composites(
        geometry_ids,
        calculation_rows,
        thermodynamic_only_geometry_ids=transition_state_geometry_ids,
    )
    results: list[MappedReactionThermodynamics] = []
    profile_rows: list[MappedReactionThermodynamicProfile] = []
    for mapped_reaction_id, _mapped_reaction in mapped_reactions_by_id.items():
        result = _build_mapped_reaction_thermodynamics(
            mapped_reaction_id=mapped_reaction_id,
            participant_rows=participants_by_reaction[mapped_reaction_id],
            binding_rows=bindings_by_reaction[mapped_reaction_id],
            endpoint_geometries_by_participant=endpoint_geometries_by_reaction[
                mapped_reaction_id
            ],
            transition_state_node_ids=frozenset(
                transition_state_node_ids_by_reaction[mapped_reaction_id]
            ),
            composites=composites,
        )
        result, reaction_profile_rows = _materialize_profile_rows(
            result,
            runtimes_by_geometry,
        )
        results.append(result)
        profile_rows.extend(reaction_profile_rows)

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
    session.add_all(mapped_reaction_values)
    session.flush()

    bounds_by_reaction_id = _persist_profile_bounds(session, mapped_reaction_ids)
    for mapped_reaction in mapped_reaction_values:
        mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
        bounds = bounds_by_reaction_id.get(mapped_reaction_id, (None, None, None, None))
        mapped_reaction.minimum_activation_gibbs_free_energy_kcal_mol = bounds[0]
        mapped_reaction.maximum_activation_gibbs_free_energy_kcal_mol = bounds[1]
        mapped_reaction.minimum_reaction_gibbs_free_energy_kcal_mol = bounds[2]
        mapped_reaction.maximum_reaction_gibbs_free_energy_kcal_mol = bounds[3]
    session.add_all(mapped_reaction_values)
    session.flush()
    return tuple(results)


__all__ = [
    "refresh_mapped_reaction_thermodynamics",
    "refresh_mapped_reactions_thermodynamics",
]
