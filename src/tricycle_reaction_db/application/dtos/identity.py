"""Authenticated-user and project-access views."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tricycle_reaction_db.domain.enums import (
    OrganizationRole,
    OrganizationStatus,
    ProjectRole,
    ProjectStatus,
    UserStatus,
)

_SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


class IdentityView(BaseModel):
    model_config = ConfigDict(frozen=True)

    issuer: str
    subject: str


class ProjectAccessView(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_id: UUID
    project_slug: str
    project_name: str
    organization_id: UUID
    organization_slug: str
    organization_name: str
    organization_role: OrganizationRole | None = None
    project_role: ProjectRole | None = None
    permissions: list[str]


class CurrentUserView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    display_name: str
    primary_email: str | None = None
    is_service_account: bool
    identity: IdentityView
    projects: list[ProjectAccessView]


class OrganizationAccessView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    slug: str
    name: str
    status: OrganizationStatus
    role: OrganizationRole | None = None
    can_create_projects: bool = False


class OrganizationCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    slug: str = Field(min_length=1, max_length=128, pattern=_SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=512)

    @field_validator("name")
    @classmethod
    def require_nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("organization name must not be blank")
        return value.strip()


class ProjectCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    organization_id: UUID
    slug: str = Field(min_length=1, max_length=128, pattern=_SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=512)


class ProjectUpdate(BaseModel):
    model_config = ConfigDict(frozen=True)

    slug: str | None = Field(default=None, min_length=1, max_length=128, pattern=_SLUG_PATTERN)
    name: str | None = Field(default=None, min_length=1, max_length=512)
    status: ProjectStatus | None = None


class ProjectView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    organization_id: UUID
    organization_slug: str
    organization_name: str
    slug: str
    name: str
    status: ProjectStatus
    role: ProjectRole | None = None
    organization_role: OrganizationRole | None = None
    permissions: list[str] = Field(default_factory=list)
    created_at: datetime | None = None


class ProjectMemberView(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: UUID
    display_name: str
    primary_email: str | None = None
    role: ProjectRole
    created_at: datetime | None = None


class ProjectMemberUpsert(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: UUID
    role: ProjectRole = ProjectRole.VIEWER


class ProjectMemberRoleUpdate(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: ProjectRole


class UserSummaryView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    display_name: str
    primary_email: str | None = None
    status: UserStatus
    is_service_account: bool
    last_authenticated_at: datetime | None = None
    created_at: datetime | None = None
    project_role: ProjectRole | None = None


class UserPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[UserSummaryView]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class UserStatusUpdate(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: UserStatus


class AuthConfigView(BaseModel):
    model_config = ConfigDict(frozen=True)

    oidc_enabled: bool
    login_path: str


class SessionView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    created_at: datetime | None = None
    expires_at: datetime
    last_seen_at: datetime
    user_agent: str | None = None
    ip_address: str | None = None
    current: bool


class McpAccessTokenCreate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(default="MCP client", min_length=1, max_length=128)

    @field_validator("name")
    @classmethod
    def require_nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("MCP token name must not be blank")
        return value.strip()


class McpAccessTokenView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    created_at: datetime | None = None
    expires_at: datetime
    last_used_at: datetime | None = None


class McpAccessTokenCreateResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    token: McpAccessTokenView
    access_token: str


class UserProfileUpdate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    display_name: str = Field(min_length=1, max_length=512)


class ProjectInvitationCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    email: str = Field(min_length=3, max_length=320)
    role: ProjectRole = ProjectRole.VIEWER
    expires_in_days: int = Field(default=7, ge=1, le=30)


class ProjectInvitationView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    project_id: UUID
    email: str
    role: ProjectRole
    created_at: datetime | None = None
    expires_at: datetime
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None
    delivery_status: str = "link_only"
    delivery_error: str | None = None


class ProjectInvitationCreateResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    invitation: ProjectInvitationView
    accept_token: str
    accept_url: str
    delivery_status: str = "link_only"
    delivery_error: str | None = None


class AuditEventView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    created_at: datetime | None = None
    actor_user_id: UUID | None = None
    project_id: UUID | None = None
    action: str
    entity_type: str
    entity_id: UUID | None = None
    metadata_json: dict[str, object]


__all__ = [
    "CurrentUserView",
    "OrganizationAccessView",
    "OrganizationCreate",
    "AuthConfigView",
    "AuditEventView",
    "IdentityView",
    "ProjectAccessView",
    "ProjectCreate",
    "ProjectMemberRoleUpdate",
    "ProjectMemberUpsert",
    "ProjectMemberView",
    "ProjectInvitationCreate",
    "ProjectInvitationView",
    "ProjectUpdate",
    "ProjectView",
    "UserPage",
    "UserProfileUpdate",
    "SessionView",
    "UserStatusUpdate",
    "UserSummaryView",
]
