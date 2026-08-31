"""Versioned parser output, calculation frames, and numerical results."""

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID

import numpy as np
import numpy.typing as npt
from pydantic import ConfigDict
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, SMALLINT
from sqlalchemy.orm import deferred
from sqlmodel import Field, Relationship, SQLModel

from tricycle_reaction_db.db.models.base import created_at_field, uuid_primary_key_field
from tricycle_reaction_db.db.types import NumpyArray
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
    SelectedEnergyKind,
    SourceFormat,
    TerminationStatus,
    string_enum,
)
from tricycle_reaction_db.domain.precision import ENERGY_HARTREE_DECIMAL_PLACES

if TYPE_CHECKING:
    from tricycle_reaction_db.db.models.artifacts import ArtifactFile, CalculationProtocol
    from tricycle_reaction_db.db.models.chemistry import Geometry, MolecularTopologyDerivation
    from tricycle_reaction_db.db.models.uploads import (
        TransitionStateEndpoint,
        TransitionStateInference,
    )

_HASH_PATTERN = "^[0-9a-f]{64}$"


class ScalarHartree(Numeric[float]):
    """Fixed-scale storage for total and correction Hartree energies."""

    def __init__(self) -> None:
        super().__init__(
            precision=24,
            scale=ENERGY_HARTREE_DECIMAL_PLACES,
            asdecimal=False,
        )


class ParseRevision(SQLModel, table=True):
    """One immutable, reproducible parse attempt for an artifact."""

    __tablename__ = "parse_revision"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "artifact_file_id",
            "revision_number",
            name="uq_parse_revision_artifact_number",
        ),
        CheckConstraint("revision_number >= 1", name="ck_parse_revision_number_positive"),
        CheckConstraint(
            f"parser_provenance_hash ~ '{_HASH_PATTERN}'",
            name="ck_parse_revision_provenance_hash_hex",
        ),
        CheckConstraint(
            f"parser_config_hash ~ '{_HASH_PATTERN}'",
            name="ck_parse_revision_parser_config_hash_hex",
        ),
        CheckConstraint(
            f"reconstruction_config_hash ~ '{_HASH_PATTERN}'",
            name="ck_parse_revision_reconstruction_config_hash_hex",
        ),
        CheckConstraint(
            f"record_sha256 IS NULL OR record_sha256 ~ '{_HASH_PATTERN}'",
            name="ck_parse_revision_record_hash_hex",
        ),
        CheckConstraint(
            "completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at",
            name="ck_parse_revision_timestamps_ordered",
        ),
        CheckConstraint(
            "running_time_seconds IS NULL OR running_time_seconds >= 0",
            name="ck_parse_revision_running_time_nonnegative",
        ),
        CheckConstraint(
            "status <> 'succeeded' OR (record_sha256 IS NOT NULL AND completed_at IS NOT NULL)",
            name="ck_parse_revision_succeeded_payload",
        ),
        Index(
            "ix_parse_revision_identity_lookup",
            "artifact_file_id",
            "export_schema_version",
            "parser_provenance_hash",
            "parser_config_hash",
            "reconstruction_config_hash",
            "revision_number",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    artifact_file_id: UUID = Field(
        foreign_key="artifact_file.id",
        ondelete="RESTRICT",
        index=True,
        nullable=False,
    )
    revision_number: int = Field(default=1, nullable=False)
    reparse_of_id: UUID | None = Field(
        default=None,
        foreign_key="parse_revision.id",
        ondelete="RESTRICT",
        index=True,
        nullable=True,
    )
    export_schema_version: str = Field(max_length=64, nullable=False)
    parser_name: str = Field(
        default="molop",
        sa_column=Column(String(64), nullable=False, server_default="molop"),
    )
    parser_version: str = Field(max_length=128, nullable=False)
    parser_id: str = Field(max_length=512, nullable=False)
    molop_version: str = Field(max_length=128, nullable=False)
    parser_commit: str | None = Field(default=None, max_length=128)
    molgr_version: str | None = Field(default=None, max_length=128)
    molgr_commit: str | None = Field(default=None, max_length=128)
    rdkit_version: str = Field(max_length=128, nullable=False)
    parser_provenance: dict[str, Any] = Field(
        sa_column=Column(JSONB, nullable=False),
    )
    parser_provenance_hash: str = Field(max_length=64, nullable=False)
    parser_config_hash: str = Field(max_length=64, nullable=False)
    reconstruction_config_hash: str = Field(max_length=64, nullable=False)
    source_format: SourceFormat = Field(
        sa_column=Column(
            string_enum(SourceFormat, name="parse_revision_source_format"),
            nullable=False,
        )
    )
    source_encoding: str = Field(max_length=64, nullable=False)
    source_content_sha256: str | None = Field(default=None, max_length=64)
    source_size_bytes: int | None = Field(default=None, sa_type=BigInteger)
    source_compression: str | None = Field(default=None, max_length=32)
    # Runtime reported by the quantum-chemistry file as a file-level fact.
    # Frame-level ``running_time_seconds`` remains the per-frame counterpart.
    running_time_seconds: float | None = Field(default=None, sa_type=Float)
    source_complete: bool | None = Field(default=None)
    parse_completeness: ParseCompleteness = Field(
        default=ParseCompleteness.NOT_ASSESSED,
        sa_column=Column(
            string_enum(ParseCompleteness, name="parse_revision_parse_completeness"),
            nullable=False,
            server_default=ParseCompleteness.NOT_ASSESSED.value,
        ),
    )
    parse_diagnostics: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default="[]"),
    )
    record_sha256: str | None = Field(default=None, max_length=64)
    status: ParseStatus = Field(
        default=ParseStatus.PENDING,
        sa_column=Column(
            string_enum(ParseStatus, name="parse_revision_status"),
            nullable=False,
            server_default=ParseStatus.PENDING.value,
            index=True,
        ),
    )
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, sa_type=Text)
    error_metadata: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB(none_as_null=True), nullable=True),
    )
    started_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    artifact_file: "ArtifactFile" = Relationship(back_populates="parse_revisions")
    reparse_of: Optional["ParseRevision"] = Relationship(
        back_populates="reparses",
        sa_relationship_kwargs={"remote_side": "ParseRevision.id"},
    )
    reparses: list["ParseRevision"] = Relationship(back_populates="reparse_of")
    segments: list["CalculationSegment"] = Relationship(
        back_populates="parse_revision",
        cascade_delete=True,
        passive_deletes=True,
    )
    transition_state_inferences: list["TransitionStateInference"] = Relationship(
        back_populates="parse_revision",
        cascade_delete=True,
        passive_deletes=True,
    )


