"""Aggregate source-compatible thermodynamics for mapped reactions."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from tricycle_reaction_db.application.dtos import (
    MappedReactionThermodynamics,
    MappedReactionThermodynamicsProfile,
    ThermodynamicDifferenceView,
    ThermodynamicStateView,
    ThermodynamicTopologyMinimumView,
)
from tricycle_reaction_db.application.services.geometry_energy import GeometryEnergyComposite
from tricycle_reaction_db.core.chemistry_config import (
    MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION,
)
from tricycle_reaction_db.core.units import hartree_per_particle_to_kcal_per_mol
from tricycle_reaction_db.domain.precision import round_energy_hartree


@dataclass(frozen=True, slots=True)
class EndpointComponentRequirement:
    """One mapped participant contribution required on a reaction endpoint."""

    side: str
    mapped_reaction_participant_id: UUID
    topology_id: UUID
    stoichiometric_coefficient: int


@dataclass(frozen=True, slots=True)
class GeometryThermodynamicCandidate:
    """A mapped node Geometry eligible for one endpoint component or TS."""

    geometry_id: UUID
    topology_id: UUID
    composite: GeometryEnergyComposite


SourceKey = tuple[tuple[object, ...], tuple[object, ...], float, float]


def _level_view(level: tuple[object, ...]) -> list[str | None]:
    return [value if value is None or isinstance(value, str) else str(value) for value in level]


def _level_value(level: Sequence[object | None], index: int) -> str:
    if index >= len(level) or level[index] is None:
        return ""
    return str(level[index])


def format_protocol_level(level: Sequence[object | None]) -> str:
    """Return a user-facing method/basis label from a protocol identity."""

    method = (
        _level_value(level, 3)
        or _level_value(level, 1)
        or _level_value(level, 2)
        or _level_value(level, 0)
    )
    basis = _level_value(level, 4)
    if method and basis:
        return f"{method}/{basis}"
    return method or basis or "未标注计算级别"


def format_composite_level_of_theory(
    electronic_level: Sequence[object | None],
    thermochemistry_level: Sequence[object | None],
) -> str:
    """Format thermochemistry//single-point levels in the requested display order."""

    electronic = format_protocol_level(electronic_level)
    thermochemistry = format_protocol_level(thermochemistry_level)
    if electronic == thermochemistry:
        return electronic
    return f"{thermochemistry}//{electronic}"


def _required_float(value: float | None, *, label: str) -> float:
    if value is None:
        raise ValueError(f"complete thermodynamic candidate is missing {label}")
    return float(value)


def _source_key(candidate: GeometryThermodynamicCandidate) -> SourceKey | None:
    composite = candidate.composite
    view = composite.view
    if (
        composite.electronic_level is None
        or composite.thermochemistry_level is None
        or view.temperature_kelvin is None
        or view.pressure_atm is None
    ):
        return None
    return (
        composite.electronic_level,
        composite.thermochemistry_level,
        float(view.temperature_kelvin),
        float(view.pressure_atm),
    )


def _is_complete(candidate: GeometryThermodynamicCandidate) -> bool:
    view = candidate.composite.view
    return (
        candidate.composite.electronic_level is not None
        and candidate.composite.thermochemistry_level is not None
        and view.electronic_selection_status == "selected"
        and view.thermochemistry_selection_status == "selected"
        and view.enthalpy_hartree is not None
        and view.gibbs_free_energy_hartree is not None
        and view.entropy_cal_mol_k is not None
        and view.temperature_kelvin is not None
        and view.pressure_atm is not None
    )


def _minimum_gibbs_candidate(
    candidates: Sequence[GeometryThermodynamicCandidate],
) -> GeometryThermodynamicCandidate:
    return min(
        candidates,
        key=lambda candidate: (
            _required_float(candidate.composite.view.gibbs_free_energy_hartree, label="G"),
            str(candidate.geometry_id),
        ),
    )


