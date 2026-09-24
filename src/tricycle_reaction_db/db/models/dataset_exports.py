"""Durable metadata for generated machine-learning dataset exports."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from tricycle_reaction_db.db.models.base import created_at_field, uuid_primary_key_field
from tricycle_reaction_db.domain.enums import UnitsDatasetExportJobStatus


class UnitsTsDatasetExportJob(SQLModel, table=True):
    """One asynchronous UniTS-compatible TS geometry dataset build."""

    __tablename__ = "units_ts_dataset_export_job"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed', 'expired')",
            name="ck_units_ts_dataset_export_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_units_ts_dataset_export_attempts"),
        CheckConstraint(
            "sample_count >= 0 AND skipped_count >= 0",
            name="ck_units_ts_dataset_export_counts",
        ),
        CheckConstraint(
            "content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_units_ts_dataset_export_sha256",
        ),
        Index(
            "ix_units_ts_dataset_export_claim",
            "status",
            "available_at",
            "requested_at",
        ),
        Index(
            "ix_units_ts_dataset_export_lease",
            "status",
            "lease_expires_at",
        ),
        Index(
            "ix_units_ts_dataset_export_expiration",
            "status",
            "expires_at",
        ),
        UniqueConstraint(
            "download_token_hash",
            name="uq_units_ts_dataset_export_token_hash",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    project_id: UUID = Field(
        foreign_key="project.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    requested_by_user_id: UUID | None = Field(
        default=None,
        foreign_key="user_account.id",
        ondelete="SET NULL",
        index=True,
    )
    download_token_hash: str = Field(sa_type=Text, nullable=False)
    status: UnitsDatasetExportJobStatus = Field(
        default=UnitsDatasetExportJobStatus.PENDING,
        sa_column=Column(
            Text,
            nullable=False,
            server_default=UnitsDatasetExportJobStatus.PENDING.value,
        ),
    )
    object_key: str | None = Field(default=None, sa_type=Text)
    bucket: str | None = Field(default=None, max_length=255)
    size_bytes: int | None = Field(default=None, sa_type=BigInteger)
    content_sha256: str | None = Field(default=None, max_length=64)
    sample_count: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default="0")
    )
    skipped_count: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default="0")
    )
    skip_reasons: dict[str, int] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    error_message: str | None = Field(default=None, sa_type=Text)
    requested_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("now()"),
        ),
    )
    available_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("now()"),
        ),
    )
    lease_id: UUID | None = Field(default=None)
    lease_expires_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    attempt_count: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default="0")
    )
    created_at: datetime | None = created_at_field()
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("now()"),
        ),
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


__all__ = ["UnitsTsDatasetExportJob"]