class CalculationSegment(SQLModel, table=True):
    """One source-local calculation job, restart, or linked program section."""

    __tablename__ = "calculation_segment"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "parse_revision_id",
            "segment_index",
            name="uq_calculation_segment_revision_index",
        ),
        UniqueConstraint(
            "id",
            "parse_revision_id",
            name="uq_calculation_segment_id_revision",
        ),
        CheckConstraint("segment_index >= 0", name="ck_calculation_segment_index_nonnegative"),
        CheckConstraint(
            "source_start_byte >= 0 AND source_end_byte > source_start_byte",
            name="ck_calculation_segment_byte_span",
        ),
        CheckConstraint(
            "num_nonnulls(source_start_char, source_end_char) = 0 OR "
            "(num_nonnulls(source_start_char, source_end_char) = 2 AND "
            "source_start_char >= 0 AND source_end_char > source_start_char)",
            name="ck_calculation_segment_char_span",
        ),
        CheckConstraint(
            "source_start_line >= 1 AND source_end_line > source_start_line",
            name="ck_calculation_segment_line_span",
        ),
        CheckConstraint(
            f"source_block_sha256 ~ '{_HASH_PATTERN}'",
            name="ck_calculation_segment_block_hash_hex",
        ),
        CheckConstraint(
            "requested_cpu_count IS NULL OR requested_cpu_count > 0",
            name="ck_calculation_segment_cpu_positive",
        ),
        CheckConstraint(
            "requested_memory_mb IS NULL OR requested_memory_mb > 0",
            name="ck_calculation_segment_memory_positive",
        ),
        CheckConstraint(
            "wall_time_seconds IS NULL OR wall_time_seconds >= 0",
            name="ck_calculation_segment_wall_time_nonnegative",
        ),
        CheckConstraint(
            "source_frame_count IS NULL OR source_frame_count >= 0",
            name="ck_calculation_segment_source_frame_count_nonnegative",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    parse_revision_id: UUID = Field(
        foreign_key="parse_revision.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    protocol_id: UUID | None = Field(
        foreign_key="calculation_protocol.id",
        ondelete="RESTRICT",
        index=True,
        nullable=True,
    )
    segment_index: int = Field(nullable=False)
    segment_label: str | None = Field(default=None, sa_type=Text)
    source_start_byte: int | None = Field(sa_type=BigInteger, nullable=True)
    source_end_byte: int | None = Field(sa_type=BigInteger, nullable=True)
    source_start_char: int | None = Field(default=None, sa_type=BigInteger)
    source_end_char: int | None = Field(default=None, sa_type=BigInteger)
    source_start_line: int | None = Field(default=None, nullable=True)
    source_end_line: int | None = Field(default=None, nullable=True)
    source_block_sha256: str | None = Field(default=None, max_length=64, nullable=True)
    source_frame_count: int | None = Field(default=None)
    parse_presence: dict[str, str] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    parse_completeness: ParseCompleteness = Field(
        default=ParseCompleteness.NOT_ASSESSED,
        sa_column=Column(
            string_enum(ParseCompleteness, name="calculation_segment_parse_completeness"),
            nullable=False,
            server_default=ParseCompleteness.NOT_ASSESSED.value,
        ),
    )
    parse_diagnostics: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default="[]"),
    )
    requested_cpu_count: int | None = Field(default=None)
    requested_memory_mb: int | None = Field(default=None, sa_type=BigInteger)
    termination_status: TerminationStatus = Field(
        default=TerminationStatus.UNKNOWN,
        sa_column=Column(
            string_enum(TerminationStatus, name="calculation_segment_termination_status"),
            nullable=False,
            server_default=TerminationStatus.UNKNOWN.value,
        ),
    )
    scf_status: SCFStatus = Field(
        default=SCFStatus.UNKNOWN,
        sa_column=Column(
            string_enum(SCFStatus, name="calculation_segment_scf_status"),
            nullable=False,
            server_default=SCFStatus.UNKNOWN.value,
        ),
    )
    wall_time_seconds: float | None = Field(default=None, sa_type=Float)
    program_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    parse_revision: ParseRevision = Relationship(back_populates="segments")
    protocol: Optional["CalculationProtocol"] = Relationship(back_populates="segments")
    frames: list["CalculationFrame"] = Relationship(
        back_populates="segment",
        cascade_delete=True,
        passive_deletes=True,
    )


_calculation_frame_observed_coordinates_column: Column[npt.NDArray[np.generic]] = Column(
    "observed_coordinates", NumpyArray(), nullable=False
)


