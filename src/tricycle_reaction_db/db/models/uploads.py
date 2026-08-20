"""Artifact ingestion attempts and TS-frame reaction-inference provenance."""

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID

import numpy as np
import numpy.typing as npt
from pydantic import ConfigDict
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import deferred
from sqlmodel import Field, Relationship, SQLModel

from tricycle_reaction_db.db.models.base import created_at_field, uuid_primary_key_field
from tricycle_reaction_db.db.types import NumpyArray
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactKind,
    TransitionStateEndpointDirection,
    TransitionStateInferenceStatus,
    UploadBatchItemStatus,
    UploadBatchStatus,
    string_enum,
)

if TYPE_CHECKING:
    from tricycle_reaction_db.db.models.artifacts import ArtifactFile
    from tricycle_reaction_db.db.models.calculations import CalculationFrame, ParseRevision
    from tricycle_reaction_db.db.models.chemistry import MolecularTopology
    from tricycle_reaction_db.db.models.reactions import LogicalReaction, MappedReaction


class UploadBatch(SQLModel, table=True):
    """A durable client-managed queue whose files are uploaded independently."""

    __tablename__ = "upload_batch"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        CheckConstraint("total_count > 0", name="ck_upload_batch_total_count_positive"),
        CheckConstraint("total_bytes >= 0", name="ck_upload_batch_total_bytes_nonnegative"),
        CheckConstraint(
            "succeeded_count >= 0 AND failed_count >= 0 AND "
            "cancelled_count >= 0 AND uploading_count >= 0",
            name="ck_upload_batch_counts_nonnegative",
        ),
        CheckConstraint(
            "succeeded_count + failed_count + cancelled_count + uploading_count <= total_count",
            name="ck_upload_batch_counts_lte_total",
        ),
        Index(
            "ix_upload_batch_owner_created",
            "created_by_user_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("now()"),
        ),
    )
    project_id: UUID = Field(
        foreign_key="project.id",
        ondelete="RESTRICT",
        index=True,
        nullable=False,
    )
    created_by_user_id: UUID = Field(
        foreign_key="user_account.id",
        ondelete="RESTRICT",
        index=True,
        nullable=False,
    )
    artifact_kind: ArtifactKind = Field(
        sa_column=Column(
            string_enum(ArtifactKind, name="upload_batch_artifact_kind"),
            nullable=False,
        )
    )
    status: UploadBatchStatus = Field(
        default=UploadBatchStatus.ACTIVE,
        sa_column=Column(
            string_enum(UploadBatchStatus, name="upload_batch_status"),
            nullable=False,
            server_default=UploadBatchStatus.ACTIVE.value,
            index=True,
        ),
    )
    shared_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    total_count: int = Field(nullable=False)
    total_bytes: int = Field(sa_type=BigInteger, nullable=False)
    succeeded_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    failed_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    cancelled_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    uploading_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )


class UploadBatchItem(SQLModel, table=True):
    """One idempotently addressed file in an upload batch."""

    __tablename__ = "upload_batch_item"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint("batch_id", "client_file_id", name="uq_upload_batch_item_client"),
        UniqueConstraint("batch_id", "position", name="uq_upload_batch_item_position"),
        CheckConstraint("position >= 0", name="ck_upload_batch_item_position_nonnegative"),
        CheckConstraint("size_bytes >= 0", name="ck_upload_batch_item_size_nonnegative"),
        CheckConstraint("attempt_count >= 0", name="ck_upload_batch_item_attempts_nonnegative"),
        Index(
            "ix_upload_batch_item_batch_status_position",
            "batch_id",
            "status",
            "position",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("now()"),
        ),
    )
    batch_id: UUID = Field(
        foreign_key="upload_batch.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    client_file_id: UUID = Field(nullable=False)
    position: int = Field(nullable=False)
    original_filename: str = Field(sa_type=Text, nullable=False)
    relative_path: str = Field(sa_type=Text, nullable=False)
    size_bytes: int = Field(sa_type=BigInteger, nullable=False)
    media_type: str = Field(max_length=255, nullable=False)
    status: UploadBatchItemStatus = Field(
        default=UploadBatchItemStatus.QUEUED,
        sa_column=Column(
            string_enum(UploadBatchItemStatus, name="upload_batch_item_status"),
            nullable=False,
            server_default=UploadBatchItemStatus.QUEUED.value,
            index=True,
        ),
    )
    attempt_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    artifact_file_id: UUID | None = Field(
        default=None,
        foreign_key="artifact_file.id",
        ondelete="SET NULL",
        index=True,
    )
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, sa_type=Text)
    metadata_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )


class ArtifactIngestion(SQLModel, table=True):
    """One idempotent MolOP parse attempt for an uploaded calculation artifact."""

    __tablename__ = "artifact_ingestion"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        CheckConstraint(
            "source_frame_count IS NULL OR source_frame_count >= 0",
            name="ck_artifact_ingestion_source_frames_nonnegative",
        ),
        CheckConstraint(
            "transition_state_frame_count IS NULL OR transition_state_frame_count >= 0",
            name="ck_artifact_ingestion_ts_frames_nonnegative",
        ),
        CheckConstraint(
            "source_frame_count IS NULL OR transition_state_frame_count IS NULL OR "
            "transition_state_frame_count <= source_frame_count",
            name="ck_artifact_ingestion_ts_frames_lte_source",
        ),
        CheckConstraint(
            "completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at",
            name="ck_artifact_ingestion_timestamps_ordered",
        ),
        CheckConstraint(
            "status = 'pending' OR completed_at IS NOT NULL",
            name="ck_artifact_ingestion_terminal_timestamp",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    artifact_file_id: UUID = Field(
        foreign_key="artifact_file.id",
        ondelete="CASCADE",
        unique=True,
        nullable=False,
    )
    status: ArtifactIngestionStatus = Field(
        default=ArtifactIngestionStatus.PENDING,
        sa_column=Column(
            string_enum(ArtifactIngestionStatus, name="artifact_ingestion_status"),
            nullable=False,
            server_default=ArtifactIngestionStatus.PENDING.value,
            index=True,
        ),
    )
    parser_name: str = Field(default="molop", max_length=64, nullable=False)
    parser_version: str = Field(max_length=128, nullable=False)
    source_frame_count: int | None = Field(default=None)
    transition_state_frame_count: int | None = Field(default=None)
    started_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, sa_type=Text)
    parser_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    artifact_file: "ArtifactFile" = Relationship(back_populates="ingestion")
    transition_state_inferences: list["TransitionStateInference"] = Relationship(
        back_populates="artifact_ingestion",
        cascade_delete=True,
        passive_deletes=True,
    )