def _minimum_view(
    candidate: GeometryThermodynamicCandidate,
    requirement: EndpointComponentRequirement,
) -> ThermodynamicTopologyMinimumView:
    view = candidate.composite.view
    return ThermodynamicTopologyMinimumView(
        side=requirement.side,
        mapped_reaction_participant_id=requirement.mapped_reaction_participant_id,
        topology_id=requirement.topology_id,
        stoichiometric_coefficient=requirement.stoichiometric_coefficient,
        geometry_id=candidate.geometry_id,
        enthalpy_hartree=_required_float(view.enthalpy_hartree, label="H"),
        gibbs_free_energy_hartree=_required_float(view.gibbs_free_energy_hartree, label="G"),
        entropy_cal_mol_k=_required_float(view.entropy_cal_mol_k, label="S"),
    )


def _transition_state_view(
    candidate: GeometryThermodynamicCandidate,
) -> ThermodynamicTopologyMinimumView:
    view = candidate.composite.view
    return ThermodynamicTopologyMinimumView(
        side="transition_state",
        topology_id=candidate.topology_id,
        stoichiometric_coefficient=1,
        geometry_id=candidate.geometry_id,
        enthalpy_hartree=_required_float(view.enthalpy_hartree, label="H"),
        gibbs_free_energy_hartree=_required_float(view.gibbs_free_energy_hartree, label="G"),
        entropy_cal_mol_k=_required_float(view.entropy_cal_mol_k, label="S"),
    )


def _state(
    selections: Sequence[ThermodynamicTopologyMinimumView],
) -> ThermodynamicStateView:
    return ThermodynamicStateView(
        topologies=list(selections),
        enthalpy_hartree=round_energy_hartree(
            sum(
                selection.enthalpy_hartree * selection.stoichiometric_coefficient
                for selection in selections
            )
        ),
        gibbs_free_energy_hartree=round_energy_hartree(
            sum(
                selection.gibbs_free_energy_hartree * selection.stoichiometric_coefficient
                for selection in selections
            )
        ),
        entropy_cal_mol_k=round(
            sum(
                selection.entropy_cal_mol_k * selection.stoichiometric_coefficient
                for selection in selections
            ),
            6,
        ),
    )


def _difference(
    target: ThermodynamicStateView,
    reference: ThermodynamicStateView,
) -> ThermodynamicDifferenceView:
    return ThermodynamicDifferenceView(
        enthalpy_kcal_mol=round(
            hartree_per_particle_to_kcal_per_mol(
                target.enthalpy_hartree - reference.enthalpy_hartree
            ),
            6,
        ),
        gibbs_free_energy_kcal_mol=round(
            hartree_per_particle_to_kcal_per_mol(
                target.gibbs_free_energy_hartree - reference.gibbs_free_energy_hartree
            ),
            6,
        ),
        entropy_cal_mol_k=round(
            target.entropy_cal_mol_k - reference.entropy_cal_mol_k,
            6,
        ),
    )


def _requirements_by_component(
    requirements: Sequence[EndpointComponentRequirement],
) -> list[EndpointComponentRequirement]:
    coefficients: dict[tuple[str, UUID, UUID], int] = defaultdict(int)
    for requirement in requirements:
        coefficients[
            (
                requirement.side,
                requirement.mapped_reaction_participant_id,
                requirement.topology_id,
            )
        ] += requirement.stoichiometric_coefficient
    return [
        EndpointComponentRequirement(side, participant_id, topology_id, coefficient)
        for (side, participant_id, topology_id), coefficient in sorted(
            coefficients.items(), key=lambda item: (item[0][0], str(item[0][1]))
        )
    ]


def _candidate_keys(
    candidates: Sequence[GeometryThermodynamicCandidate],
    *,
    topology_id: UUID | None = None,
) -> set[SourceKey]:
    return {
        key
        for candidate in candidates
        if (topology_id is None or candidate.topology_id == topology_id)
        and _is_complete(candidate)
        and (key := _source_key(candidate)) is not None
    }


def _common_component_keys(
    requirements: Sequence[EndpointComponentRequirement],
    candidates_by_component: Mapping[UUID, Sequence[GeometryThermodynamicCandidate]],
) -> set[SourceKey]:
    common_keys: set[SourceKey] | None = None
    for requirement in requirements:
        keys = _candidate_keys(
            candidates_by_component.get(requirement.mapped_reaction_participant_id, ()),
            topology_id=requirement.topology_id,
        )
        common_keys = keys if common_keys is None else common_keys & keys
    return common_keys or set()


