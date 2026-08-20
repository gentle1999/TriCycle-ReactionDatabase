"""Validated records for versioned calculation facts and numerical results."""

from hashlib import sha256
from math import isnan
from typing import Any, Self

import numpy as np
import numpy.typing as npt
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from tricycle_reaction_db.domain.enums import (
    ElectronicStateKind,
    ElectronicStateSetKind,
    EnergyQuantitySemantics,
    FrameRole,
    GeometryAssignmentKind,
    OptimizationStatus,
    ParseCompleteness,
    ParseStatus,
    SCFStatus,
    ScientificArrayKind,
    ScientificArrayOwnerKind,
    SelectedEnergyKind,
    SourceFormat,
    TerminationStatus,
)
from tricycle_reaction_db.domain.precision import EnergyHartree

_SHA256_PATTERN = r"^[0-9a-f]{64}$"

_SCIENTIFIC_ARRAY_UNITS = {
    ScientificArrayKind.FORCES: "hartree/bohr",
    ScientificArrayKind.HESSIAN: "hartree/bohr^2",
    ScientificArrayKind.VIBRATIONAL_FREQUENCIES: "cm^-1",
    ScientificArrayKind.REDUCED_MASSES: "amu",
    ScientificArrayKind.VIBRATIONAL_FORCE_CONSTANTS: "mdyne/angstrom",
    ScientificArrayKind.IR_INTENSITIES: "km/mol",
    ScientificArrayKind.NORMAL_MODES: "angstrom",
    ScientificArrayKind.MOMENTS_OF_INERTIA: "amu*bohr^2",
    ScientificArrayKind.ROTATIONAL_TEMPERATURES: "kelvin",
    ScientificArrayKind.ROTATIONAL_CONSTANTS: "gigahertz",
    ScientificArrayKind.VIBRATIONAL_TEMPERATURES: "kelvin",
    ScientificArrayKind.ORBITAL_ALPHA_ENERGIES: "hartree",
    ScientificArrayKind.ORBITAL_BETA_ENERGIES: "hartree",
    ScientificArrayKind.ORBITAL_COEFFICIENT: "dimensionless",
    ScientificArrayKind.ATOMIC_POPULATION: "dimensionless",
    ScientificArrayKind.POLARIZABILITY_TENSOR: "bohr^3",
    ScientificArrayKind.ELECTRIC_DIPOLE_MOMENT: "debye",
    ScientificArrayKind.DIPOLE: "debye",
    ScientificArrayKind.QUADRUPOLE: "debye*angstrom",
    ScientificArrayKind.TRACELESS_QUADRUPOLE: "debye*angstrom",
    ScientificArrayKind.OCTAPOLE: "debye*angstrom^2",
    ScientificArrayKind.HEXADECAPOLE: "debye*angstrom^3",
    ScientificArrayKind.NMR_SHIELDING_TENSOR: "ppm",
    ScientificArrayKind.NMR_PRINCIPAL_VALUES: "ppm",
    ScientificArrayKind.NMR_COUPLING_K: "hertz",
    ScientificArrayKind.NMR_COUPLING_J: "hertz",
    ScientificArrayKind.NMR_COUPLING_K_COMPONENT: "hertz",
    ScientificArrayKind.NMR_COUPLING_J_COMPONENT: "hertz",
    ScientificArrayKind.BOND_ORDER_MATRIX: "dimensionless",
    ScientificArrayKind.FUKUI_POSITIVE: "dimensionless",
    ScientificArrayKind.FUKUI_NEGATIVE: "dimensionless",
    ScientificArrayKind.FUKUI_ZERO: "dimensionless",
    ScientificArrayKind.FRACTIONAL_OCCUPATION_DENSITY: "dimensionless",
    ScientificArrayKind.TRANSITION_DIPOLE: "debye",
}

