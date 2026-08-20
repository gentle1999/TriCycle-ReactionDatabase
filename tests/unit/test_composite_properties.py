from uuid import uuid4

import pytest

from tricycle_reaction_db.application.services.geometry_energy import (
    GeometryEnergyCandidate,
    geometry_energy_composite,
)
from tricycle_reaction_db.application.services.queries import (
    _aggregate_primary_coordinates,
    _complete_sum,
)
from tricycle_reaction_db.db.models import (
    CalculationFrame,
    CalculationProtocol,
    ThermochemistryResult,
)


def _protocol(
    functional: str,
    basis_set: str,
    *,
    software: str,
) -> CalculationProtocol:
    return CalculationProtocol(
        id=uuid4(),
        protocol_hash=uuid4().hex + uuid4().hex,
        qm_software=software,
        qm_software_version="test",
        method_family="DFT",
        method="DFT",
        functional=functional,
        basis_set=basis_set,
    )


def _frame(geometry_id: object, energy: float) -> CalculationFrame:
    return CalculationFrame(
        id=uuid4(),
        geometry_id=geometry_id,
        selected_energy_hartree=energy,
        charge=0,
        multiplicity=1,
        electronic_state_kind="ground",
        electronic_state_index=0,
    )


def test_geometry_energy_prefers_higher_level_single_point_and_adds_thermal_correction() -> None:
    geometry_id = uuid4()
    gaussian_protocol = _protocol("B3LYP-GD3BJ", "def2-SVP", software="gaussian")
    orca_protocol = _protocol("wB97M-V", "def2-TZVPP", software="orca")
    gaussian_frame = _frame(geometry_id, -99.0)
    orca_frame = _frame(geometry_id, -100.0)
    thermochemistry = ThermochemistryResult(
        temperature_kelvin=298.15,
        pressure_atm=1.0,
        zpe_correction_hartree=0.08,
        thermal_energy_correction_hartree=0.07,
        thermal_enthalpy_correction_hartree=0.06,
        thermal_gibbs_correction_hartree=0.05,
        entropy_cal_mol_k=12.5,
    )

    composite = geometry_energy_composite(
        geometry_id,
        [
            GeometryEnergyCandidate(gaussian_frame, gaussian_protocol, thermochemistry),
            GeometryEnergyCandidate(orca_frame, orca_protocol, None),
        ],
    )

    assert composite.view.electronic_selection_status == "selected"
    assert composite.view.electronic_energy_hartree == -100.0
    assert composite.view.electronic_energy_source_frame_id == orca_frame.id
    assert composite.view.thermochemistry_source_frame_id == gaussian_frame.id
    assert composite.view.gibbs_free_energy_hartree == pytest.approx(-99.95)
    assert composite.view.enthalpy_hartree == pytest.approx(-99.94)
    assert composite.view.entropy_cal_mol_k == pytest.approx(12.5)

    aggregate = _aggregate_primary_coordinates([composite, composite])
    assert aggregate is not None
    assert aggregate.component_count == 2
    assert aggregate.source_levels_compatible is True
    assert aggregate.electronic_energy_hartree == -200.0
    assert aggregate.gibbs_free_energy_hartree == pytest.approx(-199.9)


def test_incomparable_protocols_produce_an_ambiguous_energy_view() -> None:
    geometry_id = uuid4()
    larger_basis = _protocol("B3LYP", "def2-TZVPP", software="gaussian")
    better_functional = _protocol("wB97M-V", "def2-SVP", software="orca")
    first_frame = _frame(geometry_id, -99.0)
    second_frame = _frame(geometry_id, -100.0)

    composite = geometry_energy_composite(
        geometry_id,
        [
            GeometryEnergyCandidate(first_frame, larger_basis, None),
            GeometryEnergyCandidate(second_frame, better_functional, None),
        ],
    )

    assert composite.view.electronic_selection_status == "ambiguous"
    assert composite.view.electronic_energy_hartree is None
    assert set(composite.view.electronic_candidate_frame_ids) == {
        first_frame.id,
        second_frame.id,
    }


def test_derived_energies_are_quantized_after_float_arithmetic() -> None:
    assert _complete_sum([-78.123457, -0.000001]) == -78.123458
