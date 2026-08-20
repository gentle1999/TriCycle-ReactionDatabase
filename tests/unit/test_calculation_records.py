from datetime import datetime
from hashlib import sha256

import numpy as np
import pytest
from pydantic import ValidationError

from tricycle_reaction_db.application.dtos import (
    CalculationFrameRecord,
    CalculationSegmentRecord,
    EnergyObservationRecord,
    FrameEnergyResultRecord,
    ParseRevisionCompletionRecord,
    ParseRevisionRecord,
    ScientificArrayRecord,
    ThermochemistryResultRecord,
)
from tricycle_reaction_db.db.types import summarize_numpy_array
from tricycle_reaction_db.domain.enums import (
    EnergyQuantitySemantics,
    FrameRole,
    GeometryAssignmentKind,
    ScientificArrayKind,
    SelectedEnergyKind,
    SourceFormat,
)


def _frame_record(**overrides: object) -> CalculationFrameRecord:
    coordinates = np.asarray(
        [[0.0, 0.0, 0.0], [1.2, 0.1, -0.2]],
        dtype="<f8",
        order="C",
    )
    values: dict[str, object] = {
        "frame_index": 0,
        "file_frame_index": 0,
        "frame_role": FrameRole.INITIAL,
        "source_start_byte": 0,
        "source_end_byte": 100,
        "source_start_char": 0,
        "source_end_char": 100,
        "source_start_line": 1,
        "source_end_line": 10,
        "source_block_sha256": "a" * 64,
        "charge": 0,
        "multiplicity": 1,
        "geometry_assignment_kind": GeometryAssignmentKind.PARSED_EXACT,
        "observed_coordinates": coordinates,
        "observed_coordinate_hash": sha256(coordinates.tobytes(order="C")).hexdigest(),
        "observed_to_geometry_atom_indices": [0, 1],
        "observed_to_geometry_transform": np.eye(4).ravel().tolist(),
        "geometry_assignment_rmsd_angstrom": 0.0,
        "geometry_assignment_max_abs_angstrom": 0.0,
        "geometry_assignment_policy_version": "geometry-internal-coordinate-match-v3",
    }
    values.update(overrides)
    return CalculationFrameRecord.model_validate(values)


def test_segment_source_span_must_be_a_nonempty_half_open_interval() -> None:
    with pytest.raises(ValidationError, match="non-empty half-open interval"):
        CalculationSegmentRecord(
            segment_index=0,
            source_start_byte=10,
            source_end_byte=10,
            source_start_line=1,
            source_end_line=2,
            source_block_sha256="a" * 64,
        )


def test_parse_revision_timestamps_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        ParseRevisionRecord(
            export_schema_version="molop-calculation-v1",
            parser_id="fixture.parser",
            parser_version="v1",
            molop_version="v1",
            rdkit_version="2025.09.6",
            parser_provenance={
                "parser_id": "fixture.parser",
                "parser_version": "v1",
                "molop_version": "v1",
                "rdkit_version": "2025.09.6",
                "effective_config": {},
                "effective_config_sha256": "2" * 64,
            },
            parser_provenance_hash="1" * 64,
            parser_config_hash="2" * 64,
            reconstruction_config_hash="3" * 64,
            source_format=SourceFormat.GAUSSIAN_LOG,
            source_encoding="ascii",
            started_at=datetime.now(),
        )

    with pytest.raises(ValidationError, match="timezone"):
        ParseRevisionCompletionRecord(
            record_sha256="4" * 64,
            completed_at=datetime.now(),
        )


def test_selected_energy_must_match_an_explicit_total_energy_field() -> None:
    with pytest.raises(ValidationError, match="must equal"):
        _frame_record(
            reference_total_energy_hartree=-100.0,
            selected_energy_hartree=-99.0,
            selected_energy_kind=SelectedEnergyKind.REFERENCE_TOTAL,
            energy_selection_policy_version="energy-policy-v1",
        )


def test_scalar_hartree_energies_are_rounded_to_six_decimal_places() -> None:
    frame = _frame_record(
        electronic_total_energy_hartree=-78.123456789,
        selected_energy_hartree=-78.123456789,
        selected_energy_kind=SelectedEnergyKind.ELECTRONIC_TOTAL,
        energy_selection_policy_version="energy-policy-v1",
    )
    energy = FrameEnergyResultRecord(
        electronic_energy_hartree=-78.123456789,
        source_schema_version="molop-calculation-v1",
    )
    observation = EnergyObservationRecord(
        observation_index=0,
        method="SCF",
        quantity_semantics=EnergyQuantitySemantics.TOTAL_ENERGY,
        value_hartree=-78.123456789,
        source_label="SCF Done",
    )
    thermochemistry = ThermochemistryResultRecord(
        temperature_kelvin=298.15,
        pressure_atm=1.0,
        gibbs_free_energy_hartree=-78.50701999999997,
        source_schema_version="molop-calculation-v1",
    )

    assert frame.selected_energy_hartree == -78.123457
    assert energy.electronic_energy_hartree == -78.123457
    assert observation.value_hartree == -78.123457
    assert thermochemistry.gibbs_free_energy_hartree == -78.50702


def test_frequency_summary_is_complete_or_entirely_absent() -> None:
    with pytest.raises(ValidationError, match="requires count and lowest-frequency"):
        _frame_record(frequency_count=3)

    record = _frame_record(
        frequency_count=3,
        negative_frequency_count=1,
        lowest_frequency_cm1=-312.5,
    )
    assert record.negative_frequency_count == 1


def test_calculation_frame_requires_complete_coordinate_evidence() -> None:
    values = _frame_record().model_dump()
    values.pop("observed_coordinates")
    with pytest.raises(ValidationError):
        CalculationFrameRecord.model_validate(values)

    record = _frame_record(
        geometry_assignment_kind=GeometryAssignmentKind.MATCHED_EXISTING_GEOMETRY,
        geometry_assignment_rmsd_angstrom=1e-7,
        geometry_assignment_max_abs_angstrom=2e-7,
    )
    assert record.geometry_assignment_kind is GeometryAssignmentKind.MATCHED_EXISTING_GEOMETRY


def test_scientific_array_record_uses_registry_unit_and_exact_npy_summary() -> None:
    data = np.arange(12, dtype=np.float64).reshape(4, 3)
    summary = summarize_numpy_array(data)
    record = ScientificArrayRecord(
        kind=ScientificArrayKind.FORCES,
        ordinal=0,
        unit="hartree/bohr",
        dtype=summary.dtype,
        shape=list(summary.shape),
        array_nbytes=summary.nbytes,
        payload_sha256=summary.sha256,
        data=data,
    )

    assert record.payload_sha256 == summary.sha256

    with pytest.raises(ValidationError, match="must use unit"):
        ScientificArrayRecord.model_validate(record.model_dump() | {"unit": "angstrom"})


def test_thermochemistry_result_requires_at_least_one_scientific_value() -> None:
    with pytest.raises(ValidationError, match="at least one result value"):
        ThermochemistryResultRecord(
            temperature_kelvin=298.15,
            pressure_atm=1.0,
            source_schema_version="molop-calculation-v1",
        )
