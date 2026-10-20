"""Release-contract regressions using real MolOP result containers."""

from io import BytesIO, StringIO
from pathlib import Path
from runpy import run_path
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from molop import AutoParser
from molop.io.base_models.DataClasses import (
    BondOrderMatrix,
    BondOrders,
    ElectronicState,
    ElectronicStates,
    MolecularOrbitals,
    ThermalInformations,
    Vibrations,
)
from molop.unit import atom_ureg

from tricycle_reaction_db.application.dtos import calculations as records
from tricycle_reaction_db.application.dtos.query_views import VibrationView
from tricycle_reaction_db.application.services.advanced_queries import _array_view
from tricycle_reaction_db.application.services.scientific_array_content import _json_value
from tricycle_reaction_db.db.models import calculations as models
from tricycle_reaction_db.db.types import cached_numpy_array_payload
from tricycle_reaction_db.domain.enums import ScientificArrayKind
from tricycle_reaction_db.ingestion.molop_arrays import (
    _float_array,
    scientific_array_export_from_molop_frame,
)
from tricycle_reaction_db.ingestion.molop_calculations import (
    _bond_order_record,
    _calculation_timings,
    _electronic_state_records,
    _molecular_orbital_record,
    _thermochemistry_record,
    _vibration_record,
    parse_revision_record_from_molop,
)

SCHEMA = "molop-calculation-export-v1"


def _payload(**overrides: Any) -> dict[str, Any]:
    return (
        dict.fromkeys(
            (
                "thermal_informations",
                "forces",
                "hessian",
                "rotation_constants",
                "vibrations",
                "molecular_orbitals",
                "charge_spin_populations",
                "polarizability",
                "nmr",
                "bond_orders",
                "single_point_properties",
                "electronic_states",
                "multireference_result",
            )
        )
        | overrides
    )


def _export(payload: dict[str, Any]):
    return scientific_array_export_from_molop_frame(None, frame_payload=payload)


def test_dynamic_bond_order_records_preserve_unknown_zero_negative_and_provenance() -> None:
    values = np.array([[np.nan, 0.0, -0.2], [0.0, np.nan, 0.7], [-0.2, 0.7, np.nan]])
    bond_orders = BondOrders(
        bond_orders={
            "custom_alpha_analysis": BondOrderMatrix(
                scheme="custom",
                spin_channel="alpha",
                values=values,
                source_label="Printed atom-pair table",
                print_threshold=0.05,
                missing_reason="not_printed",
                metadata={"program": "fixture"},
            )
        }
    ).model_dump(mode="python")
    result = _bond_order_record(bond_orders, SCHEMA)
    assert result is not None and result.matrix_count == 1
    arrays, assignments = _export(_payload(bond_orders=bond_orders))
    assert len(arrays) == len(assignments) == 1
    record = arrays[0]
    assert record.kind is ScientificArrayKind.BOND_ORDER_MATRIX
    assert assignments[0].slot == "custom_alpha_analysis"
    np.testing.assert_equal(record.data, values)
    np.testing.assert_equal(np.load(BytesIO(cached_numpy_array_payload(record.data))), values)
    assert record.array_metadata["missing_value_count"] == 3
    assert record.array_metadata["missing_reason"] == "not_printed"
    assert record.array_metadata["print_threshold"] == 0.05
    assert record.array_metadata["spin_channel"] == "alpha"
    assert _json_value(record.data[0, 0]) is None
    assert _json_value(record.data[0, 1]) == 0.0
    assert _json_value(record.data[0, 2]) == -0.2
    row = models.ScientificArray(id=uuid4(), frame_id=uuid4(), **record.model_dump())
    view = _array_view(row, None)
    assert '"scheme": "custom"' in view.metadata_json
    assert view.metadata_schema_version == "molop-array-source-v2"