_OPTIMIZATION_METRICS = (
    ("energy_change_hartree", "energy_change_threshold_hartree", "energy_change_converged"),
    (
        "rms_force_hartree_per_bohr",
        "rms_force_threshold_hartree_per_bohr",
        "rms_force_converged",
    ),
    (
        "max_force_hartree_per_bohr",
        "max_force_threshold_hartree_per_bohr",
        "max_force_converged",
    ),
    (
        "rms_displacement_bohr",
        "rms_displacement_threshold_bohr",
        "rms_displacement_converged",
    ),
    (
        "max_displacement_bohr",
        "max_displacement_threshold_bohr",
        "max_displacement_converged",
    ),
)

_THERMOCHEMISTRY_VALUE_FIELDS = (
    "zpe_correction_hartree",
    "thermal_energy_correction_hartree",
    "thermal_enthalpy_correction_hartree",
    "thermal_gibbs_correction_hartree",
    "zero_point_energy_hartree",
    "thermal_internal_energy_hartree",
    "enthalpy_hartree",
    "gibbs_free_energy_hartree",
    "entropy_cal_mol_k",
    "heat_capacity_cv_cal_mol_k",
    "molecular_mass_amu",
    "rotational_symmetry_number",
)


def _validate_source_span(
    *,
    start_byte: int,
    end_byte: int,
    start_char: int | None,
    end_char: int | None,
    start_line: int,
    end_line: int,
) -> None:
    if end_byte <= start_byte:
        raise ValueError("source byte span must be a non-empty half-open interval")
    if (start_char is None) != (end_char is None):
        raise ValueError("source character offsets must either both be set or both be absent")
    if start_char is not None and end_char is not None and end_char <= start_char:
        raise ValueError("source character span must be a non-empty half-open interval")
    if end_line <= start_line:
        raise ValueError("source line span must be a non-empty half-open interval")


def _same_float(left: float, right: float) -> bool:
    return left == right or (isnan(left) and isnan(right))


class ParseRevisionRecord(BaseModel):
    """Immutable inputs and outcome for one reproducible parse attempt."""

    model_config = ConfigDict(frozen=True)

    export_schema_version: str = Field(min_length=1, max_length=64)
    parser_name: str = Field(default="molop", min_length=1, max_length=64)
    parser_id: str = Field(min_length=1, max_length=512)
    parser_version: str = Field(min_length=1, max_length=128)
    molop_version: str = Field(min_length=1, max_length=128)
    parser_commit: str | None = Field(default=None, min_length=1, max_length=128)
    molgr_version: str | None = Field(default=None, min_length=1, max_length=128)
    molgr_commit: str | None = Field(default=None, min_length=1, max_length=128)
    rdkit_version: str = Field(min_length=1, max_length=128)
    parser_provenance: dict[str, Any]
    parser_provenance_hash: str = Field(pattern=_SHA256_PATTERN)
    parser_config_hash: str = Field(pattern=_SHA256_PATTERN)
    reconstruction_config_hash: str = Field(pattern=_SHA256_PATTERN)
    source_format: SourceFormat
    source_encoding: str = Field(min_length=1, max_length=64)
    source_content_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    source_size_bytes: int | None = Field(default=None, ge=0)
    source_compression: str | None = Field(default=None, min_length=1, max_length=32)
    source_complete: bool | None = None
    parse_completeness: ParseCompleteness = ParseCompleteness.NOT_ASSESSED
    parse_diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    record_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    status: ParseStatus = ParseStatus.PENDING
    error_code: str | None = Field(default=None, min_length=1, max_length=128)
    error_message: str | None = None
    error_metadata: dict[str, Any] | None = None
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        projected_values = {
            "parser_id": self.parser_id,
            "parser_version": self.parser_version,
            "molop_version": self.molop_version,
            "molgr_version": self.molgr_version,
            "rdkit_version": self.rdkit_version,
            "effective_config_sha256": self.parser_config_hash,
        }
        mismatched = [
            key
            for key, expected in projected_values.items()
            if self.parser_provenance.get(key) != expected
        ]
        if mismatched:
            raise ValueError(
                "parser provenance does not match projected fields: " + ", ".join(mismatched)
            )
        if (
            self.started_at is not None
            and self.completed_at is not None
            and self.completed_at < self.started_at
        ):
            raise ValueError("completed_at cannot precede started_at")
        if self.status is ParseStatus.SUCCEEDED and (
            self.record_sha256 is None or self.completed_at is None
        ):
            raise ValueError("a succeeded parse revision requires record_sha256 and completed_at")
        return self


