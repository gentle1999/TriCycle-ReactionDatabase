"""Local users and project authorization relationships."""

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel

from tricycle_reaction_db.db.models.base import created_at_field, uuid_primary_key_field
from tricycle_reaction_db.domain.enums import (
    OrganizationRole,
    OrganizationStatus,
    ProjectRole,
    ProjectStatus,
    UserStatus,
    string_enum,
)

if TYPE_CHECKING:
    from tricycle_reaction_db.db.models.artifacts import ArtifactFile

_SLUG_PATTERN = "^[a-z0-9]+(?:-[a-z0-9]+)*$"


class UserAccount(SQLModel, table=True):
    __tablename__ = "user_account"  # pyright: ignore[reportAssignmentType]

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    display_name: str = Field(sa_type=Text, nullable=False)
    primary_email: str | None = Field(default=None, max_length=320, index=True)
    status: UserStatus = Field(
        default=UserStatus.ACTIVE,
        sa_column=Column(
            string_enum(UserStatus, name="user_account_status"),
            nullable=False,
            server_default=UserStatus.ACTIVE.value,
            index=True,
        ),
    )
    is_service_account: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default="false"),
    )
    last_authenticated_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    identities: list["ExternalIdentity"] = Relationship(
        back_populates="user",
        passive_deletes="all",
    )
    organization_memberships: list["OrganizationMembership"] = Relationship(
        back_populates="user",
        passive_deletes="all",
    )
    project_memberships: list["ProjectMembership"] = Relationship(
        back_populates="user",
        passive_deletes="all",
    )
    mcp_access_tokens: list["McpAccessToken"] = Relationship(
        back_populates="user",
        passive_deletes="all",
    )
    created_artifacts: list["ArtifactFile"] = Relationship(back_populates="created_by_user")


class ExternalIdentity(SQLModel, table=True):
    __tablename__ = "external_identity"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_external_identity_issuer_subject"),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    user_id: UUID = Field(
        foreign_key="user_account.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    issuer: str = Field(max_length=512, nullable=False)
    subject: str = Field(max_length=512, nullable=False)
    email: str | None = Field(default=None, max_length=320)
    claims: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    last_authenticated_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    user: UserAccount = Relationship(back_populates="identities")


class Organization(SQLModel, table=True):
    __tablename__ = "organization"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint("slug", name="uq_organization_slug"),
        CheckConstraint(f"slug ~ '{_SLUG_PATTERN}'", name="ck_organization_slug_format"),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    slug: str = Field(max_length=128, nullable=False)
    name: str = Field(sa_type=Text, nullable=False)
    status: OrganizationStatus = Field(
        default=OrganizationStatus.ACTIVE,
        sa_column=Column(
            string_enum(OrganizationStatus, name="organization_status"),
            nullable=False,
            server_default=OrganizationStatus.ACTIVE.value,
            index=True,
        ),
    )
    memberships: list["OrganizationMembership"] = Relationship(
        back_populates="organization",
        passive_deletes="all",
    )
    projects: list["Project"] = Relationship(
        back_populates="organization",
        passive_deletes="all",
    )


class OrganizationMembership(SQLModel, table=True):
    __tablename__ = "organization_membership"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "user_id",
            name="uq_organization_membership_organization_user",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    organization_id: UUID = Field(
        foreign_key="organization.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    user_id: UUID = Field(
        foreign_key="user_account.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    role: OrganizationRole = Field(
        sa_column=Column(
            string_enum(OrganizationRole, name="organization_membership_role"),
            nullable=False,
            index=True,
        )
    )
    organization: Organization = Relationship(back_populates="memberships")
    user: UserAccount = Relationship(back_populates="organization_memberships")