def test_partial_spin_channels_preserve_alignment_and_missing_occupations() -> None:
    orbitals = MolecularOrbitals(
        alpha_energies=np.array([-0.7, -0.3, 0.1]) * atom_ureg.hartree,
        beta_energies=np.array([-0.6]) * atom_ureg.hartree,
        alpha_occupancies=[1, 1, 0],
        beta_occupancies=[1],
        orbital_basis="spin_resolved",
        occupancy_origin="observed",
    ).model_dump(mode="python")
    record = _molecular_orbital_record(orbitals, SCHEMA)
    assert record is not None
    assert record.beta_orbital_count == record.alpha_orbital_count == 3
    assert record.beta_occupancies == [1.0, None, None]
    assert record.orbital_basis == "spin_resolved"
    arrays, _ = _export(_payload(molecular_orbitals=orbitals))
    beta = next(a for a in arrays if a.kind is ScientificArrayKind.ORBITAL_BETA_ENERGIES)
    np.testing.assert_equal(beta.data, [-0.6, np.nan, np.nan])


def test_shared_orbitals_retain_total_occupation_and_reported_frontier_identity() -> None:
    orbitals = MolecularOrbitals.from_spatial_orbitals(
        np.array([-0.7, -0.3, 0.1]) * atom_ureg.hartree,
        [2.0, 0.5, 0.0],
        reported_homo_index=1,
        reported_lumo_index=2,
    )
    record = _molecular_orbital_record(orbitals.model_dump(mode="python"), SCHEMA)
    assert record is not None
    assert record.total_occupancies == [2.0, 0.5, 0.0]
    assert record.alpha_occupancies == [1.0, None, 0.0]
    assert record.orbital_basis == "shared_spatial"
    assert record.occupancy_origin == "unknown"
    assert (record.reported_homo_index, record.reported_lumo_index) == (1, 2)


def test_raman_arrays_preserve_mode_order_source_indices_and_units() -> None:
    vibrations = Vibrations(
        frequencies=np.array([100, 200]) * atom_ureg.cm_1,
        mode_indices=[4, 1],
        source_mode_indices=[7, None],
        raman_activities=np.array([3, 4]) * atom_ureg.angstrom**4 / atom_ureg.amu,
        raman_depolarization_ratios_polarized=np.array([0.1, 0.2]) * atom_ureg.dimensionless,
        source_program="ORCA",
        frequency_method="Hessian",
        raman_activity_method="polarizability",
        orientation="input",
    )
    payload = vibrations.model_dump(mode="python")
    record = _vibration_record(payload, SCHEMA)
    assert record is not None
    assert record.mode_indices == [4, 1]
    assert record.source_mode_indices == [7, None]
    row = models.VibrationResult(frame_id=uuid4(), **record.model_dump())
    assert VibrationView.model_validate(row, from_attributes=True).orientation == "input"
    arrays, _ = _export(_payload(vibrations=payload))
    raman = next(a for a in arrays if a.kind is ScientificArrayKind.RAMAN_ACTIVITIES)
    assert raman.unit == "angstrom^4/amu"
    assert raman.array_metadata["source_mode_indices"] == [7, None]
    np.testing.assert_equal(raman.data, [3, 4])


def test_empty_vibrations_keep_an_empty_summary() -> None:
    record = _vibration_record(Vibrations().model_dump(mode="python"), SCHEMA)
    assert record is not None
    assert record.mode_count == 0
    assert record.mode_indices == []
    assert record.lowest_frequency_cm1 is None


def test_partial_thermal_output_retains_conditions_and_derivations_without_inventing_values() -> (
    None
):
    thermal = ThermalInformations(
        source_program="ORCA",
        entropy_model="QRRHO",
        temperature=310 * atom_ureg.K,
        G_T=-5 * atom_ureg.kcal / atom_ureg.mol,
        frequency_scale_factor=0.98,
        rotor_reference_frequency=100 * atom_ureg.cm_1,
        derivations={"TCG": "G_T minus electronic energy"},
    ).model_dump(mode="python")
    record = _thermochemistry_record(_payload(thermal_informations=thermal), SCHEMA)
    assert record is not None
    assert record.temperature_kelvin == 310
    assert record.pressure_atm is None
    assert record.zpe_correction_hartree is None
    assert record.entropy_model == "QRRHO"
    assert record.frequency_scale_factor == 0.98
    assert record.rotor_reference_frequency_cm1 == 100
    assert record.derivations == {"TCG": "G_T minus electronic energy"}
    with pytest.raises(ValueError, match="temperature disagrees"):
        _thermochemistry_record(
            _payload(thermal_informations=thermal, temperature=298 * atom_ureg.K), SCHEMA
        )


