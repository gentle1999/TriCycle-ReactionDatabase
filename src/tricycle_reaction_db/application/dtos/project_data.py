"""DTOs for destructive project-data lifecycle operations."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class ProjectDataRemovalPreview(BaseModel):
    """Counts collected before deleting a project's scientific data."""

    project_id: UUID
    project_slug: str
    artifact_count: int = Field(ge=0)
    upload_batch_count: int = Field(ge=0)
    upload_batch_item_count: int = Field(ge=0)
    ingestion_count: int = Field(ge=0)
    parse_revision_count: int = Field(ge=0)
    calculation_segment_count: int = Field(ge=0)
    calculation_frame_count: int = Field(ge=0)
    logical_reaction_count: int = Field(ge=0)
    mapped_reaction_count: int = Field(ge=0)
    geometry_count: int = Field(ge=0)
    topology_count: int = Field(ge=0)
    formula_count: int = Field(ge=0)
    protocol_count: int = Field(ge=0)
    topology_derivation_count: int = Field(ge=0)
    manifest_count: int = Field(ge=0)
    geometry_catalog_entry_count: int = Field(ge=0)
    geometry_catalog_count_row_count: int = Field(ge=0)
    units_ts_dataset_export_job_count: int = Field(ge=0)
    rustfs_object_count: int = Field(ge=0)
    processing_item_count: int = Field(ge=0)
    pending_ingestion_count: int = Field(ge=0)


class ProjectDataRemovalResult(ProjectDataRemovalPreview):
    """Counts removed by a project-data purge and its object-store cleanup."""

    rustfs_objects_deleted: int = Field(ge=0)
    rustfs_objects_pending: int = Field(ge=0)
    rustfs_cleanup_error: str | None = None


__all__ = ["ProjectDataRemovalPreview", "ProjectDataRemovalResult"]
