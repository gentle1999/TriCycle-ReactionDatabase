"""Raw artifact catalogue and reusable calculation protocol entities."""

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlmodel import Field, Relationship, SQLModel

from tricycle_reaction_db.core.chemistry_config import CALCULATION_PROTOCOL_VERSION
from tricycle_reaction_db.db.models.base import created_at_field, uuid_primary_key_field
from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    ArtifactVisibility,
    QMSoftware,
    StorageStatus,
    string_enum,
)
from tricycle_reaction_db.domain.identity import SYSTEM_PROJECT_ID, SYSTEM_USER_ID

if TYPE_CHECKING:
    from tricycle_reaction_db.db.models.calculations import CalculationSegment, ParseRevision
    from tricycle_reaction_db.db.models.identity import Project, UserAccount
    from tricycle_reaction_db.db.models.reactions import (
        ManifestArtifactBinding,
        WorkflowManifest,
    )
    from tricycle_reaction_db.db.models.uploads import ArtifactIngestion

_HASH_PATTERN = "^[0-9a-f]{64}$"


class ArtifactFile(SQLModel, table=True):
    """PostgreSQL catalogue entry for an immutable RustFS object."""

    __tablename__ = "artifact_file"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "content_sha256",
            name="uq_artifact_file_project_content",
        ),
        Index(
            "ix_artifact_file_object_reference",
            "bucket",
            "object_key",
            "storage_status",
            "id",
        ),
        Index(
            "ix_artifact_file_storage_status_created_at",
            "storage_status",
            "created_at",
        ),
        Index(
            "ix_artifact_file_original_filename_trgm",
            "original_filename",
            postgresql_using="gin",
            postgresql_ops={"original_filename": "gin_trgm_ops"},
        ),
        Index(
            "ix_artifact_file_project_status_created_id",
            "project_id",
            "storage_status",
            "created_at",
            "id",
        ),
        Index(
            "ix_artifact_file_visibility_status_created_id",
            "visibility",
            "storage_status",
            "created_at",
            "id",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_artifact_file_size_nonnegative"),
        CheckConstraint(
            f"content_sha256 ~ '{_HASH_PATTERN}'",
            name="ck_artifact_file_sha256_hex",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    project_id: UUID = Field(
        default=SYSTEM_PROJECT_ID,
        foreign_key="project.id",
        ondelete="RESTRICT",
        index=True,
        nullable=False,
    )
    created_by_user_id: UUID = Field(
        default=SYSTEM_USER_ID,
        foreign_key="user_account.id",
        ondelete="RESTRICT",
        index=True,
        nullable=False,
    )
    visibility: ArtifactVisibility = Field(
        default=ArtifactVisibility.PROJECT,
        sa_column=Column(
            string_enum(ArtifactVisibility, name="artifact_visibility"),
            nullable=False,
            server_default=ArtifactVisibility.PROJECT.value,
            index=True,
        ),
    )
    bucket: str = Field(max_length=255, nullable=False)
    object_key: str = Field(sa_type=Text, nullable=False)
    version_id: str | None = Field(default=None, sa_type=Text, nullable=True)
    content_sha256: str = Field(max_length=64, index=True, nullable=False)
    size_bytes: int = Field(sa_type=BigInteger, nullable=False)
    original_filename: str = Field(sa_type=Text, nullable=False)
    media_type: str = Field(max_length=255, nullable=False)
    artifact_kind: ArtifactKind = Field(
        sa_column=Column(
            string_enum(ArtifactKind, name="artifact_file_artifact_kind"),
            nullable=False,
            index=True,
        )
    )
    storage_status: StorageStatus = Field(
        default=StorageStatus.PENDING,
        sa_column=Column(
            string_enum(StorageStatus, name="artifact_file_storage_status"),
            nullable=False,
            server_default=StorageStatus.PENDING.value,
            index=True,
        ),
    )
    etag: str | None = Field(default=None, sa_type=Text, nullable=True)
    storage_verified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    parse_revisions: list["ParseRevision"] = Relationship(
        back_populates="artifact_file",
        passive_deletes="all",
    )
    workflow_manifest: Optional["WorkflowManifest"] = Relationship(
        back_populates="artifact_file",
        passive_deletes="all",
    )
    manifest_artifact_bindings: list["ManifestArtifactBinding"] = Relationship(
        back_populates="artifact_file",
        passive_deletes="all",
    )
    project: "Project" = Relationship(back_populates="artifacts")
    created_by_user: "UserAccount" = Relationship(back_populates="created_artifacts")
    ingestion: Optional["ArtifactIngestion"] = Relationship(
        back_populates="artifact_file",
        cascade_delete=True,
        passive_deletes=True,
    )


class CalculationProtocol(SQLModel, table=True):
    """Canonical, content-addressed calculation protocol."""

    __tablename__ = "calculation_protocol"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        CheckConstraint(
            f"protocol_hash ~ '{_HASH_PATTERN}'",
            name="ck_calculation_protocol_hash_hex",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    protocol_hash: str = Field(max_length=64, unique=True, nullable=False)
    spec_schema_version: str = Field(
        default=CALCULATION_PROTOCOL_VERSION,
        sa_column=Column(
            String(64),
            nullable=False,
            server_default=CALCULATION_PROTOCOL_VERSION,
        ),
    )
    qm_software: QMSoftware = Field(
        sa_column=Column(
            string_enum(QMSoftware, name="calculation_protocol_qm_software"),
            nullable=False,
        )
    )
    qm_software_version: str = Field(max_length=128, nullable=False)
    method_family: str | None = Field(default=None, max_length=128)
    method: str | None = Field(default=None, max_length=256, index=True)
    reference_method: str | None = Field(default=None, max_length=128)
    functional: str | None = Field(default=None, max_length=128)
    basis_set: str | None = Field(default=None, max_length=256, index=True)
    auxiliary_basis_set: str | None = Field(default=None, max_length=256)
    dispersion_model: str | None = Field(default=None, max_length=128)
    solvation_model: str | None = Field(default=None, max_length=128)
    solvent: str | None = Field(default=None, max_length=128, index=True)
    relativistic_method: str | None = Field(default=None, max_length=128)
    task_requests: list[str] = Field(
        default_factory=list,
        sa_column=Column(ARRAY(Text, dimensions=1), nullable=False),
    )
    normalized_spec: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    segments: list["CalculationSegment"] = Relationship(
        back_populates="protocol",
        passive_deletes="all",
    )


__all__ = ["ArtifactFile", "CalculationProtocol"]