def test_electronic_ground_state_identity_is_explicit_and_can_be_unknown() -> None:
    states = ElectronicStates(
        states=[
            ElectronicState(state_index=4, is_ground_state=False),
            ElectronicState(state_index=7, is_ground_state=True),
            ElectronicState(state_index=2),
        ]
    )
    _, records_, _ = _electronic_state_records(
        _payload(electronic_states=states.model_dump(mode="python")), SCHEMA
    )
    assert [state.is_ground_state for state in records_] == [False, True, None]
    assert [state.state_index for state in records_] == [4, 7, 2]


def test_time_alias_uses_only_wall_clock_and_keeps_estimation_provenance() -> None:
    timings = _calculation_timings(
        {
            "running_time": 999 * atom_ureg.second,
            "wall_time": 10 * atom_ureg.second,
            "cpu_time": 40 * atom_ureg.second,
            "cpu_time_source": "estimated",
            "cpu_time_num_processes": 4,
        }
    )
    assert timings == {
        "running_time_seconds": 10,
        "wall_time_seconds": 10,
        "cpu_time_seconds": 40,
        "cpu_time_source": "estimated",
        "cpu_time_num_processes": 4,
    }
    assert (
        _calculation_timings({"running_time": 999 * atom_ureg.second})["wall_time_seconds"] is None
    )


def test_gaussian_file_clocks_are_not_combined(da_bench_log_paths: dict[str, Path]) -> None:
    file = AutoParser(str(da_bench_log_paths["product"]), n_jobs=1, capture_source_evidence=True)[0]
    record = parse_revision_record_from_molop(file, started_at=None)
    assert record.wall_time_seconds == pytest.approx(167.8)
    assert record.running_time_seconds == record.wall_time_seconds
    assert record.cpu_time_seconds == pytest.approx(4962.8)
    assert record.cpu_time_source == "observed"
    assert (
        record.parser_provenance["tricycle_ingestion_mapping_version"]
        == "molop-calculation-mapping-v3"
    )
    # A multi-segment file can estimate CPU using different process counts.
    payload = record.model_dump() | {
        "cpu_time_source": "estimated",
        "cpu_time_num_processes": None,
    }
    assert records.ParseRevisionRecord.model_validate(payload).cpu_time_num_processes is None


def test_masked_array_entries_are_not_persisted_as_hidden_backing_values() -> None:
    source = np.ma.array([7.0, 99.0, -2.0], mask=[False, True, False])
    np.testing.assert_equal(_float_array(source), [7.0, np.nan, -2.0])
    assert source.data[1] == 99.0


@pytest.mark.parametrize(
    "record_name, model_name",
    [
        ("ParseRevisionRecord", "ParseRevision"),
        ("CalculationFrameRecord", "CalculationFrame"),
        ("VibrationResultRecord", "VibrationResult"),
        ("MolecularOrbitalResultRecord", "MolecularOrbitalResult"),
        ("ThermochemistryResultRecord", "ThermochemistryResult"),
        ("ElectronicStateRecord", "ElectronicState"),
    ],
)
def test_result_contract_fields_have_persistent_columns(record_name: str, model_name: str) -> None:
    record_type = getattr(records, record_name)
    model_type = getattr(models, model_name)
    assert set(record_type.model_fields) - {"set_kind"} <= set(model_type.model_fields)


def test_migration_is_additive_and_does_not_relabel_legacy_values() -> None:
    migration = run_path(
        str(Path(__file__).parents[2] / "migrations/versions/0063_molop_result_semantics.py")
    )
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        migration["upgrade"]()
    sql = output.getvalue()
    assert "ADD COLUMN cpu_time_seconds FLOAT" in sql
    assert "ADD COLUMN is_ground_state BOOLEAN" in sql
    assert "raman_activities" in sql
    assert "ALTER COLUMN temperature_kelvin DROP NOT NULL" in sql
    assert "UPDATE " not in sql
    assert "DELETE " not in sql
    assert "DROP COLUMN" not in sql
