"""Persistent watermarks and audit records for incremental object-store GC."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, Text, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel

from tricycle_reaction_db.db.models.base import created_at_field, uuid_primary_key_field
from tricycle_reaction_db.domain.enums import (
    StorageGarbageCollectionRunStatus,
    string_enum,
)


class StorageGarbageCollectionState(SQLModel, table=True):
    """Successful scan watermark for one bucket and managed key prefix."""

    __tablename__ = "storage_garbage_collection_state"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "bucket",
            "root_prefix",
            name="uq_storage_gc_state_bucket_prefix",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    bucket: str = Field(max_length=255, nullable=False)
    root_prefix: str = Field(sa_type=Text, nullable=False)
    watermark_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    last_successful_run_id: UUID | None = Field(
        default=None,
        nullable=True,
    )
    runs: list["StorageGarbageCollectionRun"] = Relationship(
        back_populates="state",
        sa_relationship_kwargs={
            "foreign_keys": "StorageGarbageCollectionRun.state_id",
        },
    )


class StorageGarbageCollectionRun(SQLModel, table=True):
    """One bounded incremental scan and its outcome counters."""

    __tablename__ = "storage_garbage_collection_run"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        CheckConstraint("scan_until >= scan_after", name="ck_storage_gc_run_window"),
        CheckConstraint("objects_seen >= 0", name="ck_storage_gc_run_seen_nonnegative"),
        CheckConstraint("objects_deleted >= 0", name="ck_storage_gc_run_deleted_nonnegative"),
        CheckConstraint("objects_retained >= 0", name="ck_storage_gc_run_retained_nonnegative"),
        CheckConstraint("objects_failed >= 0", name="ck_storage_gc_run_failed_nonnegative"),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    state_id: UUID = Field(
        foreign_key="storage_garbage_collection_state.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    started_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    scan_after: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    scan_until: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    status: StorageGarbageCollectionRunStatus = Field(
        sa_column=Column(
            string_enum(
                StorageGarbageCollectionRunStatus,
                name="storage_garbage_collection_run_status",
            ),
            nullable=False,
            index=True,
        )
    )
    objects_seen: int = Field(
        default=0,
        sa_column=Column(BigInteger, server_default="0", nullable=False),
    )
    objects_deleted: int = Field(
        default=0,
        sa_column=Column(BigInteger, server_default="0", nullable=False),
    )
    objects_retained: int = Field(
        default=0,
        sa_column=Column(BigInteger, server_default="0", nullable=False),
    )
    objects_failed: int = Field(
        default=0,
        sa_column=Column(BigInteger, server_default="0", nullable=False),
    )
    error_message: str | None = Field(default=None, sa_type=Text, nullable=True)
    state: StorageGarbageCollectionState = Relationship(
        back_populates="runs",
        sa_relationship_kwargs={
            "foreign_keys": "StorageGarbageCollectionRun.state_id",
        },
    )


__all__ = ["StorageGarbageCollectionRun", "StorageGarbageCollectionState"]