class ParseRevisionCompletionRecord(BaseModel):
    """Validated payload used to finalize a populated parse revision."""

    model_config = ConfigDict(frozen=True)

    record_sha256: str = Field(pattern=_SHA256_PATTERN)
    completed_at: AwareDatetime


class CalculationSegmentRecord(BaseModel):
    """Source-local facts for one Link1 section, restart, or independent job."""

    model_config = ConfigDict(frozen=True)

    segment_index: int = Field(ge=0)
    segment_label: str | None = None
    source_start_byte: int = Field(ge=0)
    source_end_byte: int = Field(ge=0)
    source_start_char: int | None = Field(default=None, ge=0)
    source_end_char: int | None = Field(default=None, ge=0)
    source_start_line: int = Field(ge=1)
    source_end_line: int = Field(ge=1)
    source_block_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_frame_count: int | None = Field(default=None, ge=0)
    parse_presence: dict[str, str] = Field(default_factory=dict)
    parse_completeness: ParseCompleteness = ParseCompleteness.NOT_ASSESSED
    parse_diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    requested_cpu_count: int | None = Field(default=None, gt=0)
    requested_memory_mb: int | None = Field(default=None, gt=0)
    termination_status: TerminationStatus = TerminationStatus.UNKNOWN
    scf_status: SCFStatus = SCFStatus.UNKNOWN
    wall_time_seconds: float | None = Field(default=None, ge=0)
    program_metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_source_span(self) -> Self:
        _validate_source_span(
            start_byte=self.source_start_byte,
            end_byte=self.source_end_byte,
            start_char=self.source_start_char,
            end_char=self.source_end_char,
            start_line=self.source_start_line,
            end_line=self.source_end_line,
        )
        return self