class Project(SQLModel, table=True):
    __tablename__ = "project"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint("organization_id", "slug", name="uq_project_organization_slug"),
        CheckConstraint(f"slug ~ '{_SLUG_PATTERN}'", name="ck_project_slug_format"),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    organization_id: UUID = Field(
        foreign_key="organization.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    slug: str = Field(max_length=128, nullable=False)
    name: str = Field(sa_type=Text, nullable=False)
    status: ProjectStatus = Field(
        default=ProjectStatus.ACTIVE,
        sa_column=Column(
            string_enum(ProjectStatus, name="project_status"),
            nullable=False,
            server_default=ProjectStatus.ACTIVE.value,
            index=True,
        ),
    )
    organization: Organization = Relationship(back_populates="projects")
    memberships: list["ProjectMembership"] = Relationship(
        back_populates="project",
        passive_deletes="all",
    )
    artifacts: list["ArtifactFile"] = Relationship(back_populates="project")


class ProjectMembership(SQLModel, table=True):
    __tablename__ = "project_membership"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "user_id",
            name="uq_project_membership_project_user",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    project_id: UUID = Field(
        foreign_key="project.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    user_id: UUID = Field(
        foreign_key="user_account.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    role: ProjectRole = Field(
        sa_column=Column(
            string_enum(ProjectRole, name="project_membership_role"),
            nullable=False,
            index=True,
        )
    )
    project: Project = Relationship(back_populates="memberships")
    user: UserAccount = Relationship(back_populates="project_memberships")


class AuthSession(SQLModel, table=True):
    """Opaque browser session; only the SHA-256 token digest is persisted."""

    __tablename__ = "auth_session"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        Index(
            "ix_auth_session_user_active_last_seen",
            "user_id",
            "last_seen_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    user_id: UUID = Field(
        foreign_key="user_account.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    token_hash: str = Field(max_length=64, unique=True, index=True, nullable=False)
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )
    last_seen_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    revoked_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True, index=True),
    )
    user_agent: str | None = Field(default=None, sa_type=Text)
    ip_address: str | None = Field(default=None, max_length=64)


class McpAccessToken(SQLModel, table=True):
    """Revocable bearer credential for external MCP clients.

    The raw value is returned only when the token is created.  Persisting its
    digest keeps a database read from being enough to impersonate the user.
    """

    __tablename__ = "mcp_access_token"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        Index(
            "ix_mcp_access_token_user_active",
            "user_id",
            "revoked_at",
            "expires_at",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    user_id: UUID = Field(
        foreign_key="user_account.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    name: str = Field(max_length=128, nullable=False)
    token_hash: str = Field(max_length=64, unique=True, index=True, nullable=False)
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )
    last_used_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    revoked_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True, index=True),
    )
    user: UserAccount = Relationship(back_populates="mcp_access_tokens")


class ProjectInvitation(SQLModel, table=True):
    """Single-use invitation that is redeemed after the recipient authenticates."""

    __tablename__ = "project_invitation"  # pyright: ignore[reportAssignmentType]

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    project_id: UUID = Field(
        foreign_key="project.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    invited_by_user_id: UUID = Field(
        foreign_key="user_account.id",
        ondelete="RESTRICT",
        index=True,
        nullable=False,
    )
    email: str = Field(max_length=320, index=True, nullable=False)
    role: ProjectRole = Field(
        sa_column=Column(
            string_enum(ProjectRole, name="project_invitation_role"),
            nullable=False,
            index=True,
        )
    )
    token_hash: str = Field(max_length=64, unique=True, index=True, nullable=False)
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )
    accepted_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    revoked_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    delivery_status: str = Field(
        default="pending",
        max_length=32,
        nullable=False,
        index=True,
        sa_column_kwargs={"server_default": "'link_only'"},
    )
    delivery_error: str | None = Field(default=None, sa_type=Text)
    delivery_sent_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )


class AuditEvent(SQLModel, table=True):
    """Append-only security and project-management audit record."""

    __tablename__ = "audit_event"  # pyright: ignore[reportAssignmentType]

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    actor_user_id: UUID | None = Field(
        default=None,
        foreign_key="user_account.id",
        ondelete="SET NULL",
        index=True,
    )
    project_id: UUID | None = Field(
        default=None,
        foreign_key="project.id",
        ondelete="SET NULL",
        index=True,
    )
    action: str = Field(max_length=128, index=True, nullable=False)
    entity_type: str = Field(max_length=128, nullable=False)
    entity_id: UUID | None = Field(default=None, index=True)
    metadata_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )


__all__ = [
    "AuditEvent",
    "AuthSession",
    "ExternalIdentity",
    "Organization",
    "OrganizationMembership",
    "Project",
    "ProjectInvitation",
    "ProjectMembership",
    "UserAccount",
]