class TransitionStateInference(SQLModel, table=True):
    """Provenance linking one MolOP-confirmed TS frame to a shared reaction."""

    __tablename__ = "transition_state_inference"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "parse_revision_id",
            "file_frame_index",
            name="uq_transition_state_inference_revision_frame",
        ),
        CheckConstraint(
            "file_frame_index >= 0 AND imaginary_mode_index >= 0",
            name="ck_transition_state_inference_indices_nonnegative",
        ),
        CheckConstraint(
            "status <> 'succeeded' OR "
            "num_nonnulls(logical_reaction_id, mapped_reaction_id, calculation_frame_id) = 3",
            name="ck_transition_state_inference_succeeded_links",
        ),
        CheckConstraint(
            "status <> 'failed' OR "
            "num_nonnulls(logical_reaction_id, mapped_reaction_id, calculation_frame_id) = 0",
            name="ck_transition_state_inference_failed_links",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    artifact_ingestion_id: UUID = Field(
        foreign_key="artifact_ingestion.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    parse_revision_id: UUID = Field(
        foreign_key="parse_revision.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    file_frame_index: int = Field(nullable=False)
    imaginary_mode_index: int = Field(nullable=False)
    imaginary_frequency_cm1: float = Field(sa_type=Float, nullable=False)
    status: TransitionStateInferenceStatus = Field(
        sa_column=Column(
            string_enum(
                TransitionStateInferenceStatus,
                name="transition_state_inference_status",
            ),
            nullable=False,
            index=True,
        )
    )
    inference_method: str = Field(
        default="molop/possible_pre_post_ts",
        sa_column=Column(String(128), nullable=False),
    )
    inference_settings: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    logical_reaction_id: UUID | None = Field(
        default=None,
        foreign_key="logical_reaction.id",
        ondelete="RESTRICT",
        index=True,
    )
    mapped_reaction_id: UUID | None = Field(
        default=None,
        foreign_key="mapped_reaction.id",
        ondelete="RESTRICT",
        index=True,
    )
    calculation_frame_id: UUID | None = Field(
        default=None,
        foreign_key="calculation_frame.id",
        ondelete="RESTRICT",
        index=True,
    )
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, sa_type=Text)
    artifact_ingestion: ArtifactIngestion = Relationship(
        back_populates="transition_state_inferences"
    )
    parse_revision: "ParseRevision" = Relationship(back_populates="transition_state_inferences")
    logical_reaction: Optional["LogicalReaction"] = Relationship()
    mapped_reaction: Optional["MappedReaction"] = Relationship()
    calculation_frame: Optional["CalculationFrame"] = Relationship()


_transition_state_endpoint_coordinates_column: Column[npt.NDArray[np.generic]] = Column(
    "source_coordinates", NumpyArray(), nullable=False
)


class TransitionStateEndpoint(SQLModel, table=True):
    """One signed imaginary-mode endpoint anchored to a TS calculation frame.

    The coordinates remain in the MolOP source atom order so the two endpoints
    and the TS center can be interpolated without rebuilding intermediate
    reaction frames.  ``topology_id`` supplies the reusable endpoint graph.
    """

    __tablename__ = "transition_state_endpoint"  # pyright: ignore[reportAssignmentType]
    model_config = ConfigDict(arbitrary_types_allowed=True)  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint(
            "calculation_frame_id",
            "direction",
            name="uq_transition_state_endpoint_frame_direction",
        ),
        CheckConstraint("atom_count > 0", name="ck_transition_state_endpoint_atom_count_positive"),
        CheckConstraint(
            "displacement_ratio > 0",
            name="ck_transition_state_endpoint_displacement_ratio_positive",
        ),
        CheckConstraint(
            "cardinality(source_to_topology_atom_indices) = atom_count",
            name="ck_transition_state_endpoint_mapping_length",
        ),
        CheckConstraint(
            "source_coordinate_hash ~ '^[0-9a-f]{64}$'",
            name="ck_transition_state_endpoint_coordinate_hash_hex",
        ),
    )
    __mapper_args__ = {
        "properties": {
            "source_coordinates": deferred(
                _transition_state_endpoint_coordinates_column,
                raiseload=True,
            ),
        }
    }

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    calculation_frame_id: UUID = Field(
        foreign_key="calculation_frame.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    topology_id: UUID = Field(
        foreign_key="molecular_topology.id",
        ondelete="RESTRICT",
        index=True,
        nullable=False,
    )
    direction: TransitionStateEndpointDirection = Field(
        sa_column=Column(
            string_enum(
                TransitionStateEndpointDirection,
                name="transition_state_endpoint_direction",
            ),
            nullable=False,
        )
    )
    atom_count: int = Field(sa_type=Integer, nullable=False)
    displacement_ratio: float = Field(sa_type=Float, nullable=False)
    source_coordinates: npt.NDArray[np.generic] = Field(
        sa_column=_transition_state_endpoint_coordinates_column
    )
    source_coordinate_hash: str = Field(max_length=64, nullable=False)
    source_to_topology_atom_indices: list[int] = Field(
        sa_column=Column(ARRAY(Integer, dimensions=1), nullable=False),
    )
    provenance: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    calculation_frame: "CalculationFrame" = Relationship(
        back_populates="transition_state_endpoints"
    )
    topology: "MolecularTopology" = Relationship(back_populates="transition_state_endpoints")


__all__ = [
    "ArtifactIngestion",
    "TransitionStateEndpoint",
    "TransitionStateInference",
    "UploadBatch",
    "UploadBatchItem",
]
