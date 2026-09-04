import numpy as np
import pytest
from molop.unit import si_ureg

from tricycle_reaction_db.core.units import (
    HARTREE_PER_PARTICLE,
    HARTREE_PER_PARTICLE_TO_KCAL_PER_MOLE_FACTOR,
    KILOCALORIE_PER_MOLE,
    degrees_to_radians,
    hartree_per_particle_to_kcal_per_mol,
    magnitude_in,
    radians_to_degrees,
)


def test_energy_conversion_is_derived_by_pint() -> None:
    assert pytest.approx(627.5094740628942) == HARTREE_PER_PARTICLE_TO_KCAL_PER_MOLE_FACTOR
    assert hartree_per_particle_to_kcal_per_mol(1.0) == pytest.approx(
        HARTREE_PER_PARTICLE_TO_KCAL_PER_MOLE_FACTOR
    )
    assert magnitude_in(1 * HARTREE_PER_PARTICLE, KILOCALORIE_PER_MOLE) == pytest.approx(
        627.5094740628942
    )


def test_conversion_accepts_quantities_from_molops_other_registry() -> None:
    value = 1 * si_ureg.hartree / si_ureg.particle

    assert magnitude_in(value, KILOCALORIE_PER_MOLE) == pytest.approx(627.5094740628942)


def test_angle_conversions_support_numpy_arrays() -> None:
    radians = np.array([0.0, np.pi / 2, np.pi])

    assert np.allclose(radians_to_degrees(radians), [0.0, 90.0, 180.0])
    assert degrees_to_radians(180.0) == pytest.approx(np.pi)