class CalculationFrameRecord(BaseModel):
    """Normalized scalar facts for one ordered physical calculation frame."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    frame_index: int = Field(ge=0)
    file_frame_index: int = Field(ge=0)
    frame_role: FrameRole
    source_start_byte: int = Field(ge=0)
    source_end_byte: int = Field(ge=0)
    source_start_char: int | None = Field(default=None, ge=0)
    source_end_char: int | None = Field(default=None, ge=0)
    source_start_line: int = Field(ge=1)
    source_end_line: int = Field(ge=1)
    source_block_sha256: str = Field(pattern=_SHA256_PATTERN)
    parse_presence: dict[str, str] = Field(default_factory=dict)
    parse_completeness: ParseCompleteness = ParseCompleteness.NOT_ASSESSED
    parse_diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    charge: int
    multiplicity: int = Field(gt=0)
    coordinate_decimal_places: int | None = Field(default=None, ge=0, le=18)
    geometry_assignment_kind: GeometryAssignmentKind
    observed_coordinates: npt.NDArray[np.float64]
    observed_coordinate_hash: str = Field(pattern=_SHA256_PATTERN)
    observed_to_geometry_atom_indices: list[int]
    observed_to_geometry_transform: list[float] = Field(min_length=16, max_length=16)
    geometry_assignment_rmsd_angstrom: float = Field(ge=0, allow_inf_nan=False)
    geometry_assignment_max_abs_angstrom: float = Field(ge=0, allow_inf_nan=False)
    geometry_assignment_policy_version: str = Field(min_length=1, max_length=64)
    electronic_state_kind: ElectronicStateKind = ElectronicStateKind.GROUND
    electronic_state_index: int = Field(default=0, ge=0)
    scf_status: SCFStatus = SCFStatus.UNKNOWN
    optimization_status: OptimizationStatus = OptimizationStatus.UNKNOWN
    electronic_total_energy_hartree: EnergyHartree | None = None
    reference_total_energy_hartree: EnergyHartree | None = None
    mp2_total_energy_hartree: EnergyHartree | None = None
    mp3_total_energy_hartree: EnergyHartree | None = None
    mp4_total_energy_hartree: EnergyHartree | None = None
    mp5_total_energy_hartree: EnergyHartree | None = None
    ccsd_total_energy_hartree: EnergyHartree | None = None
    ccsd_t_total_energy_hartree: EnergyHartree | None = None
    selected_energy_hartree: EnergyHartree | None = None
    selected_energy_kind: SelectedEnergyKind | None = None
    energy_selection_policy_version: str | None = Field(default=None, max_length=64)
    energy_change_hartree: float | None = None
    energy_change_threshold_hartree: float | None = Field(default=None, ge=0)
    energy_change_converged: bool | None = None
    rms_force_hartree_per_bohr: float | None = None
    rms_force_threshold_hartree_per_bohr: float | None = Field(default=None, ge=0)
    rms_force_converged: bool | None = None
    max_force_hartree_per_bohr: float | None = None
    max_force_threshold_hartree_per_bohr: float | None = Field(default=None, ge=0)
    max_force_converged: bool | None = None
    rms_displacement_bohr: float | None = None
    rms_displacement_threshold_bohr: float | None = Field(default=None, ge=0)
    rms_displacement_converged: bool | None = None
    max_displacement_bohr: float | None = None
    max_displacement_threshold_bohr: float | None = Field(default=None, ge=0)
    max_displacement_converged: bool | None = None
    frequency_count: int | None = Field(default=None, ge=0)
    negative_frequency_count: int | None = Field(default=None, ge=0)
    lowest_frequency_cm1: float | None = None
    running_time_seconds: float | None = Field(default=None, ge=0)
    program_metadata_schema_version: str = Field(
        default="calculation-frame-metadata-v1",
        min_length=1,
        max_length=64,
    )
    program_metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_frame(self) -> Self:
        _validate_source_span(
            start_byte=self.source_start_byte,
            end_byte=self.source_end_byte,
            start_char=self.source_start_char,
            end_char=self.source_end_char,
            start_line=self.source_start_line,
            end_line=self.source_end_line,
        )
        if (
            self.electronic_state_kind is not ElectronicStateKind.GROUND
            or self.electronic_state_index != 0
        ):
            raise ValueError("v1 calculation frames only support ground electronic state index 0")
        coordinates = np.asarray(self.observed_coordinates)
        if coordinates.ndim != 2 or coordinates.shape[1] != 3:
            raise ValueError("observed_coordinates must have shape (atom_count, 3)")
        if coordinates.dtype != np.dtype("<f8") or not coordinates.flags.c_contiguous:
            raise ValueError("observed_coordinates must be C-contiguous little-endian float64")
        if not np.isfinite(coordinates).all():
            raise ValueError("observed_coordinates must contain only finite values")
        if sha256(coordinates.tobytes(order="C")).hexdigest() != self.observed_coordinate_hash:
            raise ValueError("observed_coordinate_hash does not match observed_coordinates")
        atom_count = coordinates.shape[0]
        if sorted(self.observed_to_geometry_atom_indices) != list(range(atom_count)):
            raise ValueError("observed atom indices must be a full coordinate permutation")
        transform = np.asarray(self.observed_to_geometry_transform, dtype=np.float64).reshape(4, 4)
        rotation = transform[:3, :3]
        if not np.isfinite(transform).all():
            raise ValueError("observed-to-Geometry transform must contain only finite values")
        if not np.allclose(transform[3], [0.0, 0.0, 0.0, 1.0], rtol=0.0, atol=1e-12):
            raise ValueError("observed-to-Geometry transform must be homogeneous")
        if not np.allclose(rotation @ rotation.T, np.eye(3), rtol=0.0, atol=1e-10):
            raise ValueError("observed-to-Geometry rotation must be orthonormal")
        if not np.isclose(np.linalg.det(rotation), 1.0, rtol=0.0, atol=1e-10):
            raise ValueError("observed-to-Geometry transform must use a proper rotation")
        if self.geometry_assignment_max_abs_angstrom < self.geometry_assignment_rmsd_angstrom:
            raise ValueError("geometry assignment maximum deviation cannot be smaller than RMSD")
        selected_fields = (
            self.selected_energy_hartree,
            self.selected_energy_kind,
            self.energy_selection_policy_version,
        )
        if sum(value is not None for value in selected_fields) not in {0, 3}:
            raise ValueError("selected energy value, kind, and policy version must be set together")
        if self.selected_energy_kind is not None and self.selected_energy_hartree is not None:
            source_energy = getattr(self, self.selected_energy_kind.value)
            if source_energy is None or not _same_float(
                source_energy, self.selected_energy_hartree
            ):
                raise ValueError(
                    "selected energy must equal its explicitly selected total-energy field"
                )

        for value_name, threshold_name, converged_name in _OPTIMIZATION_METRICS:
            if getattr(self, converged_name) is not None and (
                getattr(self, value_name) is None or getattr(self, threshold_name) is None
            ):
                raise ValueError(f"{converged_name} requires its metric value and threshold")

        if self.frequency_count is None:
            if self.negative_frequency_count is not None or self.lowest_frequency_cm1 is not None:
                raise ValueError("frequency details require frequency_count")
        elif self.frequency_count == 0:
            if self.negative_frequency_count != 0 or self.lowest_frequency_cm1 is not None:
                raise ValueError("an empty frequency result requires zero negative frequencies")
        elif self.negative_frequency_count is None or self.lowest_frequency_cm1 is None:
            raise ValueError(
                "a non-empty frequency result requires count and lowest-frequency summary"
            )
        if (
            self.frequency_count is not None
            and self.negative_frequency_count is not None
            and self.negative_frequency_count > self.frequency_count
        ):
            raise ValueError("negative_frequency_count cannot exceed frequency_count")
        return self


class ScientificArrayRecord(BaseModel):
    """Typed array plus independently supplied, verifiable payload summary."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    kind: ScientificArrayKind
    ordinal: int = Field(ge=0)
    unit: str = Field(min_length=1, max_length=64)
    dtype: str = Field(min_length=1, max_length=64)
    shape: list[int] = Field(min_length=1)
    array_nbytes: int = Field(ge=0)
    payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    data: npt.NDArray[np.generic]
    metadata_schema_version: str | None = Field(default=None, min_length=1, max_length=64)
    array_metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_metadata(self) -> Self:
        if any(dimension < 0 for dimension in self.shape):
            raise ValueError("scientific array dimensions must be non-negative")
        expected_unit = _SCIENTIFIC_ARRAY_UNITS[self.kind]
        if self.unit != expected_unit:
            raise ValueError(f"{self.kind.value} arrays must use unit {expected_unit!r}")
        if (self.metadata_schema_version is None) != (self.array_metadata is None):
            raise ValueError("array metadata and its schema version must be set together")
        return self