class CalculationFrame(SQLModel, table=True):
    """One ordered physical geometry occurrence in a calculation artifact."""

    model_config = ConfigDict(arbitrary_types_allowed=True)  # type: ignore[assignment]

    __tablename__ = "calculation_frame"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        ForeignKeyConstraint(
            ("segment_id", "parse_revision_id"),
            ("calculation_segment.id", "calculation_segment.parse_revision_id"),
            ondelete="CASCADE",
            name="fk_calculation_frame_segment_revision",
        ),
        UniqueConstraint(
            "segment_id",
            "frame_index",
            name="uq_calculation_frame_segment_index",
        ),
        UniqueConstraint(
            "parse_revision_id",
            "file_frame_index",
            name="uq_calculation_frame_revision_file_index",
        ),
        UniqueConstraint(
            "id",
            "geometry_id",
            name="uq_calculation_frame_id_geometry",
        ),
        CheckConstraint("frame_index >= 0", name="ck_calculation_frame_index_nonnegative"),
        CheckConstraint(
            "file_frame_index >= 0",
            name="ck_calculation_frame_file_index_nonnegative",
        ),
        CheckConstraint(
            "source_start_byte >= 0 AND source_end_byte > source_start_byte",
            name="ck_calculation_frame_byte_span",
        ),
        CheckConstraint(
            "num_nonnulls(source_start_char, source_end_char) = 0 OR "
            "(num_nonnulls(source_start_char, source_end_char) = 2 AND "
            "source_start_char >= 0 AND source_end_char > source_start_char)",
            name="ck_calculation_frame_char_span",
        ),
        CheckConstraint(
            "source_start_line >= 1 AND source_end_line > source_start_line",
            name="ck_calculation_frame_line_span",
        ),
        CheckConstraint(
            f"source_block_sha256 ~ '{_HASH_PATTERN}'",
            name="ck_calculation_frame_block_hash_hex",
        ),
        CheckConstraint("multiplicity > 0", name="ck_calculation_frame_multiplicity_positive"),
        CheckConstraint(
            "coordinate_decimal_places IS NULL OR coordinate_decimal_places BETWEEN 0 AND 18",
            name="ck_calculation_frame_coordinate_decimal_places",
        ),
        CheckConstraint(
            "electronic_state_kind = 'ground' AND electronic_state_index = 0",
            name="ck_calculation_frame_ground_state_v1",
        ),
        CheckConstraint(
            "num_nonnulls(observed_coordinates, observed_coordinate_hash, "
            "observed_to_geometry_atom_indices, observed_to_geometry_transform, "
            "geometry_assignment_rmsd_angstrom, geometry_assignment_max_abs_angstrom, "
            "geometry_assignment_policy_version) = 7",
            name="ck_calculation_frame_matched_geometry_evidence",
        ),
        CheckConstraint(
            "cardinality(observed_to_geometry_transform) = 16",
            name="ck_calculation_frame_observed_transform_length",
        ),
        CheckConstraint(
            "geometry_assignment_rmsd_angstrom IS NULL OR geometry_assignment_rmsd_angstrom >= 0",
            name="ck_calculation_frame_assignment_rmsd_nonnegative",
        ),
        CheckConstraint(
            "geometry_assignment_max_abs_angstrom IS NULL OR "
            "geometry_assignment_max_abs_angstrom >= geometry_assignment_rmsd_angstrom",
            name="ck_calculation_frame_assignment_max_abs_ge_rmsd",
        ),
        CheckConstraint(
            f"observed_coordinate_hash IS NULL OR observed_coordinate_hash ~ '{_HASH_PATTERN}'",
            name="ck_calculation_frame_observed_hash_hex",
        ),
        CheckConstraint(
            "num_nonnulls(selected_energy_hartree, selected_energy_kind, "
            "energy_selection_policy_version) IN (0, 3)",
            name="ck_calculation_frame_selected_energy_complete",
        ),
        CheckConstraint(
            "selected_energy_kind IS NULL OR selected_energy_hartree IS NOT DISTINCT FROM CASE "
            "WHEN selected_energy_kind = 'electronic_total_energy_hartree' "
            "THEN electronic_total_energy_hartree "
            "WHEN selected_energy_kind = 'reference_total_energy_hartree' "
            "THEN reference_total_energy_hartree "
            "WHEN selected_energy_kind = 'mp2_total_energy_hartree' "
            "THEN mp2_total_energy_hartree "
            "WHEN selected_energy_kind = 'mp3_total_energy_hartree' "
            "THEN mp3_total_energy_hartree "
            "WHEN selected_energy_kind = 'mp4_total_energy_hartree' "
            "THEN mp4_total_energy_hartree "
            "WHEN selected_energy_kind = 'mp5_total_energy_hartree' "
            "THEN mp5_total_energy_hartree "
            "WHEN selected_energy_kind = 'ccsd_total_energy_hartree' "
            "THEN ccsd_total_energy_hartree "
            "WHEN selected_energy_kind = 'ccsd_t_total_energy_hartree' "
            "THEN ccsd_t_total_energy_hartree END",
            name="ck_calculation_frame_selected_energy_matches_source",
        ),
        CheckConstraint(
            "energy_change_threshold_hartree IS NULL OR energy_change_threshold_hartree >= 0",
            name="ck_calculation_frame_energy_change_threshold",
        ),
        CheckConstraint(
            "rms_force_threshold_hartree_per_bohr IS NULL OR "
            "rms_force_threshold_hartree_per_bohr >= 0",
            name="ck_calculation_frame_rms_force_threshold",
        ),
        CheckConstraint(
            "max_force_threshold_hartree_per_bohr IS NULL OR "
            "max_force_threshold_hartree_per_bohr >= 0",
            name="ck_calculation_frame_max_force_threshold",
        ),
        CheckConstraint(
            "rms_displacement_threshold_bohr IS NULL OR rms_displacement_threshold_bohr >= 0",
            name="ck_calculation_frame_rms_displacement_threshold",
        ),
        CheckConstraint(
            "max_displacement_threshold_bohr IS NULL OR max_displacement_threshold_bohr >= 0",
            name="ck_calculation_frame_max_displacement_threshold",
        ),
        CheckConstraint(
            "energy_change_converged IS NULL OR "
            "num_nonnulls(energy_change_hartree, energy_change_threshold_hartree) = 2",
            name="ck_calculation_frame_energy_change_convergence_inputs",
        ),
        CheckConstraint(
            "rms_force_converged IS NULL OR "
            "num_nonnulls(rms_force_hartree_per_bohr, "
            "rms_force_threshold_hartree_per_bohr) = 2",
            name="ck_calculation_frame_rms_force_convergence_inputs",
        ),
        CheckConstraint(
            "max_force_converged IS NULL OR "
            "num_nonnulls(max_force_hartree_per_bohr, "
            "max_force_threshold_hartree_per_bohr) = 2",
            name="ck_calculation_frame_max_force_convergence_inputs",
        ),
        CheckConstraint(
            "rms_displacement_converged IS NULL OR "
            "num_nonnulls(rms_displacement_bohr, rms_displacement_threshold_bohr) = 2",
            name="ck_calculation_frame_rms_displacement_convergence_inputs",
        ),
        CheckConstraint(
            "max_displacement_converged IS NULL OR "
            "num_nonnulls(max_displacement_bohr, max_displacement_threshold_bohr) = 2",
            name="ck_calculation_frame_max_displacement_convergence_inputs",
        ),
        CheckConstraint(
            "frequency_count IS NULL OR frequency_count >= 0",
            name="ck_calculation_frame_frequency_count_nonnegative",
        ),
        CheckConstraint(
            "negative_frequency_count IS NULL OR negative_frequency_count >= 0",
            name="ck_calculation_frame_negative_frequency_count_nonnegative",
        ),
        CheckConstraint(
            "frequency_count IS NULL OR negative_frequency_count IS NULL OR "
            "negative_frequency_count <= frequency_count",
            name="ck_calculation_frame_negative_frequency_count_lte_total",
        ),
        CheckConstraint(
            "running_time_seconds IS NULL OR running_time_seconds >= 0",
            name="ck_calculation_frame_running_time_nonnegative",
        ),
        CheckConstraint(
            "(frequency_count IS NULL AND negative_frequency_count IS NULL AND "
            "lowest_frequency_cm1 IS NULL) OR "
            "(frequency_count = 0 AND negative_frequency_count IS NOT DISTINCT FROM 0 AND "
            "lowest_frequency_cm1 IS NULL) OR "
            "(frequency_count > 0 AND negative_frequency_count IS NOT NULL AND "
            "lowest_frequency_cm1 IS NOT NULL)",
            name="ck_calculation_frame_frequency_summary_complete",
        ),
        Index(
            "ix_calculation_frame_frequency_counts",
            "frequency_count",
            "negative_frequency_count",
        ),
        Index(
            "ix_calculation_frame_converged_geometry",
            "geometry_id",
            postgresql_where=text("optimization_status = 'converged'"),
        ),
        Index(
            "ix_calculation_frame_parse_revision_visibility",
            "parse_revision_id",
            postgresql_include=("id", "geometry_id"),
        ),
        Index(
            "ix_calculation_frame_geometry_revision",
            "geometry_id",
            "parse_revision_id",
            postgresql_include=("id", "frequency_count", "negative_frequency_count"),
        ),
    )
    __mapper_args__ = {
        "properties": {
            "observed_coordinates": deferred(
                _calculation_frame_observed_coordinates_column,
                raiseload=True,
            ),
        }
    }

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    parse_revision_id: UUID = Field(index=True, nullable=False)
    segment_id: UUID = Field(index=True, nullable=False)
    frame_index: int = Field(nullable=False)
    file_frame_index: int = Field(nullable=False)
    frame_role: FrameRole = Field(
        sa_column=Column(
            string_enum(FrameRole, name="calculation_frame_role"),
            nullable=False,
            index=True,
        )
    )
    source_start_byte: int | None = Field(sa_type=BigInteger, nullable=True)
    source_end_byte: int | None = Field(sa_type=BigInteger, nullable=True)
    source_start_char: int | None = Field(default=None, sa_type=BigInteger)
    source_end_char: int | None = Field(default=None, sa_type=BigInteger)
    source_start_line: int | None = Field(default=None, nullable=True)
    source_end_line: int | None = Field(default=None, nullable=True)
    source_block_sha256: str | None = Field(default=None, max_length=64, nullable=True)
    parse_presence: dict[str, str] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    parse_completeness: ParseCompleteness = Field(
        default=ParseCompleteness.NOT_ASSESSED,
        sa_column=Column(
            string_enum(ParseCompleteness, name="calculation_frame_parse_completeness"),
            nullable=False,
            server_default=ParseCompleteness.NOT_ASSESSED.value,
        ),
    )
    parse_diagnostics: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default="[]"),
    )
    geometry_id: UUID = Field(
        foreign_key="geometry.id",
        ondelete="RESTRICT",
        index=True,
        nullable=False,
    )
    topology_derivation_id: UUID = Field(
        foreign_key="molecular_topology_derivation.id",
        ondelete="RESTRICT",
        index=True,
        nullable=False,
    )
    charge: int = Field(sa_type=SMALLINT, nullable=False)
    multiplicity: int = Field(sa_type=SMALLINT, nullable=False)
    coordinate_decimal_places: int | None = Field(default=None, sa_type=SMALLINT)
    geometry_assignment_kind: GeometryAssignmentKind = Field(
        sa_column=Column(
            string_enum(
                GeometryAssignmentKind,
                name="calculation_frame_geometry_assignment_kind",
                length=26,
            ),
            nullable=False,
        )
    )
    observed_coordinates: npt.NDArray[np.generic] = Field(
        sa_column=_calculation_frame_observed_coordinates_column
    )
    observed_coordinate_hash: str = Field(max_length=64, nullable=False)
    observed_to_geometry_atom_indices: list[int] = Field(
        sa_column=Column(ARRAY(Integer, dimensions=1), nullable=False),
    )
    observed_to_geometry_transform: list[float] = Field(
        sa_column=Column(ARRAY(Float, dimensions=1), nullable=False),
    )
    geometry_assignment_rmsd_angstrom: float = Field(sa_type=Float, nullable=False)
    geometry_assignment_max_abs_angstrom: float = Field(sa_type=Float, nullable=False)
    geometry_assignment_policy_version: str = Field(max_length=64, nullable=False)
    electronic_state_kind: ElectronicStateKind = Field(
        default=ElectronicStateKind.GROUND,
        sa_column=Column(
            string_enum(ElectronicStateKind, name="calculation_frame_electronic_state_kind"),
            nullable=False,
            server_default=ElectronicStateKind.GROUND.value,
        ),
    )
    electronic_state_index: int = Field(
        default=0,
        sa_column=Column(SMALLINT, nullable=False, server_default="0"),
    )
    scf_status: SCFStatus = Field(
        default=SCFStatus.UNKNOWN,
        sa_column=Column(
            string_enum(SCFStatus, name="calculation_frame_scf_status"),
            nullable=False,
            server_default=SCFStatus.UNKNOWN.value,
        ),
    )
    optimization_status: OptimizationStatus = Field(
        default=OptimizationStatus.UNKNOWN,
        sa_column=Column(
            string_enum(OptimizationStatus, name="calculation_frame_optimization_status"),
            nullable=False,
            server_default=OptimizationStatus.UNKNOWN.value,
        ),
    )
    electronic_total_energy_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    reference_total_energy_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    mp2_total_energy_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    mp3_total_energy_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    mp4_total_energy_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    mp5_total_energy_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    ccsd_total_energy_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    ccsd_t_total_energy_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    selected_energy_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    selected_energy_kind: SelectedEnergyKind | None = Field(
        default=None,
        sa_column=Column(
            string_enum(SelectedEnergyKind, name="calculation_frame_selected_energy_kind"),
            nullable=True,
        ),
    )
    energy_selection_policy_version: str | None = Field(default=None, max_length=64)
    energy_change_hartree: float | None = Field(default=None, sa_type=Float)
    energy_change_threshold_hartree: float | None = Field(default=None, sa_type=Float)
    energy_change_converged: bool | None = Field(default=None)
    rms_force_hartree_per_bohr: float | None = Field(default=None, sa_type=Float)
    rms_force_threshold_hartree_per_bohr: float | None = Field(default=None, sa_type=Float)
    rms_force_converged: bool | None = Field(default=None)
    max_force_hartree_per_bohr: float | None = Field(default=None, sa_type=Float)
    max_force_threshold_hartree_per_bohr: float | None = Field(default=None, sa_type=Float)
    max_force_converged: bool | None = Field(default=None)
    rms_displacement_bohr: float | None = Field(default=None, sa_type=Float)
    rms_displacement_threshold_bohr: float | None = Field(default=None, sa_type=Float)
    rms_displacement_converged: bool | None = Field(default=None)
    max_displacement_bohr: float | None = Field(default=None, sa_type=Float)
    max_displacement_threshold_bohr: float | None = Field(default=None, sa_type=Float)
    max_displacement_converged: bool | None = Field(default=None)
    running_time_seconds: float | None = Field(default=None, sa_type=Float)
    frequency_count: int | None = Field(default=None)
    negative_frequency_count: int | None = Field(default=None)
    lowest_frequency_cm1: float | None = Field(default=None, sa_type=Float)
    program_metadata_schema_version: str = Field(
        default="calculation-frame-metadata-v1",
        sa_column=Column(
            String(64),
            nullable=False,
            server_default="calculation-frame-metadata-v1",
        ),
    )
    program_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    segment: CalculationSegment = Relationship(back_populates="frames")
    geometry: "Geometry" = Relationship(back_populates="calculation_frames")
    topology_derivation: "MolecularTopologyDerivation" = Relationship(
        back_populates="calculation_frames"
    )
    scientific_arrays: list["ScientificArray"] = Relationship(
        back_populates="frame",
        cascade_delete=True,
        passive_deletes=True,
    )
    energy_result: Optional["FrameEnergyResult"] = Relationship(
        back_populates="frame",
        cascade_delete=True,
        passive_deletes=True,
    )
    optimization_result: Optional["GeometryOptimizationResult"] = Relationship(
        back_populates="frame",
        cascade_delete=True,
        passive_deletes=True,
    )
    vibration_result: Optional["VibrationResult"] = Relationship(
        back_populates="frame",
        cascade_delete=True,
        passive_deletes=True,
    )
    status_result: Optional["CalculationStatusResult"] = Relationship(
        back_populates="frame",
        cascade_delete=True,
        passive_deletes=True,
    )
    thermochemistry_result: Optional["ThermochemistryResult"] = Relationship(
        back_populates="frame",
        cascade_delete=True,
        passive_deletes=True,
    )
    molecular_orbital_result: Optional["MolecularOrbitalResult"] = Relationship(
        back_populates="frame", cascade_delete=True, passive_deletes=True
    )
    charge_spin_population_result: Optional["ChargeSpinPopulationResult"] = Relationship(
        back_populates="frame", cascade_delete=True, passive_deletes=True
    )
    polarizability_result: Optional["PolarizabilityResult"] = Relationship(
        back_populates="frame", cascade_delete=True, passive_deletes=True
    )
    nmr_result: Optional["NMRResult"] = Relationship(
        back_populates="frame", cascade_delete=True, passive_deletes=True
    )
    bond_order_result: Optional["BondOrderResult"] = Relationship(
        back_populates="frame", cascade_delete=True, passive_deletes=True
    )
    total_spin_result: Optional["TotalSpinResult"] = Relationship(
        back_populates="frame", cascade_delete=True, passive_deletes=True
    )
    single_point_property_result: Optional["SinglePointPropertyResult"] = Relationship(
        back_populates="frame", cascade_delete=True, passive_deletes=True
    )
    electronic_state_sets: list["ElectronicStateSet"] = Relationship(
        back_populates="frame", cascade_delete=True, passive_deletes=True
    )
    multireference_result: Optional["MultireferenceResult"] = Relationship(
        back_populates="frame", cascade_delete=True, passive_deletes=True
    )
    implicit_solvation_result: Optional["ImplicitSolvationResult"] = Relationship(
        back_populates="frame", cascade_delete=True, passive_deletes=True
    )
    transition_state_endpoints: list["TransitionStateEndpoint"] = Relationship(
        back_populates="calculation_frame", cascade_delete=True, passive_deletes=True
    )


