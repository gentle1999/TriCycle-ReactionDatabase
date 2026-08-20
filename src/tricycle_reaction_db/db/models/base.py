"""Field factories for immutable business entities."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Column, DateTime, text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlmodel import Field


def uuid_primary_key_field() -> Any:
    """Return a fresh PostgreSQL UUIDv7 primary-key field."""

    return Field(
        default=None,
        sa_column=Column(
            PostgreSQLUUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            server_default=text("uuidv7()"),
        ),
    )


def created_at_field() -> Any:
    """Return a fresh database-generated UTC timestamp field."""

    return Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("now()"),
        ),
    )


UUIDValue = UUID | None
CreatedAtValue = datetime | None

__all__ = ["CreatedAtValue", "UUIDValue", "created_at_field", "uuid_primary_key_field"]
