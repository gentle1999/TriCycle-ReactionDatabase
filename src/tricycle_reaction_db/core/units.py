"""Pint-backed unit boundaries shared by ingestion and scientific services.

MolOP creates its public quantities with ``molop.unit.atom_ureg``.  Reusing
that registry is important: Pint quantities from different registries cannot
be combined or converted directly, even when their unit names are identical.

The database stores normalized scientific values as plain scalars for query
and indexing efficiency.  Callers must therefore convert through this module
at the boundary and only then drop ``.magnitude``.  The one SQL-only factor is
derived from Pint at import time because PostgreSQL generated columns cannot
execute Python/Pint code.
"""

from functools import lru_cache
from typing import Any, Final

from molop.unit import atom_ureg

UNIT_REGISTRY: Final[Any] = atom_ureg


@lru_cache(maxsize=64)
def unit(name: str) -> Any:
    """Return one cached Pint unit from the shared MolOP registry."""

    return UNIT_REGISTRY.parse_units(name)


ANGSTROM: Final[Any] = unit("angstrom")
BOHR: Final[Any] = unit("bohr")
CALORIE_PER_MOLE_KELVIN: Final[Any] = unit("calorie / mole / kelvin")
CM_INVERSE: Final[Any] = unit("1 / centimeter")
DEGREE: Final[Any] = unit("degree")
DEBYE: Final[Any] = unit("debye")
DEBYE_ANGSTROM: Final[Any] = unit("debye * angstrom")
DEBYE_ANGSTROM_SQUARED: Final[Any] = unit("debye * angstrom ** 2")
DEBYE_ANGSTROM_CUBED: Final[Any] = unit("debye * angstrom ** 3")
DIMENSIONLESS: Final[Any] = unit("dimensionless")
ELECTRONVOLT: Final[Any] = unit("eV")
ELECTRONVOLT_PER_PARTICLE: Final[Any] = unit("eV / particle")
GIGAHERTZ: Final[Any] = unit("gigahertz")
HARTREE: Final[Any] = unit("hartree")
HARTREE_PER_BOHR: Final[Any] = unit("hartree / bohr")
HARTREE_PER_BOHR_SQUARED: Final[Any] = unit("hartree / bohr ** 2")
HARTREE_PER_PARTICLE: Final[Any] = unit("hartree / particle")
HERTZ: Final[Any] = unit("hertz")
KELVIN: Final[Any] = unit("kelvin")
KILOCALORIE_PER_MOLE: Final[Any] = unit("kilocalorie / mole")
KILOMETER_PER_MOLE: Final[Any] = unit("kilometer / mole")
MILLIDYNE_PER_ANGSTROM: Final[Any] = unit("millidyne / angstrom")
PPM: Final[Any] = unit("ppm")
RADIAN: Final[Any] = unit("radian")
SECOND: Final[Any] = unit("second")
STANDARD_ATMOSPHERE: Final[Any] = unit("standard_atmosphere")
UNIFIED_ATOMIC_MASS_UNIT: Final[Any] = unit("unified_atomic_mass_unit")
UNIFIED_ATOMIC_MASS_UNIT_BOHR_SQUARED: Final[Any] = unit("unified_atomic_mass_unit * bohr ** 2")


def magnitude_in(value: Any, target_unit: Any) -> Any:
    """Convert a Pint quantity and return its magnitude in ``target_unit``."""

    if value is None:
        return None
    # Pass the target by name so a quantity created by MolOP's other Pint
    # registry (``si_ureg``) is not combined with a Unit object from
    # ``atom_ureg``. Pint resolves the name in the quantity's own registry.
    target_name = target_unit if isinstance(target_unit, str) else str(target_unit)
    return value.to(target_name).magnitude


def convert_magnitude(value: Any, source_unit: Any, target_unit: Any) -> Any:
    """Attach ``source_unit``, convert with Pint, and return the magnitude."""

    return magnitude_in(value * source_unit, target_unit)


def radians_to_degrees(value: Any) -> Any:
    """Convert radians to degrees through the shared Pint registry."""

    return convert_magnitude(value, RADIAN, DEGREE)


def degrees_to_radians(value: Any) -> Any:
    """Convert degrees to radians through the shared Pint registry."""

    return convert_magnitude(value, DEGREE, RADIAN)


def hartree_per_particle_to_kcal_per_mol(value: float) -> float:
    """Convert an energy scalar stored as Hartree/particle to kcal/mol."""

    return float(convert_magnitude(value, HARTREE_PER_PARTICLE, KILOCALORIE_PER_MOLE))


HARTREE_PER_PARTICLE_TO_KCAL_PER_MOLE_FACTOR: Final[float] = hartree_per_particle_to_kcal_per_mol(
    1.0
)


__all__ = [
    "ANGSTROM",
    "BOHR",
    "CALORIE_PER_MOLE_KELVIN",
    "CM_INVERSE",
    "DEGREE",
    "DEBYE",
    "DEBYE_ANGSTROM",
    "DEBYE_ANGSTROM_SQUARED",
    "DEBYE_ANGSTROM_CUBED",
    "DIMENSIONLESS",
    "ELECTRONVOLT",
    "ELECTRONVOLT_PER_PARTICLE",
    "GIGAHERTZ",
    "HARTREE",
    "HARTREE_PER_BOHR",
    "HARTREE_PER_BOHR_SQUARED",
    "HARTREE_PER_PARTICLE",
    "HARTREE_PER_PARTICLE_TO_KCAL_PER_MOLE_FACTOR",
    "HERTZ",
    "KELVIN",
    "KILOCALORIE_PER_MOLE",
    "KILOMETER_PER_MOLE",
    "MILLIDYNE_PER_ANGSTROM",
    "PPM",
    "RADIAN",
    "SECOND",
    "STANDARD_ATMOSPHERE",
    "UNIFIED_ATOMIC_MASS_UNIT",
    "UNIFIED_ATOMIC_MASS_UNIT_BOHR_SQUARED",
    "UNIT_REGISTRY",
    "convert_magnitude",
    "degrees_to_radians",
    "hartree_per_particle_to_kcal_per_mol",
    "magnitude_in",
    "radians_to_degrees",
    "unit",
]
