"""Validated DTOs for immutable artifacts and calculation protocols."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    ArtifactVisibility,
    QMSoftware,
    StorageStatus,
)
from tricycle_reaction_db.domain.identity import SYSTEM_PROJECT_ID, SYSTEM_USER_ID


class ArtifactFileRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_id: UUID = SYSTEM_PROJECT_ID
    created_by_user_id: UUID = SYSTEM_USER_ID
    visibility: ArtifactVisibility = ArtifactVisibility.PROJECT
    bucket: str
    object_key: str
    version_id: str | None = None
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    original_filename: str
    media_type: str
    artifact_kind: ArtifactKind
    storage_status: StorageStatus
    etag: str | None = None
    storage_verified_at: datetime | None = None


class CalculationProtocolRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    protocol_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec_schema_version: str
    qm_software: QMSoftware
    qm_software_version: str
    method_family: str | None = None
    method: str | None = None
    reference_method: str | None = None
    functional: str | None = None
    basis_set: str | None = None
    auxiliary_basis_set: str | None = None
    dispersion_model: str | None = None
    solvation_model: str | None = None
    solvent: str | None = None
    relativistic_method: str | None = None
    task_requests: list[str]
    normalized_spec: dict[str, Any]


__all__ = ["ArtifactFileRecord", "CalculationProtocolRecord"]