class ProjectGeometryCatalog(SQLModel, table=True):
    """Project-visible geometry membership and list-query summary fields."""

    __tablename__ = "project_geometry_catalog"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        CheckConstraint("frame_count > 0", name="ck_project_geometry_catalog_frame_count"),
        Index(
            "ix_project_geometry_catalog_created_page",
            "project_id",
            "geometry_created_at",
            "geometry_id",
        ),
        Index(
            "ix_project_geometry_catalog_thermodynamic_page",
            "project_id",
            "geometry_created_at",
            "geometry_id",
            postgresql_where=text("has_thermodynamic_property"),
        ),
        Index(
            "ix_project_geometry_catalog_nonthermodynamic_page",
            "project_id",
            "geometry_created_at",
            "geometry_id",
            postgresql_where=text("NOT has_thermodynamic_property"),
        ),
        Index(
            "ix_project_geometry_catalog_frame_count_asc",
            "project_id",
            "frame_count",
            "geometry_id",
        ),
        Index(
            "ix_project_geometry_catalog_frame_count_desc",
            "project_id",
            text("frame_count DESC NULLS LAST"),
            text("geometry_id ASC"),
        ),
        Index(
            "ix_project_geometry_catalog_thermo_frame_count_asc",
            "project_id",
            "frame_count",
            "geometry_id",
            postgresql_where=text("has_thermodynamic_property"),
        ),
        Index(
            "ix_project_geometry_catalog_thermo_frame_count_desc",
            "project_id",
            text("frame_count DESC NULLS LAST"),
            text("geometry_id ASC"),
            postgresql_where=text("has_thermodynamic_property"),
        ),
    )

    project_id: UUID = Field(primary_key=True, nullable=False)
    geometry_id: UUID = Field(primary_key=True, nullable=False)
    frame_count: int = Field(sa_type=BigInteger, nullable=False)
    geometry_created_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("now()"),
        ),
    )
    has_frequency_data: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    has_imaginary_frequency: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    has_thermodynamic_property: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )


class ProjectGeometryCatalogCount(SQLModel, table=True):
    """Exact Geometry catalogue cardinality maintained per project."""

    __tablename__ = "project_geometry_catalog_count"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        CheckConstraint(
            "geometry_count >= 0",
            name="ck_project_geometry_catalog_count_nonnegative",
        ),
    )

    project_id: UUID = Field(primary_key=True, nullable=False)
    geometry_count: int = Field(sa_type=BigInteger, nullable=False)


_scientific_array_data_column: Column[npt.NDArray[np.generic]] = Column(
    "data", NumpyArray(), nullable=False
)


class FrameEnergyResult(SQLModel, table=True):
    """Canonical scalar projection of one MolOP ``Energies`` result."""

    __tablename__ = "frame_energy_result"  # pyright: ignore[reportAssignmentType]

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    frame_id: UUID = Field(
        foreign_key="calculation_frame.id",
        ondelete="CASCADE",
        unique=True,
        nullable=False,
    )
    electronic_energy_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    reference_energy_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    mp2_energy_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    mp3_energy_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    mp4_energy_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    mp5_energy_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    ccsd_energy_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    ccsd_t_energy_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    source_schema_version: str = Field(max_length=64, nullable=False)
    frame: CalculationFrame = Relationship(back_populates="energy_result")
    observations: list["EnergyObservation"] = Relationship(
        back_populates="energy_result",
        cascade_delete=True,
        passive_deletes=True,
    )