class FrameEnergyResultRecord(BaseModel):
    """Canonical scalar projection of one MolOP ``Energies`` result."""

    model_config = ConfigDict(frozen=True)

    electronic_energy_hartree: EnergyHartree | None = None
    reference_energy_hartree: EnergyHartree | None = None
    mp2_energy_hartree: EnergyHartree | None = None
    mp3_energy_hartree: EnergyHartree | None = None
    mp4_energy_hartree: EnergyHartree | None = None
    mp5_energy_hartree: EnergyHartree | None = None
    ccsd_energy_hartree: EnergyHartree | None = None
    ccsd_t_energy_hartree: EnergyHartree | None = None
    source_schema_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_has_energy(self) -> Self:
        if not any(value is not None for name, value in self if name != "source_schema_version"):
            raise ValueError("a frame energy result must contain at least one energy")
        return self


class EnergyObservationRecord(BaseModel):
    """One ordered source-labeled energy fact emitted by the parser."""

    model_config = ConfigDict(frozen=True)

    observation_index: int = Field(ge=0)
    method: str = Field(min_length=1, max_length=128)
    quantity_semantics: EnergyQuantitySemantics
    value_hartree: EnergyHartree
    source_label: str = Field(min_length=1, max_length=256)


class GeometryOptimizationResultRecord(BaseModel):
    """One MolOP geometry-optimization status attached to its physical frame."""

    model_config = ConfigDict(frozen=True)

    geometry_optimized: bool | None = None
    convergence_multiplier: float = Field(default=2.0, ge=1)
    source_converged: dict[str, bool | None] | None = None
    source_labels: dict[str, str] | None = None
    energy_change_hartree: float | None = None
    energy_change_threshold_hartree: float | None = Field(default=None, ge=0)
    energy_change_converged: bool | None = None
    rms_force_hartree_per_bohr: float | None = None
    rms_force_threshold_hartree_per_bohr: float | None = Field(default=None, ge=0)
    rms_force_converged: bool | None = None
    max_force_hartree_per_bohr: float | None = None
    max_force_threshold_hartree_per_bohr: float | None = Field(default=None, ge=0)
    max_force_converged: bool | None = None
    rms_displacement_bohr: float | None = None
    rms_displacement_threshold_bohr: float | None = Field(default=None, ge=0)
    rms_displacement_converged: bool | None = None
    max_displacement_bohr: float | None = None
    max_displacement_threshold_bohr: float | None = Field(default=None, ge=0)
    max_displacement_converged: bool | None = None
    source_schema_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_metric_evidence(self) -> Self:
        for value_name, threshold_name, converged_name in _OPTIMIZATION_METRICS:
            if getattr(self, converged_name) is not None and (
                getattr(self, value_name) is None or getattr(self, threshold_name) is None
            ):
                raise ValueError(f"{converged_name} requires its metric value and threshold")
        return self


