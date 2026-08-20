"""Stable string values shared by ORM entities and API DTOs."""

from enum import StrEnum

from sqlalchemy import Enum as SQLAlchemyEnum


class StereoStatus(StrEnum):
    ASSIGNED = "assigned"
    UNASSIGNED = "unassigned"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class TopologySanitizationStatus(StrEnum):
    """Whether an RDKit graph passed chemical sanitization.

    A failed graph is still a useful connectivity candidate for cartridge
    substructure searches, but must not participate in descriptor or Morgan
    fingerprint projections.
    """

    SANITIZED = "sanitized"
    FAILED = "failed"


class SimilarityMetric(StrEnum):
    # NexusX uses member names as GraphQL enum literals and values for coercion.
    tanimoto = "tanimoto"
    dice = "dice"


class ArtifactKind(StrEnum):
    CALCULATION_OUTPUT = "calculation_output"
    INPUT = "input"
    WORKFLOW_MANIFEST = "workflow_manifest"
    AUXILIARY = "auxiliary"


class ArtifactVisibility(StrEnum):
    PUBLIC = "public"
    PROJECT = "project"


class StorageStatus(StrEnum):
    PENDING = "pending"
    AVAILABLE = "available"
    MISSING = "missing"
    CORRUPT = "corrupt"
    RETIRED = "retired"


class StorageGarbageCollectionRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class UserStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class OrganizationStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class OrganizationRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class ProjectRole(StrEnum):
    MANAGER = "manager"
    CONTRIBUTOR = "contributor"
    VIEWER = "viewer"


class QMSoftware(StrEnum):
    GAUSSIAN = "gaussian"
    ORCA = "orca"
    OTHER = "other"


class ParseStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    QUARANTINED = "quarantined"
    FAILED = "failed"


class ArtifactIngestionStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class UploadBatchStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class UploadBatchItemStatus(StrEnum):
    QUEUED = "queued"
    UPLOADING = "uploading"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TransitionStateInferenceStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TransitionStateEndpointDirection(StrEnum):
    """Signed displacement direction of a persisted imaginary-mode endpoint."""

    NEGATIVE = "negative"
    POSITIVE = "positive"


class ParseCompleteness(StrEnum):
    NOT_ASSESSED = "not_assessed"
    COMPLETE = "complete"
    PARTIAL = "partial"


class EnergyQuantitySemantics(StrEnum):
    TOTAL_ENERGY = "total_energy"
    CORRELATION_CORRECTION = "correlation_correction"
    COMPONENT = "component"


class SourceFormat(StrEnum):
    GAUSSIAN_LOG = "gaussian_log"
    ORCA_OUTPUT = "orca_output"
    OTHER = "other"


class TerminationStatus(StrEnum):
    NORMAL = "normal"
    ERROR = "error"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


class SCFStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    CONVERGED = "converged"
    FAILED = "failed"
    UNKNOWN = "unknown"


class FrameRole(StrEnum):
    INITIAL = "initial"
    INTERMEDIATE = "intermediate"
    TERMINAL = "terminal"
    SINGLE_POINT = "single_point"


class GeometryAssignmentKind(StrEnum):
    PARSED_EXACT = "parsed_exact"
    MATCHED_EXISTING_GEOMETRY = "matched_existing_geometry"
    # Backward-compatible Python name; the persisted value is software-neutral.
    MATCHED_GAUSSIAN_AUTHORITY = "matched_existing_geometry"


class ElectronicStateKind(StrEnum):
    GROUND = "ground"


class OptimizationStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    NOT_CONVERGED = "not_converged"
    CONVERGED = "converged"
    UNKNOWN = "unknown"


class SelectedEnergyKind(StrEnum):
    ELECTRONIC_TOTAL = "electronic_total_energy_hartree"
    REFERENCE_TOTAL = "reference_total_energy_hartree"
    MP2_TOTAL = "mp2_total_energy_hartree"
    MP3_TOTAL = "mp3_total_energy_hartree"
    MP4_TOTAL = "mp4_total_energy_hartree"
    MP5_TOTAL = "mp5_total_energy_hartree"
    CCSD_TOTAL = "ccsd_total_energy_hartree"
    CCSD_T_TOTAL = "ccsd_t_total_energy_hartree"


class ScientificArrayKind(StrEnum):
    FORCES = "forces"
    HESSIAN = "hessian"
    VIBRATIONAL_FREQUENCIES = "vibrational_frequencies"
    REDUCED_MASSES = "reduced_masses"
    VIBRATIONAL_FORCE_CONSTANTS = "vibrational_force_constants"
    IR_INTENSITIES = "ir_intensities"
    NORMAL_MODES = "normal_modes"
    MOMENTS_OF_INERTIA = "moments_of_inertia"
    ROTATIONAL_TEMPERATURES = "rotational_temperatures"
    ROTATIONAL_CONSTANTS = "rotational_constants"
    VIBRATIONAL_TEMPERATURES = "vibrational_temperatures"
    ORBITAL_ALPHA_ENERGIES = "orbital_alpha_energies"
    ORBITAL_BETA_ENERGIES = "orbital_beta_energies"
    ORBITAL_COEFFICIENT = "orbital_coefficient"
    ATOMIC_POPULATION = "atomic_population"
    POLARIZABILITY_TENSOR = "polarizability_tensor"
    ELECTRIC_DIPOLE_MOMENT = "electric_dipole_moment"
    DIPOLE = "dipole"
    QUADRUPOLE = "quadrupole"
    TRACELESS_QUADRUPOLE = "traceless_quadrupole"
    OCTAPOLE = "octapole"
    HEXADECAPOLE = "hexadecapole"
    NMR_SHIELDING_TENSOR = "nmr_shielding_tensor"
    NMR_PRINCIPAL_VALUES = "nmr_principal_values"
    NMR_COUPLING_K = "nmr_coupling_k"
    NMR_COUPLING_J = "nmr_coupling_j"
    NMR_COUPLING_K_COMPONENT = "nmr_coupling_k_component"
    NMR_COUPLING_J_COMPONENT = "nmr_coupling_j_component"
    BOND_ORDER_MATRIX = "bond_order_matrix"
    FUKUI_POSITIVE = "fukui_positive"
    FUKUI_NEGATIVE = "fukui_negative"
    FUKUI_ZERO = "fukui_zero"
    FRACTIONAL_OCCUPATION_DENSITY = "fractional_occupation_density"
    TRANSITION_DIPOLE = "transition_dipole"