class EnergyObservation(SQLModel, table=True):
    """One ordered, source-labeled observation owned by an energy result."""

    __tablename__ = "energy_observation"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "energy_result_id",
            "observation_index",
            name="uq_energy_observation_result_index",
        ),
        CheckConstraint(
            "observation_index >= 0",
            name="ck_energy_observation_index_nonnegative",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    energy_result_id: UUID = Field(
        foreign_key="frame_energy_result.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    observation_index: int = Field(sa_type=SMALLINT, nullable=False)
    method: str = Field(max_length=128, index=True, nullable=False)
    quantity_semantics: EnergyQuantitySemantics = Field(
        sa_column=Column(
            string_enum(EnergyQuantitySemantics, name="energy_observation_quantity_semantics"),
            nullable=False,
            index=True,
        )
    )
    value_hartree: float = Field(sa_type=ScalarHartree, nullable=False)
    source_label: str = Field(max_length=256, nullable=False)
    energy_result: FrameEnergyResult = Relationship(back_populates="observations")


class GeometryOptimizationResult(SQLModel, table=True):
    """One MolOP geometry-optimization status owned by a calculation frame."""

    __tablename__ = "geometry_optimization_result"  # pyright: ignore[reportAssignmentType]

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    frame_id: UUID = Field(
        foreign_key="calculation_frame.id",
        ondelete="CASCADE",
        unique=True,
        nullable=False,
    )
    geometry_optimized: bool | None = Field(default=None)
    convergence_multiplier: float = Field(default=2.0, sa_type=Float, nullable=False)
    source_converged: dict[str, bool | None] | None = Field(
        default=None,
        sa_column=Column(JSONB(none_as_null=True), nullable=True),
    )
    source_labels: dict[str, str] | None = Field(
        default=None,
        sa_column=Column(JSONB(none_as_null=True), nullable=True),
    )
    energy_change_hartree: float | None = Field(default=None, sa_type=Float)
    energy_change_threshold_hartree: float | None = Field(default=None, sa_type=Float)
    energy_change_converged: bool | None = Field(default=None)
    rms_force_hartree_per_bohr: float | None = Field(default=None, sa_type=Float)
    rms_force_threshold_hartree_per_bohr: float | None = Field(default=None, sa_type=Float)
    rms_force_converged: bool | None = Field(default=None)
    max_force_hartree_per_bohr: float | None = Field(default=None, sa_type=Float)
    max_force_threshold_hartree_per_bohr: float | None = Field(default=None, sa_type=Float)
    max_force_converged: bool | None = Field(default=None)
    rms_displacement_bohr: float | None = Field(default=None, sa_type=Float)
    rms_displacement_threshold_bohr: float | None = Field(default=None, sa_type=Float)
    rms_displacement_converged: bool | None = Field(default=None)
    max_displacement_bohr: float | None = Field(default=None, sa_type=Float)
    max_displacement_threshold_bohr: float | None = Field(default=None, sa_type=Float)
    max_displacement_converged: bool | None = Field(default=None)
    source_schema_version: str = Field(max_length=64, nullable=False)
    frame: CalculationFrame = Relationship(back_populates="optimization_result")


class VibrationResult(SQLModel, table=True):
    """Semantic metadata for one MolOP ``Vibrations`` result."""

    __tablename__ = "vibration_result"  # pyright: ignore[reportAssignmentType]

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    frame_id: UUID = Field(
        foreign_key="calculation_frame.id",
        ondelete="CASCADE",
        unique=True,
        nullable=False,
    )
    mode_count: int = Field(nullable=False)
    imaginary_mode_count: int = Field(nullable=False)
    lowest_frequency_cm1: float | None = Field(default=None, sa_type=Float)
    mode_indices: list[int] = Field(sa_column=Column(ARRAY(Integer, dimensions=1), nullable=False))
    axis_order: list[str] | None = Field(
        default=None,
        sa_column=Column(ARRAY(String(32), dimensions=1), nullable=True),
    )
    atom_order: str | None = Field(default=None, max_length=32)
    normalization: str | None = Field(default=None, max_length=64)
    mass_weighting: str | None = Field(default=None, max_length=64)
    source_schema_version: str = Field(max_length=64, nullable=False)
    frame: CalculationFrame = Relationship(back_populates="vibration_result")


class CalculationStatusResult(SQLModel, table=True):
    """Direct source status emitted by MolOP for one calculation frame."""

    __tablename__ = "calculation_status_result"  # pyright: ignore[reportAssignmentType]

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    frame_id: UUID = Field(
        foreign_key="calculation_frame.id",
        ondelete="CASCADE",
        unique=True,
        nullable=False,
    )
    scf_converged: bool | None = Field(default=None)
    normal_terminated: bool | None = Field(default=None)
    source_schema_version: str = Field(max_length=64, nullable=False)
    frame: CalculationFrame = Relationship(back_populates="status_result")


class ScientificArray(SQLModel, table=True):
    """A typed numerical payload owned by one calculation frame."""

    __tablename__ = "scientific_array"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "frame_id",
            "kind",
            "ordinal",
            name="uq_scientific_array_frame_kind_ordinal",
        ),
        CheckConstraint("ordinal >= 0", name="ck_scientific_array_ordinal_nonnegative"),
        CheckConstraint(
            "cardinality(shape) > 0 AND array_position(shape, NULL) IS NULL AND 0 <= ALL(shape)",
            name="ck_scientific_array_shape",
        ),
        CheckConstraint(
            "array_nbytes >= 0",
            name="ck_scientific_array_nbytes_nonnegative",
        ),
        CheckConstraint(
            f"payload_sha256 ~ '{_HASH_PATTERN}'",
            name="ck_scientific_array_payload_hash_hex",
        ),
        CheckConstraint(
            "num_nonnulls(metadata_schema_version, metadata) IN (0, 2)",
            name="ck_scientific_array_metadata_complete",
        ),
        Index("ix_scientific_array_dtype_shape", "dtype", "shape"),
        CheckConstraint(
            "(kind = 'forces' AND unit = 'hartree/bohr') OR "
            "(kind = 'hessian' AND unit = 'hartree/bohr^2') OR "
            "(kind = 'vibrational_frequencies' AND unit = 'cm^-1') OR "
            "(kind = 'reduced_masses' AND unit = 'amu') OR "
            "(kind = 'vibrational_force_constants' AND unit = 'mdyne/angstrom') OR "
            "(kind = 'ir_intensities' AND unit = 'km/mol') OR "
            "(kind = 'normal_modes' AND unit = 'angstrom') OR "
            "(kind = 'moments_of_inertia' AND unit = 'amu*bohr^2') OR "
            "(kind = 'rotational_temperatures' AND unit = 'kelvin') OR "
            "(kind = 'rotational_constants' AND unit = 'gigahertz') OR "
            "(kind = 'vibrational_temperatures' AND unit = 'kelvin') OR "
            "(kind IN ('orbital_alpha_energies', 'orbital_beta_energies') "
            "AND unit = 'hartree') OR "
            "(kind IN ('orbital_coefficient', 'atomic_population', 'bond_order_matrix', "
            "'fukui_positive', 'fukui_negative', 'fukui_zero', "
            "'fractional_occupation_density') AND unit = 'dimensionless') OR "
            "(kind = 'polarizability_tensor' AND unit = 'bohr^3') OR "
            "(kind IN ('electric_dipole_moment', 'dipole', 'transition_dipole') "
            "AND unit = 'debye') OR "
            "(kind IN ('quadrupole', 'traceless_quadrupole') AND unit = 'debye*angstrom') OR "
            "(kind = 'octapole' AND unit = 'debye*angstrom^2') OR "
            "(kind = 'hexadecapole' AND unit = 'debye*angstrom^3') OR "
            "(kind IN ('nmr_shielding_tensor', 'nmr_principal_values') AND unit = 'ppm') OR "
            "(kind IN ('nmr_coupling_k', 'nmr_coupling_j', "
            "'nmr_coupling_k_component', 'nmr_coupling_j_component') AND unit = 'hertz')",
            name="ck_scientific_array_kind_unit",
        ),
    )
    __mapper_args__ = {
        "properties": {
            "data": deferred(_scientific_array_data_column, raiseload=True),
        }
    }
    model_config = ConfigDict(arbitrary_types_allowed=True)  # type: ignore[assignment]

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    frame_id: UUID = Field(
        foreign_key="calculation_frame.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    kind: ScientificArrayKind = Field(
        sa_column=Column(
            string_enum(ScientificArrayKind, name="scientific_array_kind"),
            nullable=False,
            index=True,
        )
    )
    ordinal: int = Field(sa_type=SMALLINT, nullable=False)
    unit: str = Field(max_length=64, nullable=False)
    dtype: str = Field(max_length=64, nullable=False)
    shape: list[int] = Field(sa_column=Column(ARRAY(Integer, dimensions=1), nullable=False))
    array_nbytes: int = Field(sa_type=BigInteger, nullable=False)
    payload_sha256: str = Field(max_length=64, nullable=False)
    data: npt.NDArray[np.generic] = Field(sa_column=_scientific_array_data_column)
    metadata_schema_version: str | None = Field(default=None, max_length=64)
    array_metadata: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column("metadata", JSONB(none_as_null=True), nullable=True),
    )
    frame: CalculationFrame = Relationship(back_populates="scientific_arrays")
    assignment: Optional["ScientificArrayAssignment"] = Relationship(
        back_populates="scientific_array", cascade_delete=True, passive_deletes=True
    )


