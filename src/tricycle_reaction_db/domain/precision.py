"""Shared numerical precision contracts for persisted scientific scalars."""

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Final

from pydantic import BeforeValidator

ENERGY_HARTREE_DECIMAL_PLACES: Final = 6
ENERGY_HARTREE_QUANTUM: Final = Decimal("0.000001")


def round_energy_hartree(value: Any) -> float:
    """Normalize one scalar Hartree energy to the supported database precision."""

    return float(
        Decimal(str(value)).quantize(
            ENERGY_HARTREE_QUANTUM,
            rounding=ROUND_HALF_UP,
        )
    )


EnergyHartree = Annotated[float, BeforeValidator(round_energy_hartree)]

__all__ = [
    "ENERGY_HARTREE_DECIMAL_PLACES",
    "ENERGY_HARTREE_QUANTUM",
    "EnergyHartree",
    "round_energy_hartree",
]
