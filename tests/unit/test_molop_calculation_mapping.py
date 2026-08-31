from __future__ import annotations

import gzip
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest
from molop import AutoParser, molopconfig  # type: ignore[import-untyped]

from tricycle_reaction_db.ingestion import (
    frame_records_from_molop,
    parse_revision_record_from_molop,
    protocol_record_from_molop_segment,
    segment_record_from_molop,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "da_bench_minimal"


def _parse_fixture(path: Path, output_root: Path):  # type: ignore[no-untyped-def]
    output = output_root / path.name.removesuffix(".gz")
    output.write_bytes(gzip.decompress(path.read_bytes()))
    return AutoParser(
        output,
        capture_source_evidence=True,
        release_file_content=True,
    )[0]


def test_latest_molop_models_map_complete_da_fixture(
    tmp_path: Path,
    da_bench_manifest: dict[str, Any],
) -> None:
    molopconfig.show_progress_bar = False
    installed_molop_version = version("molop")
    installed_molgr_version = version("molgr")
    totals = {
        "frames": 0,
        "segments": 0,
        "arrays": 0,
        "thermochemistry": 0,
        "energies": 0,
        "energy_observations": 0,
        "vibrations": 0,
        "optimizations": 0,
        "statuses": 0,
        "molecular_orbitals": 0,
        "population_results": 0,
        "population_series": 0,
        "polarizabilities": 0,
        "array_assignments": 0,
        "running_times": 0,
    }
    file_runtime_differences: list[tuple[str, float, float]] = []

    for source_path in sorted(FIXTURE_ROOT.rglob("*.log.gz")):
        chem_file = _parse_fixture(source_path, tmp_path)
        revision = parse_revision_record_from_molop(chem_file, started_at=None)
        records = [
            frame_records_from_molop(frame, export_schema_version=chem_file.schema_version)
            for frame in chem_file
        ]

        assert revision.parser_id.startswith("molop.")
        assert revision.parser_version == installed_molop_version
        assert revision.molop_version == installed_molop_version
        assert revision.molgr_version == installed_molgr_version
        assert revision.parser_commit is None
        assert revision.molgr_commit is None
        assert revision.parser_provenance["effective_config_sha256"] == (
            revision.parser_config_hash
        )
        assert revision.source_complete is True
        assert chem_file.file_content == ""
        assert all(frame.frame_content == "" for frame in chem_file)
        assert [
            segment_record_from_molop(segment).segment_index
            for segment in chem_file.source_segments
        ] == list(range(len(chem_file.source_segments)))
        assert all(
            protocol_record_from_molop_segment(segment) is not None
            for segment in chem_file.source_segments
        )
        assert [record.frame.file_frame_index for record in records] == list(range(len(records)))
        assert all(
            record.molecule.topology_derivation.reconstruction_method == "molgr/cpp"
            and record.molecule.topology_derivation.reconstruction_version
            == installed_molgr_version
            and record.molecule.topology_derivation.reconstruction_metadata["molop_version"]
            == installed_molop_version
            for record in records
        )
        assert all(
            record.frame.coordinate_decimal_places == frame.coordinate_decimal_places
            for record, frame in zip(records, chem_file, strict=True)
        )

        totals["frames"] += len(records)
        totals["segments"] += len(chem_file.source_segments)
        totals["arrays"] += sum(len(record.arrays) for record in records)
        totals["thermochemistry"] += sum(record.thermochemistry is not None for record in records)
        totals["energies"] += sum(record.energy is not None for record in records)
        totals["energy_observations"] += sum(len(record.energy_observations) for record in records)
        totals["vibrations"] += sum(record.vibration is not None for record in records)
        totals["optimizations"] += sum(record.optimization is not None for record in records)
        totals["statuses"] += sum(record.status is not None for record in records)
        totals["molecular_orbitals"] += sum(
            record.molecular_orbitals is not None for record in records
        )
        totals["population_results"] += sum(
            record.charge_spin_populations is not None for record in records
        )
        totals["population_series"] += sum(
            len(record.atomic_population_series) for record in records
        )
        totals["polarizabilities"] += sum(record.polarizability is not None for record in records)
        totals["array_assignments"] += sum(len(record.array_assignments) for record in records)
        totals["running_times"] += sum(
            record.frame.running_time_seconds is not None for record in records
        )
        if revision.running_time_seconds is not None:
            frame_runtime_sum = sum(
                record.frame.running_time_seconds or 0.0 for record in records
            )
            if abs(revision.running_time_seconds - frame_runtime_sum) > 1e-6:
                file_runtime_differences.append(
                    (source_path.name, revision.running_time_seconds, frame_runtime_sum)
                )

    assert totals == da_bench_manifest["expected_molop_totals"]
    assert file_runtime_differences, (
        "at least one fixture must preserve a file-level runtime distinct from "
        "the sum of frame runtimes"
    )
    filename, file_runtime, frame_runtime_sum = file_runtime_differences[0]
    assert file_runtime > 0, filename
    assert frame_runtime_sum > 0, filename
    assert file_runtime != pytest.approx(frame_runtime_sum)