class ThermochemistryResult(SQLModel, table=True):
    """Canonical thermochemistry scalars for a frequency calculation frame."""

    __tablename__ = "thermochemistry_result"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        CheckConstraint(
            "temperature_kelvin > 0",
            name="ck_thermochemistry_result_temperature_positive",
        ),
        CheckConstraint(
            "pressure_atm > 0",
            name="ck_thermochemistry_result_pressure_positive",
        ),
        CheckConstraint(
            "molecular_mass_amu IS NULL OR molecular_mass_amu > 0",
            name="ck_thermochemistry_result_mass_positive",
        ),
        CheckConstraint(
            "heat_capacity_cv_cal_mol_k IS NULL OR heat_capacity_cv_cal_mol_k >= 0",
            name="ck_thermochemistry_result_heat_capacity_nonnegative",
        ),
        CheckConstraint(
            "rotational_symmetry_number IS NULL OR rotational_symmetry_number >= 1",
            name="ck_thermochemistry_result_symmetry_positive",
        ),
        CheckConstraint(
            "num_nonnulls(zpe_correction_hartree, thermal_energy_correction_hartree, "
            "thermal_enthalpy_correction_hartree, thermal_gibbs_correction_hartree, "
            "zero_point_energy_hartree, thermal_internal_energy_hartree, enthalpy_hartree, "
            "gibbs_free_energy_hartree, entropy_cal_mol_k, heat_capacity_cv_cal_mol_k, "
            "molecular_mass_amu, rotational_symmetry_number) > 0",
            name="ck_thermochemistry_result_has_value",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    frame_id: UUID = Field(
        foreign_key="calculation_frame.id",
        ondelete="CASCADE",
        unique=True,
        nullable=False,
    )
    temperature_kelvin: float = Field(sa_type=Float, nullable=False)
    pressure_atm: float = Field(sa_type=Float, nullable=False)
    zpe_correction_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    thermal_energy_correction_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    thermal_enthalpy_correction_hartree: float | None = Field(
        default=None,
        sa_type=ScalarHartree,
    )
    thermal_gibbs_correction_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    zero_point_energy_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    thermal_internal_energy_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    enthalpy_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    gibbs_free_energy_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    entropy_cal_mol_k: float | None = Field(default=None, sa_type=Float)
    heat_capacity_cv_cal_mol_k: float | None = Field(default=None, sa_type=Float)
    molecular_mass_amu: float | None = Field(default=None, sa_type=Float)
    rotational_symmetry_number: int | None = Field(default=None)
    source_schema_version: str = Field(max_length=64, nullable=False)
    frame: CalculationFrame = Relationship(back_populates="thermochemistry_result")


class MolecularOrbitalResult(SQLModel, table=True):
    """One MolOP ``MolecularOrbitals`` container owned by a frame."""

    __tablename__ = "molecular_orbital_result"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        CheckConstraint(
            "alpha_orbital_count >= 0 AND beta_orbital_count >= 0 AND coefficient_count >= 0",
            name="ck_molecular_orbital_result_counts_nonnegative",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    frame_id: UUID = Field(
        foreign_key="calculation_frame.id", ondelete="CASCADE", unique=True, nullable=False
    )
    electronic_state: str | None = Field(default=None, max_length=128)
    alpha_orbital_count: int = Field(default=0, nullable=False)
    beta_orbital_count: int = Field(default=0, nullable=False)
    coefficient_count: int = Field(default=0, nullable=False)
    alpha_occupancies: list[float | None] = Field(
        default_factory=list, sa_column=Column(ARRAY(Float, dimensions=1), nullable=False)
    )
    beta_occupancies: list[float | None] = Field(
        default_factory=list, sa_column=Column(ARRAY(Float, dimensions=1), nullable=False)
    )
    alpha_symmetries: list[str | None] = Field(
        default_factory=list, sa_column=Column(ARRAY(String(128), dimensions=1), nullable=False)
    )
    beta_symmetries: list[str | None] = Field(
        default_factory=list, sa_column=Column(ARRAY(String(128), dimensions=1), nullable=False)
    )
    source_schema_version: str = Field(max_length=64, nullable=False)
    frame: CalculationFrame = Relationship(back_populates="molecular_orbital_result")
    array_assignments: list["ScientificArrayAssignment"] = Relationship(
        back_populates="molecular_orbital_result", cascade_delete=True, passive_deletes=True
    )


class ChargeSpinPopulationResult(SQLModel, table=True):
    """One extensible MolOP population-analysis container."""

    __tablename__ = "charge_spin_population_result"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        CheckConstraint(
            "series_count >= 0", name="ck_charge_spin_population_result_count_nonnegative"
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    frame_id: UUID = Field(
        foreign_key="calculation_frame.id", ondelete="CASCADE", unique=True, nullable=False
    )
    series_count: int = Field(default=0, nullable=False)
    source_schema_version: str = Field(max_length=64, nullable=False)
    frame: CalculationFrame = Relationship(back_populates="charge_spin_population_result")
    series: list["AtomicPopulationSeries"] = Relationship(
        back_populates="result", cascade_delete=True, passive_deletes=True
    )


class AtomicPopulationSeries(SQLModel, table=True):
    """One source-order atomic population series from MolOP."""

    __tablename__ = "atomic_population_series"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint("result_id", "series_key", name="uq_atomic_population_series_key"),
        CheckConstraint("value_count > 0", name="ck_atomic_population_series_value_count_positive"),
        CheckConstraint(
            "spin_channel IS NULL OR spin_channel IN ('alpha', 'beta', 'total')",
            name="ck_atomic_population_series_spin_channel",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    result_id: UUID = Field(
        foreign_key="charge_spin_population_result.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    series_key: str = Field(max_length=128, nullable=False)
    scheme: str = Field(max_length=128, nullable=False)
    quantity: str = Field(max_length=128, nullable=False)
    value_count: int = Field(nullable=False)
    spin_channel: str | None = Field(default=None, max_length=16)
    source_label: str | None = Field(default=None, sa_type=Text)
    series_metadata: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column("metadata", JSONB, nullable=False)
    )
    result: ChargeSpinPopulationResult = Relationship(back_populates="series")
    array_assignments: list["ScientificArrayAssignment"] = Relationship(
        back_populates="atomic_population_series", cascade_delete=True, passive_deletes=True
    )


class PolarizabilityResult(SQLModel, table=True):
    """Scalar projection and numerical-array owner for MolOP polarizability."""

    __tablename__ = "polarizability_result"  # pyright: ignore[reportAssignmentType]

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    frame_id: UUID = Field(
        foreign_key="calculation_frame.id", ondelete="CASCADE", unique=True, nullable=False
    )
    electronic_spatial_extent_bohr2: float | None = Field(default=None, sa_type=Float)
    isotropic_polarizability_bohr3: float | None = Field(default=None, sa_type=Float)
    anisotropic_polarizability_bohr3: float | None = Field(default=None, sa_type=Float)
    source_schema_version: str = Field(max_length=64, nullable=False)
    frame: CalculationFrame = Relationship(back_populates="polarizability_result")
    array_assignments: list["ScientificArrayAssignment"] = Relationship(
        back_populates="polarizability_result", cascade_delete=True, passive_deletes=True
    )


class NMRResult(SQLModel, table=True):
    """One MolOP NMR result with shielding and coupling children."""

    __tablename__ = "nmr_result"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        CheckConstraint("shielding_count >= 0", name="ck_nmr_result_shielding_count_nonnegative"),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    frame_id: UUID = Field(
        foreign_key="calculation_frame.id", ondelete="CASCADE", unique=True, nullable=False
    )
    gauge: str | None = Field(default=None, max_length=128)
    shielding_count: int = Field(default=0, nullable=False)
    coupling_atom_indices: list[int] = Field(
        default_factory=list, sa_column=Column(ARRAY(Integer, dimensions=1), nullable=False)
    )
    source_schema_version: str = Field(max_length=64, nullable=False)
    frame: CalculationFrame = Relationship(back_populates="nmr_result")
    shielding_tensors: list["NMRShieldingTensor"] = Relationship(
        back_populates="result", cascade_delete=True, passive_deletes=True
    )
    array_assignments: list["ScientificArrayAssignment"] = Relationship(
        back_populates="nmr_result", cascade_delete=True, passive_deletes=True
    )


class NMRShieldingTensor(SQLModel, table=True):
    """Source-order scalar metadata for one MolOP shielding tensor."""

    __tablename__ = "nmr_shielding_tensor"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint("result_id", "atom_index", name="uq_nmr_shielding_tensor_atom"),
        CheckConstraint("atom_index >= 0", name="ck_nmr_shielding_tensor_atom_index_nonnegative"),
        CheckConstraint(
            "orientation IN ('input', 'standard', 'source', 'unknown')",
            name="ck_nmr_shielding_tensor_orientation",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    result_id: UUID = Field(
        foreign_key="nmr_result.id", ondelete="CASCADE", index=True, nullable=False
    )
    atom_index: int = Field(nullable=False)
    atom_symbol: str = Field(max_length=8, nullable=False)
    isotropic_ppm: float | None = Field(default=None, sa_type=Float)
    anisotropy_ppm: float | None = Field(default=None, sa_type=Float)
    anisotropy_convention: str | None = Field(default=None, max_length=64)
    orientation: str = Field(default="unknown", max_length=16, nullable=False)
    result: NMRResult = Relationship(back_populates="shielding_tensors")
    array_assignments: list["ScientificArrayAssignment"] = Relationship(
        back_populates="nmr_shielding_tensor", cascade_delete=True, passive_deletes=True
    )


class BondOrderResult(SQLModel, table=True):
    """One MolOP bond-order matrix collection."""

    __tablename__ = "bond_order_result"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        CheckConstraint("matrix_count >= 0", name="ck_bond_order_result_count_nonnegative"),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    frame_id: UUID = Field(
        foreign_key="calculation_frame.id", ondelete="CASCADE", unique=True, nullable=False
    )
    matrix_count: int = Field(default=0, nullable=False)
    source_schema_version: str = Field(max_length=64, nullable=False)
    frame: CalculationFrame = Relationship(back_populates="bond_order_result")
    array_assignments: list["ScientificArrayAssignment"] = Relationship(
        back_populates="bond_order_result", cascade_delete=True, passive_deletes=True
    )


class TotalSpinResult(SQLModel, table=True):
    """One MolOP total-spin scalar result."""

    __tablename__ = "total_spin_result"  # pyright: ignore[reportAssignmentType]

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    frame_id: UUID = Field(
        foreign_key="calculation_frame.id", ondelete="CASCADE", unique=True, nullable=False
    )
    spin_square: float | None = Field(default=None, sa_type=Float)
    spin_quantum_number: float | None = Field(default=None, sa_type=Float)
    source_schema_version: str = Field(max_length=64, nullable=False)
    frame: CalculationFrame = Relationship(back_populates="total_spin_result")


class SinglePointPropertyResult(SQLModel, table=True):
    """MolOP scalar and atom-aligned single-point properties."""

    __tablename__ = "single_point_property_result"  # pyright: ignore[reportAssignmentType]

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    frame_id: UUID = Field(
        foreign_key="calculation_frame.id", ondelete="CASCADE", unique=True, nullable=False
    )
    vertical_ionization_potential_ev: float | None = Field(default=None, sa_type=Float)
    vertical_electron_affinity_ev: float | None = Field(default=None, sa_type=Float)
    global_electrophilicity_index_ev: float | None = Field(default=None, sa_type=Float)
    source_schema_version: str = Field(max_length=64, nullable=False)
    frame: CalculationFrame = Relationship(back_populates="single_point_property_result")
    array_assignments: list["ScientificArrayAssignment"] = Relationship(
        back_populates="single_point_property_result", cascade_delete=True, passive_deletes=True
    )


class ElectronicStateSet(SQLModel, table=True):
    """An ordered MolOP ``ElectronicStates`` collection in a declared scope."""

    __tablename__ = "electronic_state_set"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint("frame_id", "kind", name="uq_electronic_state_set_frame_kind"),
        CheckConstraint("state_count >= 0", name="ck_electronic_state_set_count_nonnegative"),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    frame_id: UUID = Field(
        foreign_key="calculation_frame.id", ondelete="CASCADE", index=True, nullable=False
    )
    kind: ElectronicStateSetKind = Field(
        sa_column=Column(
            string_enum(ElectronicStateSetKind, name="electronic_state_set_kind"),
            nullable=False,
        )
    )
    state_count: int = Field(default=0, nullable=False)
    source_schema_version: str = Field(max_length=64, nullable=False)
    frame: CalculationFrame = Relationship(back_populates="electronic_state_sets")
    states: list["ElectronicState"] = Relationship(
        back_populates="state_set", cascade_delete=True, passive_deletes=True
    )
    multireference_result: Optional["MultireferenceResult"] = Relationship(
        back_populates="electronic_state_set"
    )


class ElectronicState(SQLModel, table=True):
    """One ordered electronic state from MolOP."""

    __tablename__ = "electronic_state"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint("state_set_id", "state_ordinal", name="uq_electronic_state_ordinal"),
        CheckConstraint("state_ordinal >= 0", name="ck_electronic_state_ordinal_nonnegative"),
        CheckConstraint(
            "multiplicity IS NULL OR multiplicity > 0",
            name="ck_electronic_state_multiplicity_positive",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    state_set_id: UUID = Field(
        foreign_key="electronic_state_set.id", ondelete="CASCADE", index=True, nullable=False
    )
    state_ordinal: int = Field(nullable=False)
    state_index: int | None = Field(default=None)
    root: int | None = Field(default=None)
    label: str | None = Field(default=None, max_length=256)
    multiplicity: int | None = Field(default=None)
    spin: float | None = Field(default=None, sa_type=Float)
    irrep: str | None = Field(default=None, max_length=128)
    method: str | None = Field(default=None, max_length=128)
    energy_hartree: float | None = Field(default=None, sa_type=ScalarHartree)
    excitation_energy_ev: float | None = Field(default=None, sa_type=Float)
    oscillator_strength: float | None = Field(default=None, sa_type=Float)
    state_properties: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column("properties", JSONB, nullable=False)
    )
    source: str | None = Field(default=None, sa_type=Text)
    state_set: ElectronicStateSet = Relationship(back_populates="states")
    configurations: list["ElectronicConfiguration"] = Relationship(
        back_populates="electronic_state", cascade_delete=True, passive_deletes=True
    )
    array_assignments: list["ScientificArrayAssignment"] = Relationship(
        back_populates="electronic_state", cascade_delete=True, passive_deletes=True
    )


class ElectronicConfiguration(SQLModel, table=True):
    """One dominant configuration attached to an electronic state."""

    __tablename__ = "electronic_configuration"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "electronic_state_id",
            "configuration_ordinal",
            name="uq_electronic_configuration_ordinal",
        ),
        CheckConstraint(
            "configuration_ordinal >= 0",
            name="ck_electronic_configuration_ordinal_nonnegative",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    electronic_state_id: UUID = Field(
        foreign_key="electronic_state.id", ondelete="CASCADE", index=True, nullable=False
    )
    configuration_ordinal: int = Field(nullable=False)
    label: str | None = Field(default=None, max_length=256)
    coefficient: float | None = Field(default=None, sa_type=Float)
    weight: float | None = Field(default=None, sa_type=Float)
    occupation: list[float] = Field(
        default_factory=list, sa_column=Column(ARRAY(Float, dimensions=1), nullable=False)
    )
    orbital_indices: list[int] = Field(
        default_factory=list, sa_column=Column(ARRAY(Integer, dimensions=1), nullable=False)
    )
    raw: str = Field(default="", sa_type=Text, nullable=False)
    electronic_state: ElectronicState = Relationship(back_populates="configurations")


class MultireferenceResult(SQLModel, table=True):
    """One MolOP multi-reference result and optional scoped state set."""

    __tablename__ = "multireference_result"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        CheckConstraint(
            "(active_space_electrons IS NULL OR active_space_electrons >= 0) AND "
            "(active_space_orbitals IS NULL OR active_space_orbitals >= 0) AND "
            "(active_space_roots IS NULL OR active_space_roots >= 0)",
            name="ck_multireference_result_active_space_nonnegative",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    frame_id: UUID = Field(
        foreign_key="calculation_frame.id", ondelete="CASCADE", unique=True, nullable=False
    )
    electronic_state_set_id: UUID | None = Field(
        default=None,
        foreign_key="electronic_state_set.id",
        ondelete="RESTRICT",
        unique=True,
        nullable=True,
    )
    method: str | None = Field(default=None, max_length=128)
    reference_method: str | None = Field(default=None, max_length=128)
    ci_type: str | None = Field(default=None, max_length=128)
    active_space_electrons: int | None = Field(default=None)
    active_space_orbitals: int | None = Field(default=None)
    active_space_roots: int | None = Field(default=None)
    active_orbitals: list[int] = Field(
        default_factory=list, sa_column=Column(ARRAY(Integer, dimensions=1), nullable=False)
    )
    inactive_orbitals: list[int] = Field(
        default_factory=list, sa_column=Column(ARRAY(Integer, dimensions=1), nullable=False)
    )
    frozen_orbitals: list[int] = Field(
        default_factory=list, sa_column=Column(ARRAY(Integer, dimensions=1), nullable=False)
    )
    active_space_raw: str = Field(default="", sa_type=Text, nullable=False)
    active_space_options: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    corrections: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    diagnostics: list[str] = Field(
        default_factory=list, sa_column=Column(ARRAY(Text, dimensions=1), nullable=False)
    )
    result_properties: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column("properties", JSONB, nullable=False)
    )
    source_schema_version: str = Field(max_length=64, nullable=False)
    frame: CalculationFrame = Relationship(back_populates="multireference_result")
    electronic_state_set: ElectronicStateSet | None = Relationship(
        back_populates="multireference_result"
    )


class ImplicitSolvationResult(SQLModel, table=True):
    """Normalized MolOP implicit-solvation parameters attached to a frame."""

    __tablename__ = "implicit_solvation_result"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        CheckConstraint(
            "solvent_epsilon IS NULL OR solvent_epsilon > 0",
            name="ck_implicit_solvation_result_epsilon_positive",
        ),
        CheckConstraint(
            "solvent_epsilon_infinite IS NULL OR solvent_epsilon_infinite > 0",
            name="ck_implicit_solvation_result_epsilon_infinite_positive",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    frame_id: UUID = Field(
        foreign_key="calculation_frame.id", ondelete="CASCADE", unique=True, nullable=False
    )
    solvent: str | None = Field(default=None, max_length=128)
    solvent_model: str | None = Field(default=None, max_length=128)
    atomic_radii: str | None = Field(default=None, max_length=128)
    solvent_epsilon: float | None = Field(default=None, sa_type=Float)
    solvent_epsilon_infinite: float | None = Field(default=None, sa_type=Float)
    source_schema_version: str = Field(max_length=64, nullable=False)
    frame: CalculationFrame = Relationship(back_populates="implicit_solvation_result")


class ScientificArrayAssignment(SQLModel, table=True):
    """Semantic ownership and slot for one frame-owned scientific array."""

    __tablename__ = "scientific_array_assignment"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        CheckConstraint(
            "slot_ordinal >= 0", name="ck_scientific_array_assignment_slot_ordinal_nonnegative"
        ),
        CheckConstraint(
            "num_nonnulls(molecular_orbital_result_id, atomic_population_series_id, "
            "polarizability_result_id, nmr_result_id, nmr_shielding_tensor_id, "
            "bond_order_result_id, single_point_property_result_id, electronic_state_id) = 1",
            name="ck_scientific_array_assignment_one_owner",
        ),
        UniqueConstraint(
            "molecular_orbital_result_id",
            "slot",
            "slot_ordinal",
            name="uq_scientific_array_assignment_molecular_orbital",
        ),
        UniqueConstraint(
            "atomic_population_series_id",
            "slot",
            "slot_ordinal",
            name="uq_scientific_array_assignment_population",
        ),
        UniqueConstraint(
            "polarizability_result_id",
            "slot",
            "slot_ordinal",
            name="uq_scientific_array_assignment_polarizability",
        ),
        UniqueConstraint(
            "nmr_result_id",
            "slot",
            "slot_ordinal",
            name="uq_scientific_array_assignment_nmr",
        ),
        UniqueConstraint(
            "nmr_shielding_tensor_id",
            "slot",
            "slot_ordinal",
            name="uq_scientific_array_assignment_nmr_shielding",
        ),
        UniqueConstraint(
            "bond_order_result_id",
            "slot",
            "slot_ordinal",
            name="uq_scientific_array_assignment_bond_order",
        ),
        UniqueConstraint(
            "single_point_property_result_id",
            "slot",
            "slot_ordinal",
            name="uq_scientific_array_assignment_single_point",
        ),
        UniqueConstraint(
            "electronic_state_id",
            "slot",
            "slot_ordinal",
            name="uq_scientific_array_assignment_electronic_state",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    scientific_array_id: UUID = Field(
        foreign_key="scientific_array.id", ondelete="CASCADE", unique=True, nullable=False
    )
    slot: str = Field(max_length=128, nullable=False)
    slot_ordinal: int = Field(default=0, nullable=False)
    molecular_orbital_result_id: UUID | None = Field(
        default=None, foreign_key="molecular_orbital_result.id", ondelete="CASCADE"
    )
    atomic_population_series_id: UUID | None = Field(
        default=None, foreign_key="atomic_population_series.id", ondelete="CASCADE"
    )
    polarizability_result_id: UUID | None = Field(
        default=None, foreign_key="polarizability_result.id", ondelete="CASCADE"
    )
    nmr_result_id: UUID | None = Field(
        default=None, foreign_key="nmr_result.id", ondelete="CASCADE"
    )
    nmr_shielding_tensor_id: UUID | None = Field(
        default=None, foreign_key="nmr_shielding_tensor.id", ondelete="CASCADE"
    )
    bond_order_result_id: UUID | None = Field(
        default=None, foreign_key="bond_order_result.id", ondelete="CASCADE"
    )
    single_point_property_result_id: UUID | None = Field(
        default=None, foreign_key="single_point_property_result.id", ondelete="CASCADE"
    )
    electronic_state_id: UUID | None = Field(
        default=None, foreign_key="electronic_state.id", ondelete="CASCADE"
    )
    scientific_array: ScientificArray = Relationship(back_populates="assignment")
    molecular_orbital_result: MolecularOrbitalResult | None = Relationship(
        back_populates="array_assignments"
    )
    atomic_population_series: AtomicPopulationSeries | None = Relationship(
        back_populates="array_assignments"
    )
    polarizability_result: PolarizabilityResult | None = Relationship(
        back_populates="array_assignments"
    )
    nmr_result: NMRResult | None = Relationship(back_populates="array_assignments")
    nmr_shielding_tensor: NMRShieldingTensor | None = Relationship(
        back_populates="array_assignments"
    )
    bond_order_result: BondOrderResult | None = Relationship(back_populates="array_assignments")
    single_point_property_result: SinglePointPropertyResult | None = Relationship(
        back_populates="array_assignments"
    )
    electronic_state: ElectronicState | None = Relationship(back_populates="array_assignments")


__all__ = [
    "AtomicPopulationSeries",
    "BondOrderResult",
    "CalculationFrame",
    "CalculationSegment",
    "CalculationStatusResult",
    "EnergyObservation",
    "ElectronicConfiguration",
    "ElectronicState",
    "ElectronicStateSet",
    "FrameEnergyResult",
    "GeometryOptimizationResult",
    "ImplicitSolvationResult",
    "MolecularOrbitalResult",
    "MultireferenceResult",
    "NMRResult",
    "NMRShieldingTensor",
    "ParseRevision",
    "PolarizabilityResult",
    "ChargeSpinPopulationResult",
    "ScientificArray",
    "ScientificArrayAssignment",
    "SinglePointPropertyResult",
    "ThermochemistryResult",
    "TotalSpinResult",
    "VibrationResult",
]
