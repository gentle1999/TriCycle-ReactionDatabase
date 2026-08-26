"""Thin mapping from validated public MolOP models to database records."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any, TypedDict, cast

import numpy as np

from tricycle_reaction_db.application.dtos import (
    AtomicPopulationSeriesRecord,
    BondOrderResultRecord,
    CalculationFrameRecord,
    CalculationProtocolRecord,
    CalculationSegmentRecord,
    CalculationStatusResultRecord,
    ChargeSpinPopulationResultRecord,
    ElectronicConfigurationRecord,
    ElectronicStateRecord,
    ElectronicStateSetRecord,
    EnergyObservationRecord,
    FrameEnergyResultRecord,
    GeometryOptimizationResultRecord,
    ImplicitSolvationResultRecord,
    MolecularOrbitalResultRecord,
    MultireferenceResultRecord,
    NMRResultRecord,
    NMRShieldingTensorRecord,
    NormalizedMoleculeRecord,
    ParseRevisionRecord,
    PolarizabilityResultRecord,
    ScientificArrayAssignmentRecord,
    ScientificArrayRecord,
    SinglePointPropertyResultRecord,
    ThermochemistryResultRecord,
    TotalSpinResultRecord,
    VibrationResultRecord,
)
from tricycle_reaction_db.domain.enums import (
    ElectronicStateSetKind,
    EnergyQuantitySemantics,
    FrameRole,
    GeometryAssignmentKind,
    OptimizationStatus,
    ParseCompleteness,
    QMSoftware,
    SCFStatus,
    SelectedEnergyKind,
    SourceFormat,
    TerminationStatus,
)
from tricycle_reaction_db.ingestion.artifacts import calculation_protocol_record
from tricycle_reaction_db.ingestion.molop import normalize_molop_frame
from tricycle_reaction_db.ingestion.molop_arrays import scientific_array_export_from_molop_frame


@dataclass(frozen=True, slots=True)
class MolOPFrameRecords:
    segment_index: int
    molecule: NormalizedMoleculeRecord
    frame: CalculationFrameRecord
    energy: FrameEnergyResultRecord | None
    energy_observations: tuple[EnergyObservationRecord, ...]
    optimization: GeometryOptimizationResultRecord | None
    vibration: VibrationResultRecord | None
    thermochemistry: ThermochemistryResultRecord | None
    status: CalculationStatusResultRecord | None
    molecular_orbitals: MolecularOrbitalResultRecord | None
    charge_spin_populations: ChargeSpinPopulationResultRecord | None
    atomic_population_series: tuple[AtomicPopulationSeriesRecord, ...]
    polarizability: PolarizabilityResultRecord | None
    nmr: NMRResultRecord | None
    nmr_shielding_tensors: tuple[NMRShieldingTensorRecord, ...]
    bond_orders: BondOrderResultRecord | None
    total_spin: TotalSpinResultRecord | None
    single_point_properties: SinglePointPropertyResultRecord | None
    electronic_state_sets: tuple[ElectronicStateSetRecord, ...]
    electronic_states: tuple[ElectronicStateRecord, ...]
    electronic_configurations: tuple[ElectronicConfigurationRecord, ...]
    multireference: MultireferenceResultRecord | None
    implicit_solvation: ImplicitSolvationResultRecord | None
    arrays: tuple[ScientificArrayRecord, ...]
    array_assignments: tuple[ScientificArrayAssignmentRecord, ...]


class _SourceSpanValues(TypedDict):
    source_start_byte: int | None
    source_end_byte: int | None
    source_start_char: int | None
    source_end_char: int | None
    source_start_line: int | None
    source_end_line: int | None


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _model_dump(
    value: Any,
    *,
    mode: str = "python",
    computed_fields: tuple[str, ...] = (),
    include: set[str] | None = None,
) -> dict[str, Any]:
    """Take the public MolOP/Pydantic dump as the adapter's source payload."""

    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    dump = getattr(value, "model_dump", None)
    if not callable(dump):
        raise TypeError(f"expected a mapping or Pydantic model, got {type(value)!r}")
    dump_kwargs: dict[str, Any] = {"mode": mode, "exclude_none": False}
    if include is not None:
        dump_kwargs["include"] = include
    payload = cast(dict[str, Any], dump(**dump_kwargs))
    for field_name in computed_fields:
        payload.setdefault(field_name, getattr(value, field_name))
    return payload


def _model_json(value: Any) -> dict[str, Any]:
    return _model_dump(value, mode="json")


