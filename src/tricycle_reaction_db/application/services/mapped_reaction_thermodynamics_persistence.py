"""Persist mapped-reaction thermodynamic profiles after source facts change."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, func
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
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    CalculationFrame,
    CalculationProtocol,
    CalculationSegment,
    Geometry,
    LogicalReactionParticipant,
    MappedReaction,
    MappedReactionEdge,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionParticipant,
    MappedReactionThermodynamicProfile,
    ParseRevision,
    ThermochemistryResult,
)
from tricycle_reaction_db.domain.enums import MappedReactionNodeRole


@dataclass(slots=True)
class _MappedReactionThermodynamicsInput:
    """All shared source facts needed to build one reaction profile."""

    mapped_reaction: MappedReaction
    participant_rows: tuple[tuple[MappedReactionParticipant, LogicalReactionParticipant], ...]
    binding_rows: tuple[tuple[MappedReactionNode, MappedReactionNodeGeometry, Geometry], ...]
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


def _load_mapped_reaction_thermodynamics_input(
    session: Session,
    mapped_reaction: MappedReaction,
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
    geometries = {
        _require_id(geometry, label="Geometry"): geometry for _, _, geometry in binding_rows
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
            .outerjoin(
                CalculationProtocol,
                col(CalculationSegment.protocol_id) == col(CalculationProtocol.id),
            )
            .outerjoin(
                ThermochemistryResult,
                col(ThermochemistryResult.frame_id) == col(CalculationFrame.id),
            )
            .where(col(CalculationFrame.geometry_id).in_(geometry_ids))
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
            .where(col(CalculationFrame.geometry_id).in_(geometry_ids))
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
        transition_state_node_ids=transition_state_node_ids,
        composites=geometry_energy_composites(geometry_ids, calculation_rows),
        runtimes_by_geometry=runtimes_by_geometry,
    )


def _build_mapped_reaction_thermodynamics(
    *,
    mapped_reaction_id: UUID,
    participant_rows: Sequence[tuple[MappedReactionParticipant, LogicalReactionParticipant]],
    binding_rows: Sequence[tuple[MappedReactionNode, MappedReactionNodeGeometry, Geometry]],
    transition_state_node_ids: frozenset[UUID],
    composites: dict[UUID, GeometryEnergyComposite],
) -> MappedReactionThermodynamics:
    """Build one reaction's profiles from already-loaded source rows."""

    requirements: list[EndpointComponentRequirement] = []
    expected_roles: dict[UUID, MappedReactionNodeRole] = {}
    for mapped_participant, logical_participant in participant_rows:
        participant_id = _require_id(mapped_participant, label="MappedReactionParticipant")
        side = mapped_participant.side.value
        requirements.append(
            EndpointComponentRequirement(
                side=side,
                mapped_reaction_participant_id=participant_id,
                topology_id=(
                    mapped_participant.concrete_topology_id or logical_participant.topology_id
                ),
                stoichiometric_coefficient=logical_participant.stoichiometric_coefficient,
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
        }
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
                reactants=profile.reactants.model_dump(mode="json"),
                transition_state=(
                    transition_state.model_dump(mode="json")
                    if transition_state is not None
                    else None
                ),
                products=products.model_dump(mode="json") if products is not None else None,
                reactants_enthalpy_hartree=float(profile.reactants.enthalpy_hartree),
                reactants_gibbs_free_energy_hartree=float(
                    profile.reactants.gibbs_free_energy_hartree
                ),
                reactants_entropy_cal_mol_k=profile.reactants.entropy_cal_mol_k,
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
) -> MappedReactionThermodynamics:
    """Recompute and persist one mapping's profile after source facts change.

    This is deliberately called from write workflows. Read queries consume the
    JSON profile, indexed screening bounds, and distinct source-file runtime
    aggregates already stored on MappedReactionThermodynamicProfile.
    """

    _attach_pending_entities(session)
    mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
    refresh_input = _input or _load_mapped_reaction_thermodynamics_input(
        session,
        mapped_reaction,
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
        }
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
                reactants=profile.reactants.model_dump(mode="json"),
                transition_state=(
                    transition_state.model_dump(mode="json")
                    if transition_state is not None
                    else None
                ),
                products=products.model_dump(mode="json") if products is not None else None,
                reactants_enthalpy_hartree=float(profile.reactants.enthalpy_hartree),
                reactants_gibbs_free_energy_hartree=float(
                    profile.reactants.gibbs_free_energy_hartree
                ),
                reactants_entropy_cal_mol_k=profile.reactants.entropy_cal_mol_k,
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

    geometry_ids = list(geometries)
    calculation_rows: Sequence[Any] = ()
    if geometry_ids:
        calculation_rows = session.exec(
            select(CalculationFrame, CalculationProtocol, ThermochemistryResult)
            .join(
                CalculationSegment,
                col(CalculationFrame.segment_id) == col(CalculationSegment.id),
            )
            .outerjoin(
                CalculationProtocol,
                col(CalculationSegment.protocol_id) == col(CalculationProtocol.id),
            )
            .outerjoin(
                ThermochemistryResult,
                col(ThermochemistryResult.frame_id) == col(CalculationFrame.id),
            )
            .where(col(CalculationFrame.geometry_id).in_(geometry_ids))
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
            .where(col(CalculationFrame.geometry_id).in_(geometry_ids))
        ).all()
        for geometry_id, artifact_id, revision_number, running_time in runtime_rows:
            if geometry_id is None or artifact_id is None:
                continue
            by_file = runtimes_by_geometry.setdefault(geometry_id, {})
            previous = by_file.get(artifact_id)
            candidate = (int(revision_number), running_time)
            if previous is None or candidate[0] > previous[0]:
                by_file[artifact_id] = candidate

    composites = geometry_energy_composites(geometry_ids, calculation_rows)
    results: list[MappedReactionThermodynamics] = []
    profile_rows: list[MappedReactionThermodynamicProfile] = []
    for mapped_reaction_id, _mapped_reaction in mapped_reactions_by_id.items():
        result = _build_mapped_reaction_thermodynamics(
            mapped_reaction_id=mapped_reaction_id,
            participant_rows=participants_by_reaction[mapped_reaction_id],
            binding_rows=bindings_by_reaction[mapped_reaction_id],
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