class VibrationResultRecord(BaseModel):
    """Semantic metadata and summary for one MolOP ``Vibrations`` result."""

    model_config = ConfigDict(frozen=True)

    mode_count: int = Field(ge=0)
    imaginary_mode_count: int = Field(ge=0)
    lowest_frequency_cm1: float | None = None
    mode_indices: list[int]
    axis_order: list[str] | None = None
    atom_order: str | None = Field(default=None, max_length=32)
    normalization: str | None = Field(default=None, max_length=64)
    mass_weighting: str | None = Field(default=None, max_length=64)
    source_schema_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_modes(self) -> Self:
        if self.imaginary_mode_count > self.mode_count:
            raise ValueError("imaginary_mode_count cannot exceed mode_count")
        if len(self.mode_indices) != self.mode_count:
            raise ValueError("mode_indices must contain one source index per mode")
        if self.mode_indices != sorted(set(self.mode_indices)):
            raise ValueError("mode_indices must be sorted and unique")
        if self.mode_count == 0 and self.lowest_frequency_cm1 is not None:
            raise ValueError("an empty vibration result cannot have a lowest frequency")
        if self.mode_count > 0 and self.lowest_frequency_cm1 is None:
            raise ValueError("a non-empty vibration result requires a lowest frequency")
        return self


class CalculationStatusResultRecord(BaseModel):
    """Direct projection of one MolOP frame ``Status`` result."""

    model_config = ConfigDict(frozen=True)

    scf_converged: bool | None = None
    normal_terminated: bool | None = None
    source_schema_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_has_status(self) -> Self:
        if self.scf_converged is None and self.normal_terminated is None:
            raise ValueError("a calculation status result must contain source status evidence")
        return self


