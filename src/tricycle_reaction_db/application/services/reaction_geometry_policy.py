"""Admission policy for Geometry links owned by reaction paths."""

from __future__ import annotations

from typing import Any

from sqlalchemy import not_, or_
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.services._persistence import _require_id
from tricycle_reaction_db.db.models import CalculationFrame, Geometry, ThermochemistryResult

_THERMODYNAMIC_PROPERTY_COLUMNS = (
    ThermochemistryResult.zpe_correction_hartree,
    ThermochemistryResult.thermal_energy_correction_hartree,
    ThermochemistryResult.thermal_enthalpy_correction_hartree,
    ThermochemistryResult.thermal_gibbs_correction_hartree,
    ThermochemistryResult.zero_point_energy_hartree,
    ThermochemistryResult.thermal_internal_energy_hartree,
    ThermochemistryResult.enthalpy_hartree,
    ThermochemistryResult.gibbs_free_energy_hartree,
    ThermochemistryResult.entropy_cal_mol_k,
    ThermochemistryResult.heat_capacity_cv_cal_mol_k,
)


def geometry_has_thermodynamic_property_predicate(geometry_id: Any) -> Any:
    """Return a correlated predicate for at least one real thermodynamic scalar."""

    return (
        select(col(ThermochemistryResult.id))
        .join(
            CalculationFrame,
            col(ThermochemistryResult.frame_id) == col(CalculationFrame.id),
        )
        .where(
            col(CalculationFrame.geometry_id) == geometry_id,
            or_(*(col(column).is_not(None) for column in _THERMODYNAMIC_PROPERTY_COLUMNS)),
        )
        .exists()
    )


def geometry_ids_with_thermodynamic_property(frame_visibility: Any) -> Any:
    """Select geometry IDs whose visible frame exposes a thermodynamic scalar."""

    return (
        select(col(CalculationFrame.geometry_id))
        .join(
            ThermochemistryResult,
            col(ThermochemistryResult.frame_id) == col(CalculationFrame.id),
        )
        .where(
            frame_visibility,
            or_(*(col(column).is_not(None) for column in _THERMODYNAMIC_PROPERTY_COLUMNS)),
        )
    )


def geometry_has_thermodynamic_property(session: Session, geometry: Geometry) -> bool:
    geometry_id = _require_id(geometry, label="Geometry")
    return bool(
        session.exec(select(geometry_has_thermodynamic_property_predicate(geometry_id))).one()
    )


def geometry_has_no_imaginary_frequency_predicate(geometry_id: Any) -> Any:
    """Return a predicate excluding Geometry rows with any negative mode."""

    has_imaginary_frequency = (
        select(col(CalculationFrame.id))
        .where(
            col(CalculationFrame.geometry_id) == geometry_id,
            col(CalculationFrame.negative_frequency_count) > 0,
        )
        .exists()
    )
    return not_(has_imaginary_frequency)


def geometry_has_no_imaginary_frequency(session: Session, geometry: Geometry) -> bool:
    geometry_id = _require_id(geometry, label="Geometry")
    return bool(
        session.exec(select(geometry_has_no_imaginary_frequency_predicate(geometry_id))).one()
    )


def require_geometry_reaction_endpoint_eligibility(
    session: Session,
    geometry: Geometry,
) -> None:
    """Require thermodynamic evidence and no imaginary mode for endpoints."""

    if not geometry_has_thermodynamic_property(session, geometry):
        raise ValueError("reaction-linked Geometry requires at least one thermodynamic property")
    if not geometry_has_no_imaginary_frequency(session, geometry):
        raise ValueError("reaction endpoint Geometry cannot contain an imaginary frequency")


def require_geometry_thermodynamic_property(session: Session, geometry: Geometry) -> None:
    if not geometry_has_thermodynamic_property(session, geometry):
        raise ValueError("reaction-linked Geometry requires at least one thermodynamic property")


__all__ = [
    "geometry_ids_with_thermodynamic_property",
    "geometry_has_thermodynamic_property",
    "geometry_has_thermodynamic_property_predicate",
    "geometry_has_no_imaginary_frequency",
    "geometry_has_no_imaginary_frequency_predicate",
    "require_geometry_reaction_endpoint_eligibility",
    "require_geometry_thermodynamic_property",
]