class ElectronicStateSetKind(StrEnum):
    FRAME = "frame"
    MULTIREFERENCE = "multireference"


class ScientificArrayOwnerKind(StrEnum):
    MOLECULAR_ORBITAL_RESULT = "molecular_orbital_result"
    ATOMIC_POPULATION_SERIES = "atomic_population_series"
    POLARIZABILITY_RESULT = "polarizability_result"
    NMR_RESULT = "nmr_result"
    NMR_SHIELDING_TENSOR = "nmr_shielding_tensor"
    BOND_ORDER_RESULT = "bond_order_result"
    SINGLE_POINT_PROPERTY_RESULT = "single_point_property_result"
    ELECTRONIC_STATE = "electronic_state"


class WorkflowManifestStatus(StrEnum):
    RECEIVED = "received"
    VALIDATED = "validated"
    PUBLISHED = "published"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ManifestArtifactRole(StrEnum):
    GAUSSIAN_OPT_FREQ = "gaussian_opt_freq"
    ORCA_SINGLE_POINT = "orca_single_point"
    INPUT = "input"
    SUPPORTING = "supporting"


class ArtifactResolutionStatus(StrEnum):
    DECLARED = "declared"
    RESOLVED = "resolved"
    MISSING = "missing"
    HASH_MISMATCH = "hash_mismatch"
    PARSE_FAILED = "parse_failed"
    QUARANTINED = "quarantined"


class ReactionClass(StrEnum):
    CYCLOADDITION = "cycloaddition"


class LogicalReactionParticipantSide(StrEnum):
    REACTANT = "reactant"
    PRODUCT = "product"


class LogicalReactionParticipantRole(StrEnum):
    DIENE = "diene"
    DIENOPHILE = "dienophile"
    DIPOLE = "dipole"
    DIPOLAROPHILE = "dipolarophile"
    PRODUCT = "product"
    OTHER = "other"


class MappedReactionKind(StrEnum):
    CURATED = "curated"
    MINIMUM_ENERGY = "minimum_energy"
    IRC_SUPPORTED = "irc_supported"
    OTHER = "other"


class MappedReactionNodeRole(StrEnum):
    REACTANT = "reactant"
    REACTANT_COMPLEX = "reactant_complex"
    INTERMEDIATE = "intermediate"
    TRANSITION_STATE = "transition_state"
    PRODUCT = "product"
    PRODUCT_COMPLEX = "product_complex"
    OTHER = "other"


class MappedReactionEdgeKind(StrEnum):
    ELEMENTARY_STEP = "elementary_step"
    CONFORMATIONAL = "conformational"
    ASSOCIATION = "association"
    DISSOCIATION = "dissociation"
    OTHER = "other"


def string_enum(
    enum_type: type[StrEnum],
    *,
    name: str,
    length: int | None = None,
) -> SQLAlchemyEnum:
    """Build a non-native enum that persists member values, not Python names."""

    return SQLAlchemyEnum(
        enum_type,
        values_callable=lambda members: [member.value for member in members],
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        name=name,
        length=length,
    )


__all__ = [
    "ArtifactKind",
    "ArtifactIngestionStatus",
    "ArtifactResolutionStatus",
    "ArtifactVisibility",
    "ElectronicStateKind",
    "ElectronicStateSetKind",
    "EnergyQuantitySemantics",
    "FrameRole",
    "GeometryAssignmentKind",
    "ManifestArtifactRole",
    "OptimizationStatus",
    "OrganizationRole",
    "OrganizationStatus",
    "ParseCompleteness",
    "ParseStatus",
    "ProjectRole",
    "ProjectStatus",
    "QMSoftware",
    "ReactionClass",
    "LogicalReactionParticipantRole",
    "LogicalReactionParticipantSide",
    "MappedReactionEdgeKind",
    "MappedReactionKind",
    "MappedReactionNodeRole",
    "SCFStatus",
    "SelectedEnergyKind",
    "ScientificArrayKind",
    "ScientificArrayOwnerKind",
    "SourceFormat",
    "StereoStatus",
    "TopologySanitizationStatus",
    "StorageStatus",
    "StorageGarbageCollectionRunStatus",
    "UploadBatchItemStatus",
    "UploadBatchStatus",
    "UserStatus",
    "TerminationStatus",
    "TransitionStateInferenceStatus",
    "TransitionStateEndpointDirection",
    "WorkflowManifestStatus",
    "string_enum",
]