def _canonical_json_sha256(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _optional_model_dump(
    value: Any,
    *,
    computed_fields: tuple[str, ...] = (),
    include: set[str] | None = None,
) -> dict[str, Any] | None:
    if value is None:
        return None
    return _model_dump(value, computed_fields=computed_fields, include=include)


def _child_payload(
    frame_payload: dict[str, Any],
    name: str,
    model: Any,
    *,
    computed_fields: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """Reuse the parent dump and fill only Pydantic computed child fields."""

    payload = frame_payload[name]
    if payload is None:
        return None
    if computed_fields:
        if not isinstance(payload, dict):
            raise TypeError(f"MolOP child payload {name!r} is not a mapping")
        for field_name in computed_fields:
            payload.setdefault(field_name, getattr(model, field_name))
    return cast(dict[str, Any], payload)


def _diagnostics(values: list[Any]) -> list[dict[str, Any]]:
    return [_model_json(value) for value in values]


def _parse_presence(values: dict[str, Any]) -> dict[str, str]:
    return {key: _enum_value(value) for key, value in values.items()}


def _span_values(span: Any) -> _SourceSpanValues:
    if span is None:
        return {
            "source_start_byte": None,
            "source_end_byte": None,
            "source_start_char": None,
            "source_end_char": None,
            "source_start_line": None,
            "source_end_line": None,
        }
    if isinstance(span, dict):
        return {
            "source_start_byte": span["start_byte"],
            "source_end_byte": span["end_byte"],
            "source_start_char": span["start_char"],
            "source_end_char": span["end_char"],
            "source_start_line": span["start_line"],
            "source_end_line": span["end_line"],
        }
    return {
        "source_start_byte": span.start_byte,
        "source_end_byte": span.end_byte,
        "source_start_char": span.start_char,
        "source_end_char": span.end_char,
        "source_start_line": span.start_line,
        "source_end_line": span.end_line,
    }


def parse_revision_record_from_molop(
    chem_file: Any,
    *,
    started_at: datetime | None,
    source_compression: str | None = None,
    artifact_sha256: str | None = None,
    artifact_size_bytes: int | None = None,
) -> ParseRevisionRecord:
    """Map MolOP file-level provenance and parse evidence without reparsing source text."""

    file_payload = _model_dump(chem_file, computed_fields=("source_diagnostics",))
    provenance_value = file_payload["parser_provenance"]
    if provenance_value is None:
        raise ValueError("MolOP source capture did not provide parser provenance")
    provenance = dict(_model_dump(provenance_value))
    source_evidence_captured = bool(chem_file.source_segments)
    provenance["source_evidence_captured"] = source_evidence_captured
    source_format = {
        "g16log": SourceFormat.GAUSSIAN_LOG,
        "orcalog": SourceFormat.ORCA_OUTPUT,
        "orcaout": SourceFormat.ORCA_OUTPUT,
    }.get(file_payload["source_format"], SourceFormat.OTHER)
    reconstruction_config = {
        "tricycle_ingestion_mapping_version": "molop-calculation-mapping-v2",
        "molop_version": provenance["molop_version"],
        "molgr_version": provenance["molgr_version"],
        "molop": provenance["effective_config"].get("molop", {}),
        "molgr": provenance["effective_config"].get("molgr", {}),
        "source_evidence_captured": source_evidence_captured,
    }
    return ParseRevisionRecord(
        export_schema_version=file_payload["schema_version"],
        parser_id=provenance["parser_id"],
        parser_version=provenance["parser_version"],
        molop_version=provenance["molop_version"],
        molgr_version=provenance["molgr_version"],
        rdkit_version=provenance["rdkit_version"],
        parser_provenance=provenance,
        parser_provenance_hash=_canonical_json_sha256(provenance),
        parser_config_hash=provenance["effective_config_sha256"],
        reconstruction_config_hash=_canonical_json_sha256(reconstruction_config),
        source_format=source_format,
        source_encoding=file_payload.get("source_encoding") or "utf-8",
        source_content_sha256=(
            artifact_sha256
            if artifact_sha256 is not None
            else file_payload.get("artifact_sha256")
        ),
        source_size_bytes=(
            artifact_size_bytes
            if artifact_size_bytes is not None
            else file_payload.get("artifact_size_bytes")
        ),
        source_compression=source_compression,
        source_complete=file_payload.get("source_complete"),
        parse_completeness=ParseCompleteness(
            _enum_value(file_payload.get("parse_completeness") or ParseCompleteness.NOT_ASSESSED)
        ),
        parse_diagnostics=_diagnostics(file_payload.get("source_diagnostics") or []),
        started_at=started_at,
    )


def protocol_record_from_molop_segment(
    segment: Any,
) -> CalculationProtocolRecord | None:
    """Map MolOP's normalized segment protocol to the database protocol identity."""

    segment_payload = _model_dump(segment)
    if segment_payload["protocol"] is None or segment_payload["qm_software"] is None:
        return None
    software = {
        "gaussian": QMSoftware.GAUSSIAN,
        "orca": QMSoftware.ORCA,
    }.get(segment_payload["qm_software"].lower(), QMSoftware.OTHER)
    protocol = _model_dump(segment_payload["protocol"], mode="json")
    return calculation_protocol_record(
        qm_software=software,
        qm_software_version=segment_payload["qm_software_version"] or "unknown",
        method_family=_optional_string(protocol.get("method_family")),
        method=_optional_string(protocol.get("method")),
        reference_method=_optional_string(protocol.get("reference_method")),
        functional=_optional_string(protocol.get("functional")),
        basis_set=_optional_string(protocol.get("basis_set")),
        auxiliary_basis_set=_optional_string(protocol.get("auxiliary_basis_set")),
        dispersion_model=_optional_string(protocol.get("dispersion_correction")),
        solvation_model=_optional_string(protocol.get("solvation_model")),
        solvent=_optional_string(protocol.get("solvent")),
        relativistic_method=_optional_string(protocol.get("relativistic")),
        task_requests=list(segment_payload["task_types"]),
        normalized_spec={
            "protocol": protocol,
            "task_requests": [_model_json(request) for request in segment_payload["task_requests"]],
            **(
                {
                    "qm_software": segment_payload["qm_software"],
                    "qm_software_version": segment_payload["qm_software_version"],
                }
                if software is QMSoftware.OTHER
                else {}
            ),
        },
    )


def segment_record_from_molop(segment: Any) -> CalculationSegmentRecord:
    """Map one MolOP ``SourceSegmentEvidence`` object."""

    segment_payload = _model_dump(segment)
    return CalculationSegmentRecord(
        segment_index=segment_payload["segment_index"],
        source_block_sha256=segment_payload.get("source_block_sha256"),
        source_frame_count=segment_payload["frame_count"],
        parse_presence=_parse_presence(segment_payload["parse_presence"]),
        parse_completeness=ParseCompleteness(_enum_value(segment_payload["parse_completeness"])),
        parse_diagnostics=_diagnostics(segment_payload["diagnostics"]),
        termination_status=_termination_status(segment_payload["termination_status"]),
        scf_status=_scf_status(segment_payload["scf_status"]),
        program_metadata={
            "molop_task_requests": [
                _model_json(request) for request in segment_payload["task_requests"]
            ],
            "molop_task_types": list(segment_payload["task_types"]),
            "molop_captured_frame_indices": list(segment_payload["captured_frame_indices"]),
        },
        **_span_values(segment_payload.get("source_span")),
    )


def frame_records_from_molop(
    frame: Any,
    *,
    export_schema_version: str,
    fallback_index: int | None = None,
) -> MolOPFrameRecords:
    """Map one validated MolOP frame and its public scientific submodels."""

    frame_payload = _model_dump(
        frame,
        computed_fields=(
            "parse_diagnostics",
            "coordinate_decimal_places",
            "force_source_field",
            "force_transformation",
        ),
    )
    molecule = normalize_molop_frame(frame)
    energy = _energy_record(frame_payload["energies"], export_schema_version)
    observations = tuple(
        EnergyObservationRecord(
            observation_index=index,
            method=observation["method"],
            quantity_semantics=EnergyQuantitySemantics(observation["quantity_semantics"]),
            value_hartree=float(observation["value"].to("hartree").magnitude),
            source_label=observation["source_label"],
        )
        for index, observation in enumerate(
            (frame_payload["energies"].get("observations") or [])
            if frame_payload["energies"]
            else []
        )
    )
    # Pydantic's top-level dump already serializes every nested public MolOP
    # model. Re-dumping each child here was a large per-frame CPU multiplier.
    optimization_payload = _child_payload(
        frame_payload,
        "geometry_optimization_status",
        frame.geometry_optimization_status,
        computed_fields=(
            "energy_change_threshold",
            "energy_change_converged",
            "rms_force_converged",
            "max_force_converged",
            "rms_displacement_converged",
            "max_displacement_converged",
        ),
    )
    optimization = _optimization_record(optimization_payload, export_schema_version)
    vibration_payload = _child_payload(
        frame_payload,
        "vibrations",
        frame.vibrations,
        computed_fields=(
            "mode_indices",
            "axis_order",
            "atom_order",
            "normalization",
            "mass_weighting",
        ),
    )
    vibration = _vibration_record(vibration_payload, export_schema_version)
    thermochemistry = _thermochemistry_record(frame_payload, export_schema_version)
    status_payload = frame_payload["status"]
    status = _status_record(status_payload, export_schema_version)
    molecular_orbitals_payload = frame_payload["molecular_orbitals"]
    molecular_orbitals = _molecular_orbital_record(
        molecular_orbitals_payload, export_schema_version
    )
    populations_payload = frame_payload["charge_spin_populations"]
    population_result, population_series = _population_records(
        populations_payload, export_schema_version
    )
    polarizability_payload = _child_payload(
        frame_payload,
        "polarizability",
        frame.polarizability,
        computed_fields=("isotropic_polarizability", "anisotropic_polarizability"),
    )
    polarizability = _polarizability_record(polarizability_payload, export_schema_version)
    nmr_payload = _child_payload(
        frame_payload,
        "nmr",
        frame.nmr,
        computed_fields=("coupling_atom_indices",),
    )
    nmr, shielding_tensors = _nmr_records(nmr_payload, export_schema_version)
    bond_orders_payload = frame_payload["bond_orders"]
    bond_orders = _bond_order_record(bond_orders_payload, export_schema_version)
    total_spin_payload = frame_payload["total_spin"]
    total_spin = _total_spin_record(total_spin_payload, export_schema_version)
    single_point_payload = frame_payload["single_point_properties"]
    single_point = _single_point_record(single_point_payload, export_schema_version)
    state_sets, states, configurations = _electronic_state_records(
        frame_payload, export_schema_version
    )
    multireference_payload = frame_payload["multireference_result"]
    multireference = _multireference_record(multireference_payload, export_schema_version)
    solvent_payload = frame_payload["solvent"]
    implicit_solvation = _implicit_solvation_record(solvent_payload, export_schema_version)
    arrays, array_assignments = scientific_array_export_from_molop_frame(
        frame,
        frame_payload=frame_payload,
    )
    segment_index = frame_payload.get("segment_index")
    if segment_index is None:
        segment_index = 0
    return MolOPFrameRecords(
        segment_index=segment_index,
        molecule=molecule,
        frame=_frame_record(
            frame_payload,
            molecule,
            energy,
            optimization,
            vibration,
            fallback_index=fallback_index,
        ),
        energy=energy,
        energy_observations=observations,
        optimization=optimization,
        vibration=vibration,
        thermochemistry=thermochemistry,
        status=status,
        molecular_orbitals=molecular_orbitals,
        charge_spin_populations=population_result,
        atomic_population_series=population_series,
        polarizability=polarizability,
        nmr=nmr,
        nmr_shielding_tensors=shielding_tensors,
        bond_orders=bond_orders,
        total_spin=total_spin,
        single_point_properties=single_point,
        electronic_state_sets=state_sets,
        electronic_states=states,
        electronic_configurations=configurations,
        multireference=multireference,
        implicit_solvation=implicit_solvation,
        arrays=tuple(arrays),
        array_assignments=tuple(array_assignments),
    )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _termination_status(value: bool | None) -> TerminationStatus:
    if value is True:
        return TerminationStatus.NORMAL
    if value is False:
        return TerminationStatus.ERROR
    return TerminationStatus.UNKNOWN


def _scf_status(value: bool | None) -> SCFStatus:
    if value is True:
        return SCFStatus.CONVERGED
    if value is False:
        return SCFStatus.FAILED
    return SCFStatus.UNKNOWN


def _quantity(value: Any, unit: str) -> float | None:
    return float(value.to(unit).magnitude) if value is not None else None


def _energy_record(
    energies: dict[str, Any] | None,
    schema_version: str,
) -> FrameEnergyResultRecord | None:
    if energies is None:
        return None
    return FrameEnergyResultRecord(
        electronic_energy_hartree=_quantity(energies["electronic_energy"], "hartree"),
        reference_energy_hartree=_quantity(energies["reference_energy"], "hartree"),
        mp2_energy_hartree=_quantity(energies["mp2_energy"], "hartree"),
        mp3_energy_hartree=_quantity(energies["mp3_energy"], "hartree"),
        mp4_energy_hartree=_quantity(energies["mp4_energy"], "hartree"),
        mp5_energy_hartree=_quantity(energies["mp5_energy"], "hartree"),
        ccsd_energy_hartree=_quantity(energies["ccsd_energy"], "hartree"),
        ccsd_t_energy_hartree=_quantity(energies["ccsd_t_energy"], "hartree"),
        source_schema_version=schema_version,
    )


def _optimization_record(
    optimization: dict[str, Any] | None,
    schema_version: str,
) -> GeometryOptimizationResultRecord | None:
    if optimization is None:
        return None
    return GeometryOptimizationResultRecord(
        geometry_optimized=optimization.get("geometry_optimized"),
        convergence_multiplier=optimization.get("convergence_multiplier") or 2.0,
        source_converged=optimization.get("source_converged"),
        source_labels=optimization.get("source_labels", {}),
        energy_change_hartree=_quantity(optimization.get("energy_change"), "hartree"),
        energy_change_threshold_hartree=_quantity(
            optimization.get("energy_change_threshold"),
            "hartree",
        ),
        energy_change_converged=optimization.get("energy_change_converged"),
        rms_force_hartree_per_bohr=_quantity(optimization.get("rms_force"), "hartree/bohr"),
        rms_force_threshold_hartree_per_bohr=_quantity(
            optimization.get("rms_force_threshold"),
            "hartree/bohr",
        ),
        rms_force_converged=optimization.get("rms_force_converged"),
        max_force_hartree_per_bohr=_quantity(optimization.get("max_force"), "hartree/bohr"),
        max_force_threshold_hartree_per_bohr=_quantity(
            optimization.get("max_force_threshold"),
            "hartree/bohr",
        ),
        max_force_converged=optimization.get("max_force_converged"),
        rms_displacement_bohr=_quantity(optimization.get("rms_displacement"), "bohr"),
        rms_displacement_threshold_bohr=_quantity(
            optimization.get("rms_displacement_threshold"),
            "bohr",
        ),
        rms_displacement_converged=optimization.get("rms_displacement_converged"),
        max_displacement_bohr=_quantity(optimization.get("max_displacement"), "bohr"),
        max_displacement_threshold_bohr=_quantity(
            optimization.get("max_displacement_threshold"),
            "bohr",
        ),
        max_displacement_converged=optimization.get("max_displacement_converged"),
        source_schema_version=schema_version,
    )


def _vibration_record(
    vibrations: dict[str, Any] | None,
    schema_version: str,
) -> VibrationResultRecord | None:
    if vibrations is None:
        return None
    frequencies = np.asarray(
        vibrations["frequencies"].to("1/centimeter").magnitude,
        dtype=np.float64,
    )
    return VibrationResultRecord(
        mode_count=int(frequencies.size),
        imaginary_mode_count=int(np.count_nonzero(frequencies < 0)),
        lowest_frequency_cm1=float(frequencies.min()) if frequencies.size else None,
        mode_indices=list(vibrations["mode_indices"]),
        axis_order=list(vibrations["axis_order"]) if vibrations["axis_order"] is not None else None,
        atom_order=vibrations["atom_order"],
        normalization=vibrations["normalization"],
        mass_weighting=vibrations["mass_weighting"],
        source_schema_version=schema_version,
    )


def _thermochemistry_record(
    frame: dict[str, Any],
    schema_version: str,
) -> ThermochemistryResultRecord | None:
    thermal = frame["thermal_informations"]
    if thermal is None:
        return None
    if frame["temperature"] is None or frame["pressure"] is None:
        raise ValueError("MolOP thermochemistry requires explicit temperature and pressure")
    temperature_kelvin = _quantity(frame["temperature"], "kelvin")
    pressure_atm = _quantity(frame["pressure"], "standard_atmosphere")
    if temperature_kelvin is None or pressure_atm is None:
        raise RuntimeError("MolOP thermochemistry units did not produce scalar values")
    return ThermochemistryResultRecord(
        temperature_kelvin=temperature_kelvin,
        pressure_atm=pressure_atm,
        zpe_correction_hartree=_quantity(thermal["ZPVE"], "hartree/particle"),
        thermal_energy_correction_hartree=_quantity(thermal["TCE"], "hartree/particle"),
        thermal_enthalpy_correction_hartree=_quantity(thermal["TCH"], "hartree/particle"),
        thermal_gibbs_correction_hartree=_quantity(thermal["TCG"], "hartree/particle"),
        zero_point_energy_hartree=_quantity(thermal["U_0"], "hartree/particle"),
        thermal_internal_energy_hartree=_quantity(thermal["U_T"], "hartree/particle"),
        enthalpy_hartree=_quantity(thermal["H_T"], "hartree/particle"),
        gibbs_free_energy_hartree=_quantity(thermal["G_T"], "hartree/particle"),
        entropy_cal_mol_k=_quantity(thermal["S"], "calorie/mole/kelvin"),
        heat_capacity_cv_cal_mol_k=_quantity(thermal["C_V"], "calorie/mole/kelvin"),
        molecular_mass_amu=_quantity(thermal["molecular_mass"], "amu"),
        rotational_symmetry_number=thermal["rotational_symmetry_number"],
        source_schema_version=schema_version,
    )


def _status_record(
    status: dict[str, Any] | None,
    schema_version: str,
) -> CalculationStatusResultRecord | None:
    if status is None or (status["scf_converged"] is None and status["normal_terminated"] is None):
        return None
    return CalculationStatusResultRecord(
        scf_converged=status["scf_converged"],
        normal_terminated=status["normal_terminated"],
        source_schema_version=schema_version,
    )


def _molecular_orbital_record(
    molecular_orbitals: dict[str, Any] | None,
    schema_version: str,
) -> MolecularOrbitalResultRecord | None:
    if molecular_orbitals is None:
        return None
    return MolecularOrbitalResultRecord(
        electronic_state=molecular_orbitals["electronic_state"],
        alpha_orbital_count=len(molecular_orbitals["alpha_energies"]),
        beta_orbital_count=len(molecular_orbitals["beta_energies"]),
        coefficient_count=sum(value is not None for value in molecular_orbitals["coefficients"]),
        alpha_occupancies=[
            None if value is None else float(value)
            for value in molecular_orbitals["alpha_occupancies"]
        ],
        beta_occupancies=[
            None if value is None else float(value)
            for value in molecular_orbitals["beta_occupancies"]
        ],
        alpha_symmetries=list(molecular_orbitals["alpha_symmetries"]),
        beta_symmetries=list(molecular_orbitals["beta_symmetries"]),
        source_schema_version=schema_version,
    )


def _population_records(
    populations: dict[str, Any] | None,
    schema_version: str,
) -> tuple[ChargeSpinPopulationResultRecord | None, tuple[AtomicPopulationSeriesRecord, ...]]:
    if populations is None:
        return None, ()
    series = tuple(
        AtomicPopulationSeriesRecord(
            series_key=series_key,
            scheme=value["scheme"],
            quantity=value["quantity"],
            value_count=len(value["values"]),
            spin_channel=value["spin_channel"],
            source_label=value["source_label"],
            series_metadata=value["metadata"],
        )
        for series_key, value in populations["populations"].items()
    )
    return (
        ChargeSpinPopulationResultRecord(
            series_count=len(series), source_schema_version=schema_version
        ),
        series,
    )


def _polarizability_record(
    polarizability: dict[str, Any] | None,
    schema_version: str,
) -> PolarizabilityResultRecord | None:
    if polarizability is None:
        return None
    return PolarizabilityResultRecord(
        electronic_spatial_extent_bohr2=_quantity(
            polarizability.get("electronic_spatial_extent"), "bohr ** 2"
        ),
        isotropic_polarizability_bohr3=_quantity(
            polarizability.get("isotropic_polarizability"), "bohr ** 3"
        ),
        anisotropic_polarizability_bohr3=_quantity(
            polarizability.get("anisotropic_polarizability"), "bohr ** 3"
        ),
        source_schema_version=schema_version,
    )


def _nmr_records(
    nmr: dict[str, Any] | None,
    schema_version: str,
) -> tuple[NMRResultRecord | None, tuple[NMRShieldingTensorRecord, ...]]:
    if nmr is None:
        return None, ()
    tensors = tuple(
        NMRShieldingTensorRecord(
            atom_index=tensor["atom_index"],
            atom_symbol=tensor["atom_symbol"],
            isotropic_ppm=_quantity(tensor["isotropic"], "ppm"),
            anisotropy_ppm=_quantity(tensor["anisotropy"], "ppm"),
            anisotropy_convention=tensor["anisotropy_convention"],
            orientation=tensor["orientation"],
        )
        for tensor in nmr["shielding_tensors"]
    )
    return (
        NMRResultRecord(
            gauge=nmr["gauge"],
            shielding_count=len(tensors),
            coupling_atom_indices=list(nmr["coupling_atom_indices"]),
            source_schema_version=schema_version,
        ),
        tensors,
    )


def _bond_order_record(
    bond_orders: dict[str, Any] | None, schema_version: str
) -> BondOrderResultRecord | None:
    if bond_orders is None:
        return None
    matrix_count = sum(
        np.asarray(bond_orders[field_name]).size > 0
        for field_name in (
            "wiberg_bond_order",
            "mo_bond_order",
            "mayer_bond_order",
            "atom_atom_overlap_bond_order",
            "nbo_bond_order",
            "nbo_bond_order_for_alpha_spin",
            "nbo_bond_order_for_beta_spin",
        )
    )
    return BondOrderResultRecord(matrix_count=matrix_count, source_schema_version=schema_version)


def _total_spin_record(
    total_spin: dict[str, Any] | None, schema_version: str
) -> TotalSpinResultRecord | None:
    if total_spin is None:
        return None
    return TotalSpinResultRecord(
        spin_square=total_spin["spin_square"],
        spin_quantum_number=total_spin["spin_quantum_number"],
        source_schema_version=schema_version,
    )


def _single_point_record(
    properties: dict[str, Any] | None,
    schema_version: str,
) -> SinglePointPropertyResultRecord | None:
    if properties is None:
        return None
    return SinglePointPropertyResultRecord(
        vertical_ionization_potential_ev=_quantity(properties["vip"], "eV / particle"),
        vertical_electron_affinity_ev=_quantity(properties["vea"], "eV / particle"),
        global_electrophilicity_index_ev=_quantity(properties["gei"], "eV / particle"),
        source_schema_version=schema_version,
    )


def _electronic_state_records(
    frame: dict[str, Any],
    schema_version: str,
) -> tuple[
    tuple[ElectronicStateSetRecord, ...],
    tuple[ElectronicStateRecord, ...],
    tuple[ElectronicConfigurationRecord, ...],
]:
    sets: list[ElectronicStateSetRecord] = []
    states: list[ElectronicStateRecord] = []
    configurations: list[ElectronicConfigurationRecord] = []
    source_sets = (
        (ElectronicStateSetKind.FRAME, frame["electronic_states"]),
        (
            ElectronicStateSetKind.MULTIREFERENCE,
            frame["multireference_result"]["electronic_states"]
            if frame["multireference_result"] is not None
            else None,
        ),
    )
    for set_kind, state_set in source_sets:
        if state_set is None:
            continue
        sets.append(
            ElectronicStateSetRecord(
                kind=set_kind,
                state_count=len(state_set["states"]),
                source_schema_version=schema_version,
            )
        )
        for state_ordinal, state in enumerate(state_set["states"]):
            states.append(
                ElectronicStateRecord(
                    set_kind=set_kind,
                    state_ordinal=state_ordinal,
                    state_index=state["state_index"],
                    root=state["root"],
                    label=state["label"],
                    multiplicity=state["multiplicity"],
                    spin=state["spin"],
                    irrep=state["irrep"],
                    method=state["method"],
                    energy_hartree=_quantity(state["energy"], "hartree"),
                    excitation_energy_ev=_quantity(state["excitation_energy"], "eV"),
                    oscillator_strength=state["oscillator_strength"],
                    state_properties=state["properties"],
                    source=state["source"],
                )
            )
            configurations.extend(
                ElectronicConfigurationRecord(
                    set_kind=set_kind,
                    state_ordinal=state_ordinal,
                    configuration_ordinal=configuration_ordinal,
                    label=configuration["label"],
                    coefficient=configuration["coefficient"],
                    weight=configuration["weight"],
                    occupation=list(configuration["occupation"]),
                    orbital_indices=list(configuration["orbital_indices"]),
                    raw=configuration["raw"],
                )
                for configuration_ordinal, configuration in enumerate(state["configurations"])
            )
    return tuple(sets), tuple(states), tuple(configurations)


def _multireference_record(
    result: dict[str, Any] | None,
    schema_version: str,
) -> MultireferenceResultRecord | None:
    if result is None:
        return None
    active_space = result["active_space"]
    return MultireferenceResultRecord(
        method=result["method"],
        reference_method=result["reference_method"],
        ci_type=result["ci_type"],
        active_space_electrons=active_space["electrons"] if active_space else None,
        active_space_orbitals=active_space["orbitals"] if active_space else None,
        active_space_roots=active_space["roots"] if active_space else None,
        active_orbitals=list(active_space["active_orbitals"]) if active_space else [],
        inactive_orbitals=list(active_space["inactive_orbitals"]) if active_space else [],
        frozen_orbitals=list(active_space["frozen_orbitals"]) if active_space else [],
        active_space_raw=active_space["raw"] if active_space else "",
        active_space_options=active_space["options"] if active_space else {},
        corrections=result["corrections"],
        diagnostics=list(result["diagnostics"]),
        result_properties=result["properties"],
        source_schema_version=schema_version,
    )


def _implicit_solvation_record(
    solvent: dict[str, Any] | None,
    schema_version: str,
) -> ImplicitSolvationResultRecord | None:
    if solvent is None:
        return None
    return ImplicitSolvationResultRecord(
        solvent=solvent["solvent"],
        solvent_model=solvent["solvent_model"],
        atomic_radii=solvent["atomic_radii"],
        solvent_epsilon=solvent["solvent_epsilon"],
        solvent_epsilon_infinite=solvent["solvent_epsilon_infinite"],
        source_schema_version=schema_version,
    )


def _selected_energy(
    energy: FrameEnergyResultRecord | None,
) -> tuple[float | None, SelectedEnergyKind | None]:
    if energy is None:
        return None, None
    priorities = (
        (energy.electronic_energy_hartree, SelectedEnergyKind.ELECTRONIC_TOTAL),
        (energy.ccsd_t_energy_hartree, SelectedEnergyKind.CCSD_T_TOTAL),
        (energy.ccsd_energy_hartree, SelectedEnergyKind.CCSD_TOTAL),
        (energy.mp5_energy_hartree, SelectedEnergyKind.MP5_TOTAL),
        (energy.mp4_energy_hartree, SelectedEnergyKind.MP4_TOTAL),
        (energy.mp3_energy_hartree, SelectedEnergyKind.MP3_TOTAL),
        (energy.mp2_energy_hartree, SelectedEnergyKind.MP2_TOTAL),
        (energy.reference_energy_hartree, SelectedEnergyKind.REFERENCE_TOTAL),
    )
    return next(((value, kind) for value, kind in priorities if value is not None), (None, None))


def _frame_record(
    frame: dict[str, Any],
    molecule: NormalizedMoleculeRecord,
    energy: FrameEnergyResultRecord | None,
    optimization: GeometryOptimizationResultRecord | None,
    vibration: VibrationResultRecord | None,
    *,
    fallback_index: int | None = None,
) -> CalculationFrameRecord:
    segment_frame_index = frame.get("segment_frame_index")
    file_frame_index = frame.get("file_frame_index")
    if segment_frame_index is None:
        segment_frame_index = fallback_index
    if file_frame_index is None:
        file_frame_index = fallback_index
    if segment_frame_index is None or file_frame_index is None:
        raise ValueError("MolOP frame is missing stable source indices")
    selected_energy, selected_kind = _selected_energy(energy)
    optimization_status = OptimizationStatus.UNKNOWN
    if optimization is not None:
        if optimization.geometry_optimized is True:
            optimization_status = OptimizationStatus.CONVERGED
        elif optimization.geometry_optimized is False:
            optimization_status = OptimizationStatus.NOT_CONVERGED
    elif _parse_presence(frame.get("parse_presence") or {}).get("optimization") == "not_requested":
        optimization_status = OptimizationStatus.NOT_REQUESTED
    return CalculationFrameRecord(
        frame_index=segment_frame_index,
        file_frame_index=file_frame_index,
        frame_role=FrameRole(frame.get("frame_role") or FrameRole.SINGLE_POINT),
        source_block_sha256=frame.get("source_block_sha256"),
        parse_presence=_parse_presence(frame.get("parse_presence") or {}),
        parse_completeness=ParseCompleteness(
            _enum_value(frame.get("parse_completeness") or ParseCompleteness.NOT_ASSESSED)
        ),
        parse_diagnostics=_diagnostics(frame.get("parse_diagnostics") or []),
        charge=frame["charge"],
        multiplicity=frame["multiplicity"],
        coordinate_decimal_places=frame["coordinate_decimal_places"],
        geometry_assignment_kind=GeometryAssignmentKind.PARSED_EXACT,
        observed_coordinates=molecule.observed_coordinates,
        observed_coordinate_hash=molecule.observed_coordinate_hash,
        observed_to_geometry_atom_indices=molecule.observed_to_geometry_atom_indices,
        observed_to_geometry_transform=molecule.observed_to_geometry_transform,
        geometry_assignment_rmsd_angstrom=molecule.geometry_assignment_rmsd_angstrom,
        geometry_assignment_max_abs_angstrom=molecule.geometry_assignment_max_abs_angstrom,
        geometry_assignment_policy_version="geometry-internal-coordinate-match-v3",
        scf_status=_scf_status(frame["status"]["scf_converged"] if frame["status"] else None),
        optimization_status=optimization_status,
        electronic_total_energy_hartree=(energy.electronic_energy_hartree if energy else None),
        reference_total_energy_hartree=(energy.reference_energy_hartree if energy else None),
        mp2_total_energy_hartree=(energy.mp2_energy_hartree if energy else None),
        mp3_total_energy_hartree=(energy.mp3_energy_hartree if energy else None),
        mp4_total_energy_hartree=(energy.mp4_energy_hartree if energy else None),
        mp5_total_energy_hartree=(energy.mp5_energy_hartree if energy else None),
        ccsd_total_energy_hartree=(energy.ccsd_energy_hartree if energy else None),
        ccsd_t_total_energy_hartree=(energy.ccsd_t_energy_hartree if energy else None),
        selected_energy_hartree=selected_energy,
        selected_energy_kind=selected_kind,
        energy_selection_policy_version=(
            "molop-energies-priority-v1" if selected_energy is not None else None
        ),
        frequency_count=vibration.mode_count if vibration else None,
        negative_frequency_count=vibration.imaginary_mode_count if vibration else None,
        lowest_frequency_cm1=vibration.lowest_frequency_cm1 if vibration else None,
        running_time_seconds=_quantity(frame["running_time"], "second"),
        program_metadata_schema_version="molop-frame-evidence-v1",
        program_metadata={
            "molop_frame_id": frame.get("frame_id", fallback_index),
            "coordinate_source": frame.get("coordinate_source"),
            "coordinate_provenance": frame.get("coordinate_provenance"),
            "force_source_field": frame.get("force_source_field"),
            "force_transformation": frame.get("force_transformation"),
            # Keep the graph-reconstruction trust decision next to the frame
            # evidence so charge/spin values from an OpenBabel fallback are
            # queryable without inspecting the derived topology row.
            "topology_reconstruction_backend": _enum_value(
                frame.get("topology_reconstruction_backend")
            ),
            "topology_reconstruction_status": _enum_value(
                frame.get("topology_reconstruction_status")
            ),
        },
        **(
            optimization.model_dump(
                include={
                    "energy_change_hartree",
                    "energy_change_threshold_hartree",
                    "energy_change_converged",
                    "rms_force_hartree_per_bohr",
                    "rms_force_threshold_hartree_per_bohr",
                    "rms_force_converged",
                    "max_force_hartree_per_bohr",
                    "max_force_threshold_hartree_per_bohr",
                    "max_force_converged",
                    "rms_displacement_bohr",
                    "rms_displacement_threshold_bohr",
                    "rms_displacement_converged",
                    "max_displacement_bohr",
                    "max_displacement_threshold_bohr",
                    "max_displacement_converged",
                }
            )
            if optimization is not None
            else {}
        ),
        **_span_values(frame.get("source_span")),
    )


__all__ = [
    "MolOPFrameRecords",
    "frame_records_from_molop",
    "parse_revision_record_from_molop",
    "protocol_record_from_molop_segment",
    "segment_record_from_molop",
]
