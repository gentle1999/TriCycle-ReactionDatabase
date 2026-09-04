"""Geometry-owned composite energy selection and projection."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from tricycle_reaction_db.application.dtos import GeometryEnergyView
from tricycle_reaction_db.core.chemistry_config import GEOMETRY_ENERGY_POLICY_VERSION
from tricycle_reaction_db.db.models import (
    CalculationFrame,
    CalculationProtocol,
    ThermochemistryResult,
)
from tricycle_reaction_db.domain.precision import round_energy_hartree

_METHOD_FAMILY_RANKS = {
    "POST-HF": 400,
    "POSTHF": 400,
    "WAVEFUNCTION": 400,
    "DFT": 300,
    "HF": 250,
    "SEMIEMPIRICAL": 100,
    "SEMI-EMPIRICAL": 100,
    "MM": 10,
    "FORCE-FIELD": 10,
}
_METHOD_RANKS = {
    "CCSD(T)": 90,
    "CCSDT": 90,
    "CCSD": 80,
    "MP5": 70,
    "MP4": 60,
    "MP3": 50,
    "MP2": 40,
    "HF": 10,
}
_FUNCTIONAL_RANKS = {
    "WB97M-V": 90,
    "WB97M": 88,
    "WB97X-V": 85,
    "WB97X-D": 82,
    "M06-2X": 78,
    "PBE0": 70,
    "TPSSH": 65,
    "B3LYP": 50,
    "PBE": 40,
    "BP86": 35,
}
_BASIS_RANKS = {
    "DEF2-SVP": 20,
    "DEF2SVP": 20,
    "DEF2-TZVP": 30,
    "DEF2TZVP": 30,
    "DEF2-TZVPP": 32,
    "DEF2TZVPP": 32,
    "DEF2-QZVP": 40,
    "DEF2QZVP": 40,
    "DEF2-QZVPP": 42,
    "DEF2QZVPP": 42,
    "6-31G": 15,
    "6-31G*": 20,
    "6-311G": 25,
    "6-311G*": 28,
    "CC-PVDZ": 20,
    "CCPVDZ": 20,
    "CC-PVTZ": 30,
    "CCPVTZ": 30,
    "CC-PVQZ": 40,
    "CCPVQZ": 40,
}


@dataclass(frozen=True, slots=True)
class GeometryEnergyCandidate:
    frame: CalculationFrame
    protocol: CalculationProtocol | None
    thermochemistry: ThermochemistryResult | None


@dataclass(frozen=True, slots=True)
class GeometryEnergyComposite:
    view: GeometryEnergyView
    electronic_level: tuple[object, ...] | None
    thermochemistry_level: tuple[object, ...] | None


def _normalise_level_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", "", value.upper().replace("Ω", "W").replace("ω", "W"))


def _functional_name(protocol: CalculationProtocol | None) -> str:
    value = _normalise_level_text(protocol.functional if protocol is not None else None)
    for suffix in ("-GD3BJ", "GD3BJ", "-D3BJ", "D3BJ", "-D3", "D3"):
        value = value.removesuffix(suffix)
    return value


def _method_family_rank(protocol: CalculationProtocol | None) -> int:
    family = _normalise_level_text(protocol.method_family if protocol is not None else None)
    method = _normalise_level_text(protocol.method if protocol is not None else None)
    if family in _METHOD_FAMILY_RANKS:
        return _METHOD_FAMILY_RANKS[family]
    if method.startswith(("CCSD", "MP")):
        return _METHOD_FAMILY_RANKS["POST-HF"]
    if method in {"HF", "RHF", "UHF", "ROHF"}:
        return _METHOD_FAMILY_RANKS["HF"]
    return 0


def protocol_level(protocol: CalculationProtocol | None) -> tuple[int, int, int, str]:
    family = _method_family_rank(protocol)
    method = _normalise_level_text(protocol.method if protocol is not None else None)
    functional = _functional_name(protocol)
    basis = _normalise_level_text(protocol.basis_set if protocol is not None else None)
    dispersion = _normalise_level_text(protocol.dispersion_model if protocol is not None else None)
    method_rank = max(_METHOD_RANKS.get(method, 0), _FUNCTIONAL_RANKS.get(functional, 0))
    method_axis = family * 100 + method_rank
    dispersion_rank = int(functional.endswith("V") or dispersion in {"D3", "D3BJ", "GD3", "GD3BJ"})
    return (
        method_axis,
        _BASIS_RANKS.get(basis, 0),
        dispersion_rank,
        (f"{method}|{functional}|{basis}"),
    )


def protocol_dominates(
    candidate: CalculationProtocol | None,
    incumbent: CalculationProtocol | None,
) -> bool:
    candidate_axes = protocol_level(candidate)[:3]
    incumbent_axes = protocol_level(incumbent)[:3]
    return all(
        left >= right for left, right in zip(candidate_axes, incumbent_axes, strict=True)
    ) and any(left > right for left, right in zip(candidate_axes, incumbent_axes, strict=True))


def _frame_order(frame: CalculationFrame) -> tuple[str, str]:
    return (
        frame.created_at.isoformat() if frame.created_at is not None else "",
        str(frame.id),
    )


def _select_candidate(
    candidates: Sequence[GeometryEnergyCandidate],
    *,
    context: Callable[[GeometryEnergyCandidate], tuple[object, ...]],
) -> tuple[str, GeometryEnergyCandidate | None, list[UUID]]:
    if not candidates:
        return "missing", None, []
    contexts = {context(candidate) for candidate in candidates}
    if len(contexts) != 1:
        all_candidate_ids: list[UUID] = sorted(
            [item.frame.id for item in candidates if item.frame.id is not None],
            key=str,
        )
        return "ambiguous", None, all_candidate_ids
    non_dominated = [
        candidate
        for candidate in candidates
        if not any(
            protocol_dominates(other.protocol, candidate.protocol)
            for other in candidates
            if other is not candidate
        )
    ]
    top_levels = {protocol_level(candidate.protocol)[:3] for candidate in non_dominated}
    top_candidate_ids: list[UUID] = sorted(
        [item.frame.id for item in non_dominated if item.frame.id is not None],
        key=str,
    )
    if len(top_levels) != 1:
        return "ambiguous", None, top_candidate_ids
    return (
        "selected",
        min(non_dominated, key=lambda item: _frame_order(item.frame)),
        top_candidate_ids,
    )


def _protocol_identity(protocol: CalculationProtocol | None) -> tuple[object, ...] | None:
    if protocol is None:
        return None
    return (
        protocol.method_family,
        protocol.method,
        protocol.reference_method,
        protocol.functional,
        protocol.basis_set,
        protocol.auxiliary_basis_set,
        protocol.dispersion_model,
        protocol.solvation_model,
        protocol.solvent,
    )


def geometry_energy_composite(
    geometry_id: UUID,
    candidates: Sequence[GeometryEnergyCandidate],
) -> GeometryEnergyComposite:
    electronic_candidates = [
        candidate for candidate in candidates if candidate.frame.selected_energy_hartree is not None
    ]
    electronic_status, electronic_source, electronic_candidate_ids = _select_candidate(
        electronic_candidates,
        context=lambda candidate: (
            candidate.frame.charge,
            candidate.frame.multiplicity,
            candidate.frame.electronic_state_kind,
            candidate.frame.electronic_state_index,
            candidate.protocol.solvation_model if candidate.protocol is not None else None,
            candidate.protocol.solvent if candidate.protocol is not None else None,
        ),
    )
    thermal_candidates = [
        candidate for candidate in candidates if candidate.thermochemistry is not None
    ]
    if electronic_source is not None:
        electronic_context = (
            electronic_source.frame.charge,
            electronic_source.frame.multiplicity,
            electronic_source.frame.electronic_state_kind,
            electronic_source.frame.electronic_state_index,
            electronic_source.protocol.solvation_model
            if electronic_source.protocol is not None
            else None,
            electronic_source.protocol.solvent if electronic_source.protocol is not None else None,
        )
        thermal_candidates = [
            candidate
            for candidate in thermal_candidates
            if (
                candidate.frame.charge,
                candidate.frame.multiplicity,
                candidate.frame.electronic_state_kind,
                candidate.frame.electronic_state_index,
                candidate.protocol.solvation_model if candidate.protocol is not None else None,
                candidate.protocol.solvent if candidate.protocol is not None else None,
            )
            == electronic_context
        ]
    thermal_status, thermal_source, thermal_candidate_ids = _select_candidate(
        thermal_candidates,
        context=lambda candidate: (
            candidate.frame.charge,
            candidate.frame.multiplicity,
            candidate.frame.electronic_state_kind,
            candidate.frame.electronic_state_index,
            candidate.protocol.solvation_model if candidate.protocol is not None else None,
            candidate.protocol.solvent if candidate.protocol is not None else None,
            candidate.thermochemistry.temperature_kelvin
            if candidate.thermochemistry is not None
            else None,
            candidate.thermochemistry.pressure_atm
            if candidate.thermochemistry is not None
            else None,
        ),
    )
    electronic_energy = (
        electronic_source.frame.selected_energy_hartree if electronic_source is not None else None
    )
    thermochemistry = thermal_source.thermochemistry if thermal_source is not None else None

    def corrected(correction: float | None) -> float | None:
        if electronic_energy is None or correction is None:
            return None
        return round_energy_hartree(electronic_energy + correction)

    view = GeometryEnergyView(
        geometry_id=geometry_id,
        policy_version=GEOMETRY_ENERGY_POLICY_VERSION,
        electronic_selection_status=electronic_status,
        electronic_candidate_frame_ids=electronic_candidate_ids,
        electronic_energy_hartree=electronic_energy,
        electronic_energy_source_frame_id=(
            electronic_source.frame.id if electronic_source is not None else None
        ),
        electronic_energy_protocol_id=(
            electronic_source.protocol.id
            if electronic_source is not None and electronic_source.protocol is not None
            else None
        ),
        charge=(electronic_source.frame.charge if electronic_source is not None else None),
        multiplicity=(
            electronic_source.frame.multiplicity if electronic_source is not None else None
        ),
        electronic_state_kind=(
            str(electronic_source.frame.electronic_state_kind)
            if electronic_source is not None
            else None
        ),
        electronic_state_index=(
            electronic_source.frame.electronic_state_index
            if electronic_source is not None
            else None
        ),
        thermochemistry_selection_status=thermal_status,
        thermochemistry_candidate_frame_ids=thermal_candidate_ids,
        thermochemistry_source_frame_id=(
            thermal_source.frame.id if thermal_source is not None else None
        ),
        thermochemistry_protocol_id=(
            thermal_source.protocol.id
            if thermal_source is not None and thermal_source.protocol is not None
            else None
        ),
        temperature_kelvin=(
            thermochemistry.temperature_kelvin if thermochemistry is not None else None
        ),
        pressure_atm=thermochemistry.pressure_atm if thermochemistry is not None else None,
        zpe_correction_hartree=(
            thermochemistry.zpe_correction_hartree if thermochemistry is not None else None
        ),
        thermal_energy_correction_hartree=(
            thermochemistry.thermal_energy_correction_hartree
            if thermochemistry is not None
            else None
        ),
        thermal_enthalpy_correction_hartree=(
            thermochemistry.thermal_enthalpy_correction_hartree
            if thermochemistry is not None
            else None
        ),
        thermal_gibbs_correction_hartree=(
            thermochemistry.thermal_gibbs_correction_hartree
            if thermochemistry is not None
            else None
        ),
        zero_point_energy_hartree=corrected(
            thermochemistry.zpe_correction_hartree if thermochemistry is not None else None
        ),
        thermal_internal_energy_hartree=corrected(
            thermochemistry.thermal_energy_correction_hartree
            if thermochemistry is not None
            else None
        ),
        enthalpy_hartree=corrected(
            thermochemistry.thermal_enthalpy_correction_hartree
            if thermochemistry is not None
            else None
        ),
        gibbs_free_energy_hartree=corrected(
            thermochemistry.thermal_gibbs_correction_hartree
            if thermochemistry is not None
            else None
        ),
        entropy_cal_mol_k=(
            thermochemistry.entropy_cal_mol_k if thermochemistry is not None else None
        ),
    )
    return GeometryEnergyComposite(
        view=view,
        electronic_level=(
            _protocol_identity(electronic_source.protocol)
            if electronic_source is not None
            else None
        ),
        thermochemistry_level=(
            _protocol_identity(thermal_source.protocol) if thermal_source is not None else None
        ),
    )


def geometry_energy_composites(
    geometry_ids: Iterable[UUID],
    rows: Iterable[
        tuple[CalculationFrame, CalculationProtocol | None, ThermochemistryResult | None]
    ],
) -> dict[UUID, GeometryEnergyComposite]:
    candidates_by_geometry: dict[UUID, list[GeometryEnergyCandidate]] = defaultdict(list)
    for frame, protocol, thermochemistry in rows:
        candidates_by_geometry[frame.geometry_id].append(
            GeometryEnergyCandidate(frame, protocol, thermochemistry)
        )
    return {
        geometry_id: geometry_energy_composite(
            geometry_id,
            candidates_by_geometry.get(geometry_id, []),
        )
        for geometry_id in geometry_ids
    }


__all__ = [
    "GEOMETRY_ENERGY_POLICY_VERSION",
    "GeometryEnergyCandidate",
    "GeometryEnergyComposite",
    "geometry_energy_composite",
    "geometry_energy_composites",
    "protocol_dominates",
    "protocol_level",
]
