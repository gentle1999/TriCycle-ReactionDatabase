"""Validated records for manifest-declared reaction path aggregates."""

from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from tricycle_reaction_db.domain.enums import (
    ArtifactResolutionStatus,
    LogicalReactionParticipantRole,
    LogicalReactionParticipantSide,
    ManifestArtifactRole,
    MappedReactionEdgeKind,
    MappedReactionKind,
    MappedReactionNodeRole,
    ReactionClass,
    WorkflowManifestStatus,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class WorkflowManifestRecord(BaseModel):
    """Immutable identity and validation state for one manifest revision."""

    model_config = ConfigDict(frozen=True)

    manifest_key: str = Field(min_length=1)
    revision: int = Field(ge=1)
    schema_version: str = Field(min_length=1, max_length=64)
    payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    qc_policy_version: str = Field(min_length=1, max_length=64)
    status: WorkflowManifestStatus = WorkflowManifestStatus.RECEIVED
    validation_metadata: dict[str, object] = Field(default_factory=dict)
    published_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_publication_timestamp(self) -> Self:
        if self.status is WorkflowManifestStatus.PUBLISHED and self.published_at is None:
            raise ValueError("a published manifest requires published_at")
        if (
            self.status
            in {
                WorkflowManifestStatus.RECEIVED,
                WorkflowManifestStatus.VALIDATED,
                WorkflowManifestStatus.REJECTED,
            }
            and self.published_at is not None
        ):
            raise ValueError(f"{self.status.value} manifests cannot have published_at")
        return self


class ManifestArtifactBindingRecord(BaseModel):
    """One manifest declaration for an expected calculation artifact."""

    model_config = ConfigDict(frozen=True)

    artifact_key: str = Field(min_length=1)
    expected_content_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    artifact_role: ManifestArtifactRole
    reaction_key: str = Field(min_length=1)
    path_key: str = Field(min_length=1)
    node_key: str = Field(min_length=1)
    segment_index: int | None = Field(default=None, ge=0)
    frame_index: int | None = Field(default=None, ge=0)
    source_geometry_artifact_key: str | None = Field(default=None, min_length=1)
    resolution_status: ArtifactResolutionStatus = ArtifactResolutionStatus.DECLARED

    @model_validator(mode="after")
    def validate_resolution_shape(self) -> Self:
        if (self.segment_index is None) != (self.frame_index is None):
            raise ValueError("segment_index and frame_index must either both be set or both absent")
        if self.source_geometry_artifact_key == self.artifact_key:
            raise ValueError("an artifact binding cannot use itself as its geometry source")
        if self.resolution_status is ArtifactResolutionStatus.RESOLVED and (
            self.expected_content_sha256 is None or self.segment_index is None
        ):
            raise ValueError("a resolved binding requires an expected hash and frame selector")
        return self


class LogicalReactionRecord(BaseModel):
    """Topology-defined identity for one net chemical transformation."""

    model_config = ConfigDict(frozen=True)

    reaction_key: str = Field(min_length=1)
    label: str | None = Field(default=None, min_length=1)
    reaction_class: ReactionClass | None = None
    cycloaddition_pattern: str | None = Field(default=None, min_length=1, max_length=32)
    reaction_hash: str = Field(pattern=_SHA256_PATTERN)


class CreateReactionCommand(BaseModel):
    """Create a reaction from one RDKit-parseable representation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reaction: str = Field(min_length=1, max_length=16_384)
    label: str | None = Field(default=None, min_length=1)
    reaction_class: ReactionClass | None = None
    cycloaddition_pattern: str | None = Field(default=None, min_length=1, max_length=32)
    mapped_reaction_key: str | None = Field(default=None, min_length=1)
    mapped_reaction_kind: MappedReactionKind = MappedReactionKind.CURATED


class CreateReactionResult(BaseModel):
    """Stable identifiers returned by idempotent logical reaction creation."""

    model_config = ConfigDict(frozen=True)

    logical_reaction_id: UUID
    mapped_reaction_id: UUID | None
    reactant_node_id: UUID | None
    product_node_id: UUID | None
    reaction_hash: str
    topology_ids: list[UUID]
    topologies_created: int
    mapping_complete: bool
    logical_reaction_created: bool
    mapped_reaction_created: bool


class LogicalReactionParticipantRecord(BaseModel):
    """One topology instance on a side of a net reaction."""

    model_config = ConfigDict(frozen=True)

    side: LogicalReactionParticipantSide
    participant_index: int = Field(ge=0)
    role: LogicalReactionParticipantRole | None = None
    stoichiometric_coefficient: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_v1_mapping(self) -> Self:
        if self.stoichiometric_coefficient != 1:
            raise ValueError("v1 mapped participants require stoichiometric_coefficient=1")
        if self.role is LogicalReactionParticipantRole.PRODUCT:
            if self.side is not LogicalReactionParticipantSide.PRODUCT:
                raise ValueError("the product role requires the product side")
        elif self.side is LogicalReactionParticipantSide.PRODUCT and self.role not in {
            None,
            LogicalReactionParticipantRole.OTHER,
        }:
            raise ValueError("reactant-specific roles cannot be used on the product side")
        return self


class MappedReactionRecord(BaseModel):
    """One explicit mapped reaction under a logical reaction."""

    model_config = ConfigDict(frozen=True)

    mapped_reaction_key: str = Field(min_length=1)
    label: str | None = Field(default=None, min_length=1)
    mapped_reaction_kind: MappedReactionKind
    mapped_reaction_smiles: str = Field(min_length=1)
    mapping_hash: str = Field(pattern=_SHA256_PATTERN)


class MappedReactionNodeRecord(BaseModel):
    """One logical state in a reaction path."""

    model_config = ConfigDict(frozen=True)

    node_key: str = Field(min_length=1)
    node_index: int = Field(ge=0)
    role: MappedReactionNodeRole


class MappedReactionNodeGeometryRecord(BaseModel):
    """One concrete coordinate member bound to a logical path node."""

    model_config = ConfigDict(frozen=True)

    component_key: str = Field(min_length=1)
    component_index: int = Field(ge=0)
    coordinate_index: int = Field(default=0, ge=0)
    is_primary: bool = True


class MappedReactionNodeGeometryMappingRecord(BaseModel):
    """One verified reaction-map to coordinate-order conversion."""

    model_config = ConfigDict(frozen=True)

    geometry_atom_map_numbers: list[int] = Field(min_length=1)
    mapped_smiles: str = Field(min_length=1)
    mapping_method: str = Field(min_length=1, max_length=64)
    mapping_version: str = Field(min_length=1, max_length=64)
    verified: bool

    @model_validator(mode="after")
    def validate_mapping(self) -> Self:
        if any(number <= 0 for number in self.geometry_atom_map_numbers):
            raise ValueError("Geometry atom-map numbers must be positive")
        if len(set(self.geometry_atom_map_numbers)) != len(self.geometry_atom_map_numbers):
            raise ValueError("Geometry atom-map numbers must be unique")
        return self


class MappedReactionEdgeRecord(BaseModel):
    """One manifest-directed edge between two path nodes."""

    model_config = ConfigDict(frozen=True)

    edge_key: str = Field(min_length=1)
    edge_kind: MappedReactionEdgeKind


__all__ = [
    "CreateReactionCommand",
    "CreateReactionResult",
    "ManifestArtifactBindingRecord",
    "LogicalReactionParticipantRecord",
    "MappedReactionEdgeRecord",
    "MappedReactionNodeGeometryMappingRecord",
    "MappedReactionNodeGeometryRecord",
    "MappedReactionNodeRecord",
    "MappedReactionRecord",
    "LogicalReactionRecord",
    "WorkflowManifestRecord",
]
