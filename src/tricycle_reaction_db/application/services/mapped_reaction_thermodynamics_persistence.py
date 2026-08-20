"""Persist mapped-reaction thermodynamic profiles after source facts change."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.dtos import MappedReactionThermodynamics
from tricycle_reaction_db.application.services._persistence import _require_id
from tricycle_reaction_db.application.services.geometry_energy import geometry_energy_composites
from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics import (
    MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION,
    EndpointComponentRequirement,
    GeometryThermodynamicCandidate,
    build_mapped_reaction_thermodynamics,
)
from tricycle_reaction_db.db.models import (
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
    ThermochemistryResult,
)
from tricycle_reaction_db.domain.enums import MappedReactionNodeRole


def refresh_mapped_reaction_thermodynamics(
    session: Session,
    mapped_reaction: MappedReaction,
) -> MappedReactionThermodynamics:
    """Recompute and persist one mapping's profile after source facts change.

    This is deliberately called from write workflows. Read queries consume the
    JSON profile and indexed screening bounds already stored on MappedReaction.
    """

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
    transition_state_node_ids = set(
        session.exec(
            select(col(MappedReactionEdge.transition_state_node_id)).where(
                MappedReactionEdge.mapped_reaction_id == mapped_reaction_id,
                col(MappedReactionEdge.transition_state_node_id).is_not(None),
            )
        ).all()
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

    composites = geometry_energy_composites(geometry_ids, calculation_rows)
    requirements: list[EndpointComponentRequirement] = []
    expected_roles: dict[UUID, MappedReactionNodeRole] = {}
    for mapped_participant, logical_participant in participant_rows:
        participant_id = _require_id(mapped_participant, label="MappedReactionParticipant")
        side = mapped_participant.side.value
        requirements.append(
            EndpointComponentRequirement(
                side=side,
                mapped_reaction_participant_id=participant_id,
                topology_id=logical_participant.topology_id,
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

    result = build_mapped_reaction_thermodynamics(
        mapped_reaction_id=mapped_reaction_id,
        endpoint_requirements=requirements,
        candidates_by_component=candidates_by_component,
        transition_state_candidates=transition_state_candidates,
    )
    session.exec(
        delete(MappedReactionThermodynamicProfile).where(
            col(MappedReactionThermodynamicProfile.mapped_reaction_id) == mapped_reaction_id
        )
    )
    profile_rows: list[MappedReactionThermodynamicProfile] = []
    for profile in result.profiles:
        transition_state = profile.transition_state
        products = profile.products
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
            MappedReactionThermodynamicProfile(
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
            )
        )
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


__all__ = ["refresh_mapped_reaction_thermodynamics"]