class ThermochemistryResultRecord(BaseModel):
    """Canonical scalar thermochemistry at explicit temperature and pressure."""

    model_config = ConfigDict(frozen=True)

    temperature_kelvin: float = Field(gt=0)
    pressure_atm: float = Field(gt=0)
    zpe_correction_hartree: EnergyHartree | None = None
    thermal_energy_correction_hartree: EnergyHartree | None = None
    thermal_enthalpy_correction_hartree: EnergyHartree | None = None
    thermal_gibbs_correction_hartree: EnergyHartree | None = None
    zero_point_energy_hartree: EnergyHartree | None = None
    thermal_internal_energy_hartree: EnergyHartree | None = None
    enthalpy_hartree: EnergyHartree | None = None
    gibbs_free_energy_hartree: EnergyHartree | None = None
    entropy_cal_mol_k: float | None = None
    heat_capacity_cv_cal_mol_k: float | None = Field(default=None, ge=0)
    molecular_mass_amu: float | None = Field(default=None, gt=0)
    rotational_symmetry_number: int | None = Field(default=None, ge=1)
    source_schema_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_has_value(self) -> Self:
        if not any(
            getattr(self, field_name) is not None for field_name in _THERMOCHEMISTRY_VALUE_FIELDS
        ):
            raise ValueError("a thermochemistry result must contain at least one result value")
        return self


class MolecularOrbitalResultRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    electronic_state: str | None = Field(default=None, max_length=128)
    alpha_orbital_count: int = Field(ge=0)
    beta_orbital_count: int = Field(ge=0)
    coefficient_count: int = Field(ge=0)
    alpha_occupancies: list[float | None] = Field(default_factory=list)
    beta_occupancies: list[float | None] = Field(default_factory=list)
    alpha_symmetries: list[str | None] = Field(default_factory=list)
    beta_symmetries: list[str | None] = Field(default_factory=list)
    source_schema_version: str = Field(min_length=1, max_length=64)


class ChargeSpinPopulationResultRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    series_count: int = Field(ge=0)
    source_schema_version: str = Field(min_length=1, max_length=64)


class AtomicPopulationSeriesRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    series_key: str = Field(min_length=1, max_length=128)
    scheme: str = Field(min_length=1, max_length=128)
    quantity: str = Field(min_length=1, max_length=128)
    value_count: int = Field(gt=0)
    spin_channel: str | None = Field(default=None, pattern=r"^(alpha|beta|total)$")
    source_label: str | None = None
    series_metadata: dict[str, Any] = Field(default_factory=dict)


class PolarizabilityResultRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    electronic_spatial_extent_bohr2: float | None = None
    isotropic_polarizability_bohr3: float | None = None
    anisotropic_polarizability_bohr3: float | None = None
    source_schema_version: str = Field(min_length=1, max_length=64)


class NMRResultRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    gauge: str | None = Field(default=None, max_length=128)
    shielding_count: int = Field(ge=0)
    coupling_atom_indices: list[int] = Field(default_factory=list)
    source_schema_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_coupling_indices(self) -> Self:
        if self.coupling_atom_indices != list(dict.fromkeys(self.coupling_atom_indices)):
            raise ValueError("NMR coupling atom indices must be ordered and unique")
        if any(index < 0 for index in self.coupling_atom_indices):
            raise ValueError("NMR coupling atom indices must be non-negative")
        return self


class NMRShieldingTensorRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    atom_index: int = Field(ge=0)
    atom_symbol: str = Field(min_length=1, max_length=8)
    isotropic_ppm: float | None = None
    anisotropy_ppm: float | None = None
    anisotropy_convention: str | None = Field(default=None, max_length=64)
    orientation: str = Field(pattern=r"^(input|standard|source|unknown)$")


class BondOrderResultRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    matrix_count: int = Field(ge=0)
    source_schema_version: str = Field(min_length=1, max_length=64)


class TotalSpinResultRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    spin_square: float | None = None
    spin_quantum_number: float | None = None
    source_schema_version: str = Field(min_length=1, max_length=64)


class SinglePointPropertyResultRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    vertical_ionization_potential_ev: float | None = None
    vertical_electron_affinity_ev: float | None = None
    global_electrophilicity_index_ev: float | None = None
    source_schema_version: str = Field(min_length=1, max_length=64)


class ElectronicStateSetRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ElectronicStateSetKind
    state_count: int = Field(ge=0)
    source_schema_version: str = Field(min_length=1, max_length=64)


class ElectronicStateRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    set_kind: ElectronicStateSetKind
    state_ordinal: int = Field(ge=0)
    state_index: int | None = Field(default=None, ge=0)
    root: int | None = Field(default=None, ge=0)
    label: str | None = Field(default=None, max_length=256)
    multiplicity: int | None = Field(default=None, gt=0)
    spin: float | None = None
    irrep: str | None = Field(default=None, max_length=128)
    method: str | None = Field(default=None, max_length=128)
    energy_hartree: EnergyHartree | None = None
    excitation_energy_ev: float | None = None
    oscillator_strength: float | None = None
    state_properties: dict[str, Any] = Field(default_factory=dict)
    source: str | None = None


class ElectronicConfigurationRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    set_kind: ElectronicStateSetKind
    state_ordinal: int = Field(ge=0)
    configuration_ordinal: int = Field(ge=0)
    label: str | None = Field(default=None, max_length=256)
    coefficient: float | None = None
    weight: float | None = None
    occupation: list[float] = Field(default_factory=list)
    orbital_indices: list[int] = Field(default_factory=list)
    raw: str = ""


class MultireferenceResultRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    method: str | None = Field(default=None, max_length=128)
    reference_method: str | None = Field(default=None, max_length=128)
    ci_type: str | None = Field(default=None, max_length=128)
    active_space_electrons: int | None = Field(default=None, ge=0)
    active_space_orbitals: int | None = Field(default=None, ge=0)
    active_space_roots: int | None = Field(default=None, ge=0)
    active_orbitals: list[int] = Field(default_factory=list)
    inactive_orbitals: list[int] = Field(default_factory=list)
    frozen_orbitals: list[int] = Field(default_factory=list)
    active_space_raw: str = ""
    active_space_options: dict[str, Any] = Field(default_factory=dict)
    corrections: dict[str, Any] = Field(default_factory=dict)
    diagnostics: list[str] = Field(default_factory=list)
    result_properties: dict[str, Any] = Field(default_factory=dict)
    source_schema_version: str = Field(min_length=1, max_length=64)


class ImplicitSolvationResultRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    solvent: str | None = Field(default=None, max_length=128)
    solvent_model: str | None = Field(default=None, max_length=128)
    atomic_radii: str | None = Field(default=None, max_length=128)
    solvent_epsilon: float | None = Field(default=None, gt=0)
    solvent_epsilon_infinite: float | None = Field(default=None, gt=0)
    source_schema_version: str = Field(min_length=1, max_length=64)


class ScientificArrayAssignmentRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    array_kind: ScientificArrayKind
    array_ordinal: int = Field(ge=0)
    owner_kind: ScientificArrayOwnerKind
    owner_key: str | None = Field(default=None, max_length=128)
    slot: str = Field(min_length=1, max_length=128)
    slot_ordinal: int = Field(default=0, ge=0)


__all__ = [
    "AtomicPopulationSeriesRecord",
    "BondOrderResultRecord",
    "ChargeSpinPopulationResultRecord",
    "CalculationStatusResultRecord",
    "CalculationFrameRecord",
    "CalculationSegmentRecord",
    "EnergyObservationRecord",
    "ElectronicConfigurationRecord",
    "ElectronicStateRecord",
    "ElectronicStateSetRecord",
    "FrameEnergyResultRecord",
    "GeometryOptimizationResultRecord",
    "ImplicitSolvationResultRecord",
    "MolecularOrbitalResultRecord",
    "MultireferenceResultRecord",
    "NMRResultRecord",
    "NMRShieldingTensorRecord",
    "ParseRevisionCompletionRecord",
    "ParseRevisionRecord",
    "ScientificArrayRecord",
    "ScientificArrayAssignmentRecord",
    "SinglePointPropertyResultRecord",
    "ThermochemistryResultRecord",
    "TotalSpinResultRecord",
    "PolarizabilityResultRecord",
    "VibrationResultRecord",
]
