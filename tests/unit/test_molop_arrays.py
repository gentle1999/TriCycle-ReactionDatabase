from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from molop import AutoParser, molopconfig

from tricycle_reaction_db.db.types import summarize_numpy_array
from tricycle_reaction_db.domain.enums import ScientificArrayKind
from tricycle_reaction_db.ingestion import (
    scientific_array_export_from_molop_frame,
    scientific_array_records_from_molop_frame,
)
from tricycle_reaction_db.ingestion.molop_arrays import _rotational_constant_difference


class _FrameWithPartialThermochemistry:
    def model_dump(self, *, mode: str, exclude_none: bool) -> dict[str, Any]:
        del mode, exclude_none
        return {
            "thermal_informations": {
                "G_T": 0.0,
                "rotational_temperatures": None,
                # MolOP may omit this optional field instead of serializing None.
            },
            "forces": None,
            "hessian": None,
            "rotation_constants": None,
            "vibrations": None,
            "molecular_orbitals": None,
            "charge_spin_populations": None,
            "polarizability": None,
            "nmr": None,
            "bond_orders": None,
            "single_point_properties": None,
            "electronic_states": None,
            "multireference_result": None,
        }


def test_partial_thermal_information_without_moments_of_inertia_is_accepted() -> None:
    records = scientific_array_records_from_molop_frame(_FrameWithPartialThermochemistry())

    assert records == []


def test_rotational_constant_difference_treats_matching_infinities_as_zero() -> None:
    frame = np.array([np.inf, 11.0613252, 11.0613198])
    thermal = np.array([np.inf, 11.06133, 11.06132])

    assert _rotational_constant_difference(frame, thermal) == pytest.approx(4.8e-6)


def test_rotational_constant_difference_returns_zero_for_only_matching_infinities() -> None:
    frame = np.array([np.inf, -np.inf])
    thermal = np.array([np.inf, -np.inf])

    assert _rotational_constant_difference(frame, thermal) == 0.0


def test_rotational_constant_difference_rejects_nan() -> None:
    frame = np.array([np.nan, 11.0, 10.0])
    thermal = np.array([np.nan, 11.0, 10.0])

    with pytest.raises(ValueError, match="contain NaN"):
        _rotational_constant_difference(frame, thermal)


def test_rotational_constant_difference_rejects_infinite_mismatch() -> None:
    frame = np.array([np.inf, 11.0, 10.0])
    thermal = np.array([-np.inf, 11.0, 10.0])

    with pytest.raises(ValueError, match="sources disagree"):
        _rotational_constant_difference(frame, thermal)


def test_da_bench_exports_every_supported_molop_array(
    da_bench_log_paths: dict[str, Path],
    da_bench_manifest: dict[str, Any],
) -> None:
    molopconfig.show_progress_bar = False
    records = [
        record
        for path in da_bench_log_paths.values()
        for frame in AutoParser(str(path), n_jobs=1)[0]
        for record in scientific_array_records_from_molop_frame(frame)
    ]

    expected_counts = {
        ScientificArrayKind(kind): count
        for kind, count in da_bench_manifest["expected_array_counts"].items()
    }
    assert Counter(record.kind for record in records) == expected_counts
    assert len(records) == sum(expected_counts.values())
    for record in records:
        summary = summarize_numpy_array(record.data)
        assert record.dtype == summary.dtype == "float64"
        assert record.shape == list(summary.shape)
        assert record.array_nbytes == summary.nbytes
        assert record.payload_sha256 == summary.sha256
        assert record.data.flags.c_contiguous
        assert record.data.dtype == np.dtype("<f8")
        assert np.isfinite(record.data).all()
        assert record.array_metadata is not None
        assert record.array_metadata["unit"] == record.unit


def test_ts_vibrational_temperatures_map_only_positive_frequency_modes(
    da_bench_log_paths: dict[str, Path],
    da_bench_manifest: dict[str, Any],
) -> None:
    molopconfig.show_progress_bar = False
    ts_entry = next(
        entry for entry in da_bench_manifest["logs"] if entry["role"] == "transition_state"
    )
    frame = AutoParser(str(da_bench_log_paths["transition_state"]), n_jobs=1)[0][
        ts_entry["reaction_frame_file_index"]
    ]
    records = scientific_array_records_from_molop_frame(frame)
    temperatures = next(
        record for record in records if record.kind is ScientificArrayKind.VIBRATIONAL_TEMPERATURES
    )

    positive_mode_count = ts_entry["positive_frequency_mode_count"]
    assert temperatures.shape == [positive_mode_count]
    assert temperatures.array_metadata is not None
    assert temperatures.array_metadata["frequency_mode_indices"] == list(
        range(1, positive_mode_count + 1)
    )


def test_atomic_population_array_keeps_molop_name_and_normalized_unit() -> None:
    class _Frame:
        def model_dump(self, *, mode: str, exclude_none: bool) -> dict[str, Any]:
            del mode, exclude_none
            return {}

    payload: dict[str, Any] = {
        "thermal_informations": None,
        "forces": None,
        "hessian": None,
        "rotation_constants": None,
        "vibrations": None,
        "molecular_orbitals": None,
        "charge_spin_populations": {
            "populations": {
                "npa_charges": {
                    "scheme": "npa",
                    "quantity": "charge",
                    "values": [0.1, -0.1],
                    "spin_channel": None,
                    "source_label": "Natural Population Analysis",
                }
            }
        },
        "polarizability": None,
        "nmr": None,
        "bond_orders": None,
        "single_point_properties": None,
        "electronic_states": None,
        "multireference_result": None,
    }

    records, assignments = scientific_array_export_from_molop_frame(
        _Frame(), frame_payload=payload
    )

    assert len(records) == len(assignments) == 1
    record = records[0]
    assert record.kind is ScientificArrayKind.ATOMIC_POPULATION
    assert record.unit == "dimensionless"
    assert record.array_metadata is not None
    assert record.array_metadata["unit"] == "dimensionless"
    assert record.array_metadata["population_name"] == "npa_charges"
    assert record.array_metadata["population_source_label"] == "Natural Population Analysis"
    assert assignments[0].owner_key == "npa_charges"
