"""Admission policy for Geometry links owned by reaction paths."""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_
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


def geometry_has_thermodynamic_property(session: Session, geometry: Geometry) -> bool:
    geometry_id = _require_id(geometry, label="Geometry")
    return bool(
        session.exec(select(geometry_has_thermodynamic_property_predicate(geometry_id))).one()
    )


def require_geometry_thermodynamic_property(session: Session, geometry: Geometry) -> None:
    if not geometry_has_thermodynamic_property(session, geometry):
        raise ValueError("reaction-linked Geometry requires at least one thermodynamic property")


__all__ = [
    "geometry_has_thermodynamic_property",
    "geometry_has_thermodynamic_property_predicate",
    "require_geometry_thermodynamic_property",
]