def build_mapped_reaction_thermodynamics(
    *,
    mapped_reaction_id: UUID,
    endpoint_requirements: Sequence[EndpointComponentRequirement],
    candidates_by_component: Mapping[UUID, Sequence[GeometryThermodynamicCandidate]],
    transition_state_candidates: Sequence[GeometryThermodynamicCandidate],
) -> MappedReactionThermodynamics:
    """Return source-compatible profiles using only one mapped reaction's bindings."""

    requirements = _requirements_by_component(endpoint_requirements)
    reactant_requirements = [item for item in requirements if item.side == "reactant"]
    product_requirements = [item for item in requirements if item.side == "product"]
    empty = MappedReactionThermodynamics(mapped_reaction_id=mapped_reaction_id, profiles=[])
    if not reactant_requirements or not product_requirements:
        return empty

    reactant_keys = _common_component_keys(reactant_requirements, candidates_by_component)
    reaction_keys = reactant_keys & _common_component_keys(
        product_requirements, candidates_by_component
    )
    activation_keys = reactant_keys & _candidate_keys(transition_state_candidates)
    source_keys = reaction_keys | activation_keys

    profiles: list[MappedReactionThermodynamicsProfile] = []
    for source_key in sorted(source_keys, key=repr):
        has_reaction = source_key in reaction_keys
        has_activation = source_key in activation_keys
        endpoint_selections: dict[UUID, ThermodynamicTopologyMinimumView] = {}
        selected_requirements = [
            *reactant_requirements,
            *(product_requirements if has_reaction else []),
        ]
        for requirement in selected_requirements:
            compatible_candidates = [
                candidate
                for candidate in candidates_by_component.get(
                    requirement.mapped_reaction_participant_id, ()
                )
                if candidate.topology_id == requirement.topology_id
                and _is_complete(candidate)
                and _source_key(candidate) == source_key
            ]
            if not compatible_candidates:
                break
            endpoint_selections[requirement.mapped_reaction_participant_id] = _minimum_view(
                _minimum_gibbs_candidate(compatible_candidates),
                requirement,
            )
        else:
            reactants = _state(
                [
                    endpoint_selections[requirement.mapped_reaction_participant_id]
                    for requirement in reactant_requirements
                ]
            )
            products = (
                _state(
                    [
                        endpoint_selections[requirement.mapped_reaction_participant_id]
                        for requirement in product_requirements
                    ]
                )
                if has_reaction
                else None
            )
            transition_state = None
            if has_activation:
                compatible_ts_candidates = [
                    candidate
                    for candidate in transition_state_candidates
                    if _is_complete(candidate) and _source_key(candidate) == source_key
                ]
                if not compatible_ts_candidates:
                    continue
                transition_state_candidate = _minimum_gibbs_candidate(compatible_ts_candidates)
                transition_state = _state([_transition_state_view(transition_state_candidate)])
            profiles.append(
                MappedReactionThermodynamicsProfile(
                    mapped_reaction_id=mapped_reaction_id,
                    policy_version=MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION,
                    electronic_level=_level_view(source_key[0]),
                    thermochemistry_level=_level_view(source_key[1]),
                    level_of_theory=format_composite_level_of_theory(source_key[0], source_key[1]),
                    temperature_kelvin=source_key[2],
                    pressure_atm=source_key[3],
                    reactants=reactants,
                    transition_state=transition_state,
                    products=products,
                    activation=(
                        _difference(transition_state, reactants)
                        if transition_state is not None
                        else None
                    ),
                    reaction=(_difference(products, reactants) if products is not None else None),
                )
            )

    return MappedReactionThermodynamics(
        mapped_reaction_id=mapped_reaction_id,
        profiles=profiles,
    )


__all__ = [
    "EndpointComponentRequirement",
    "GeometryThermodynamicCandidate",
    "MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION",
    "build_mapped_reaction_thermodynamics",
    "format_composite_level_of_theory",
    "format_protocol_level",
]
