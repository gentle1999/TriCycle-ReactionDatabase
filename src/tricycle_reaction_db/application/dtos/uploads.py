"""DTOs for authenticated artifact ingestion and durable upload queues."""

from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactKind,
    ArtifactVisibility,
    StorageStatus,
    TransitionStateInferenceStatus,
    UploadBatchItemStatus,
    UploadBatchStatus,
)


class TransitionStateInferenceView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    parse_revision_id: UUID
    file_frame_index: int = Field(ge=0)
    imaginary_mode_index: int = Field(ge=0)
    imaginary_frequency_cm1: float
    status: TransitionStateInferenceStatus
    logical_reaction_id: UUID | None = None
    mapped_reaction_id: UUID | None = None
    calculation_frame_id: UUID | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_metadata_json: str | None = None


class ArtifactUploadResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    artifact_id: UUID
    artifact_kind: ArtifactKind
    storage_status: StorageStatus
    ingestion_id: UUID | None = None
    parse_revision_id: UUID | None = None
    parse_revision_created: bool | None = None
    ingestion_status: ArtifactIngestionStatus | None = None
    source_frame_count: int | None = Field(default=None, ge=0)
    transition_state_frame_count: int | None = Field(default=None, ge=0)
    inferred_reaction_count: int = Field(ge=0)
    inferences: list[TransitionStateInferenceView]


class ArtifactValidationInferenceView(BaseModel):
    model_config = ConfigDict(frozen=True)

    file_frame_index: int = Field(ge=0)
    imaginary_mode_index: int = Field(ge=0)
    imaginary_frequency_cm1: float
    succeeded: bool
    reaction_smiles: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_metadata_json: str | None = None


class ArtifactValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    filename: str
    source_format: str | None = None
    source_compression: str | None = None
    source_frame_count: int = Field(ge=0)
    transition_state_frame_count: int = Field(ge=0)
    successful_inference_count: int = Field(ge=0)
    failed_inference_count: int = Field(ge=0)
    inferences: list[ArtifactValidationInferenceView]


class ArtifactBatchUploadItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    filename: str
    succeeded: bool
    result: ArtifactUploadResult | None = None
    error_code: str | None = None
    error_message: str | None = None


class ArtifactBatchUploadResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_count: int = Field(ge=0)
    succeeded_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    source_frame_count: int = Field(ge=0)
    transition_state_frame_count: int = Field(ge=0)
    inferred_reaction_count: int = Field(ge=0)
    timings_ms: dict[str, float] = Field(default_factory=dict)
    items: list[ArtifactBatchUploadItem]


class ArtifactMetadataUpdate(BaseModel):
    model_config = ConfigDict(frozen=True)

    original_filename: str | None = Field(default=None, min_length=1, max_length=1024)
    visibility: ArtifactVisibility | None = None


class UploadBatchFileCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    client_file_id: UUID
    original_filename: str = Field(min_length=1, max_length=1024)
    relative_path: str = Field(min_length=1, max_length=4096)
    size_bytes: int = Field(ge=0)
    media_type: str = Field(default="application/octet-stream", min_length=1, max_length=255)

    @field_validator("original_filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "/" in normalized or "\\" in normalized:
            raise ValueError("original_filename must be a plain filename")
        return normalized

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        path = PurePosixPath(normalized)
        if not normalized or path.is_absolute() or ".." in path.parts:
            raise ValueError("relative_path must stay within the selected directory")
        return str(path)


class UploadBatchCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_id: UUID
    artifact_kind: ArtifactKind = ArtifactKind.CALCULATION_OUTPUT
    shared_metadata: dict[str, Any] = Field(default_factory=dict)
    files: list[UploadBatchFileCreate] = Field(min_length=1, max_length=100_000)

    @model_validator(mode="after")
    def validate_unique_files(self) -> "UploadBatchCreate":
        client_ids = {item.client_file_id for item in self.files}
        if len(client_ids) != len(self.files):
            raise ValueError("client_file_id must be unique within an upload batch")
        paths = {item.relative_path for item in self.files}
        if len(paths) != len(self.files):
            raise ValueError("relative_path must be unique within an upload batch")
        return self


class UploadBatchStatusUpdate(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal[UploadBatchStatus.ACTIVE, UploadBatchStatus.PAUSED]


class UploadBatchView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    created_at: datetime
    updated_at: datetime
    project_id: UUID
    created_by_user_id: UUID
    artifact_kind: ArtifactKind
    status: UploadBatchStatus
    shared_metadata: dict[str, Any]
    total_count: int = Field(ge=1)
    total_bytes: int = Field(ge=0)
    succeeded_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    cancelled_count: int = Field(ge=0)
    uploading_count: int = Field(ge=0)


class UploadBatchPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[UploadBatchView]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class UploadBatchItemView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    created_at: datetime
    updated_at: datetime
    client_file_id: UUID
    position: int = Field(ge=0)
    original_filename: str
    relative_path: str
    size_bytes: int = Field(ge=0)
    media_type: str
    status: UploadBatchItemStatus
    attempt_count: int = Field(ge=0)
    artifact_file_id: UUID | None = None
    ingestion_status: ArtifactIngestionStatus | None = None
    ingestion_error_message: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any]


class UploadBatchItemPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[UploadBatchItemView]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


__all__ = [
    "ArtifactBatchUploadItem",
    "ArtifactBatchUploadResult",
    "ArtifactMetadataUpdate",
    "ArtifactUploadResult",
    "ArtifactValidationInferenceView",
    "ArtifactValidationResult",
    "TransitionStateInferenceView",
    "UploadBatchCreate",
    "UploadBatchFileCreate",
    "UploadBatchItemPage",
    "UploadBatchItemView",
    "UploadBatchPage",
    "UploadBatchStatusUpdate",
    "UploadBatchView",
]
