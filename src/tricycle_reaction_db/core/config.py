from email.utils import parseaddr
from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from tricycle_reaction_db.domain.identity import DEVELOPMENT_USER_ID


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TRICYCLE_",
        extra="ignore",
    )

    # Deployment-facing labels. Keep protocol/package identifiers stable; these
    # values are safe to replace per installation through TRICYCLE_* variables.
    app_name: str = Field(default="Example Chemistry Database", min_length=1)
    brand_name: str = Field(default="Example Research Platform", min_length=1)
    mcp_server_name: str = Field(default="Example Chemistry Database MCP", min_length=1)
    nexusx_app_name: str = Field(
        default="example-chemistry-database",
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    nexusx_playground_name: str = Field(
        default="example-chemistry-database-playground",
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    nexusx_database_cluster_name: str = Field(
        default="example-chemistry-postgresql",
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    nexusx_database_cluster_color: str = Field(
        default="#E3F2FD",
        pattern=r"^#[0-9A-Fa-f]{6}$",
    )
    environment: Literal["development", "test", "production"] = "development"
    debug: bool = False
    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8000, ge=1, le=65535)
    # Per-file cap remains separate from the batch budget so callers cannot
    # trade one unbounded dimension for another.
    max_upload_bytes: int = Field(default=64 * 1024 * 1024, ge=1024)
    max_batch_files: int = Field(default=64, ge=1, le=10_000)
    max_batch_bytes: int = Field(default=512 * 1024 * 1024, ge=1024, le=4 * 1024 * 1024 * 1024)
    max_upload_queue_files: int = Field(default=20_000, ge=1, le=100_000)
    max_upload_queue_bytes: int = Field(
        default=1024 * 1024 * 1024 * 1024,
        ge=1024,
        le=16 * 1024 * 1024 * 1024 * 1024,
    )
    max_upload_metadata_bytes: int = Field(default=16 * 1024, ge=128, le=1024 * 1024)
    database_url: str = (
        "postgresql+psycopg://example_user:example-local-password@127.0.0.1:5432/"
        "example_reaction_db"
    )
    query_statement_timeout_ms: int = Field(default=15_000, ge=100, le=300_000)
    slow_query_threshold_ms: int = Field(default=500, ge=1, le=300_000)
    graphql_max_query_characters: int = Field(default=20_000, ge=100, le=1_000_000)
    graphql_max_tokens: int = Field(default=2_000, ge=10, le=100_000)
    graphql_max_depth: int = Field(default=12, ge=1, le=100)
    graphql_max_complexity: int = Field(default=250, ge=1, le=100_000)
    query_rate_limit_requests: int = Field(default=120, ge=1, le=1_000_000)
    read_rate_limit_requests: int = Field(default=10_000, ge=1, le=1_000_000)
    upload_rate_limit_requests: int = Field(default=1_000, ge=1, le=1_000_000)
    upload_max_concurrency: int = Field(default=8, ge=1, le=128)
    # MolOP 0.2.11 collects frame roles and source locators without implicitly
    # reconstructing molecular graphs. Keep evidence enabled so optimization
    # frames retain their initial/intermediate/terminal role during ingestion.
    # Deployments may still disable it explicitly for legacy fast-ingestion
    # behavior, but those frames cannot provide MolOP's evidence-derived role.
    molop_capture_source_evidence: bool = True
    # Fast ingestion batches revision-local frame rows in one transaction.
    # Evidence capture no longer disables deferred topology reconstruction.
    molop_parallel_frame_persistence: bool = True
    # Baseline end-to-end budget for a 10 MiB source; larger files scale this
    # budget proportionally while smaller files retain the baseline.
    molop_file_parse_timeout_seconds: float = Field(default=60.0, gt=0.0, le=86400.0)
    molecule_query_rate_limit_requests: int = Field(default=10_000, ge=1, le=1_000_000)
    depiction_rate_limit_requests: int = Field(default=10_000, ge=1, le=1_000_000)
    query_rate_limit_window_seconds: int = Field(default=60, ge=1, le=86_400)
    rate_limit_backend: Literal["memory", "redis"] = "memory"
    rate_limit_redis_url: str | None = None
    rate_limit_key_prefix: str = Field(
        default="reaction-database",
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9]+(?:[-_:][a-z0-9]+)*$",
    )
    structure_query_max_characters: int = Field(default=16_384, ge=100, le=1_000_000)
    structure_candidate_limit: int = Field(default=50_000, ge=1, le=10_000_000)
    molop_batch_n_jobs: int = Field(default=2, ge=-1)
    auth_mode: Literal["development", "oidc"] = "development"
    development_user_id: UUID = DEVELOPMENT_USER_ID
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_ca_bundle: str | None = None
    oidc_algorithm: Literal["RS256", "ES256"] = "RS256"
    oidc_client_id: str = "example-chemistry-database"
    oidc_client_secret: str | None = None
    oidc_redirect_uri: str | None = None
    oidc_frontend_url: str = "http://127.0.0.1:5173"
    oidc_bootstrap_subject: str | None = None
    oidc_bootstrap_user_id: UUID | None = None
    bootstrap_oidc_issuer: str | None = None
    bootstrap_oidc_subject: str | None = None
    bootstrap_admin_email: str | None = None
    bootstrap_admin_display_name: str | None = None
    bootstrap_organization_slug: str | None = None
    bootstrap_organization_name: str | None = None
    bootstrap_project_slug: str | None = None
    bootstrap_project_name: str | None = None
    bootstrap_system_user_display_name: str = "Application System"
    bootstrap_development_user_display_name: str = "Development User"
    bootstrap_development_user_email: str = "developer@localhost"
    session_secret: str = "development-session-secret-change-me"
    session_cookie_name: str = Field(
        default="example_session",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    csrf_cookie_name: str = Field(
        default="example_csrf",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    csrf_header_name: str = Field(
        default="x-csrf-token",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    session_ttl_seconds: int = Field(default=86_400, ge=300, le=2_592_000)
    mcp_token_ttl_seconds: int = Field(default=31_536_000, ge=300, le=31_536_000 * 5)
    session_cookie_secure: bool = False
    email_delivery_mode: Literal["link", "smtp"] = "link"
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_email: str | None = None
    smtp_starttls: bool = True
    smtp_ca_bundle: str | None = None
    smtp_timeout_seconds: int = Field(default=15, ge=1, le=120)
    rustfs_endpoint_url: str = "http://127.0.0.1:19000"
    rustfs_verify_tls: bool = True
    rustfs_ca_bundle: str | None = None
    cors_origins: list[str] = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:5174",
        "http://localhost:5174",
    ]

    @field_validator("database_url")
    @classmethod
    def require_psycopg_postgresql(cls, value: str) -> str:
        if not value.startswith("postgresql+psycopg://"):
            raise ValueError("database_url must use the postgresql+psycopg driver")
        return value

    @field_validator(
        "app_name",
        "brand_name",
        "mcp_server_name",
        "bootstrap_system_user_display_name",
        "bootstrap_development_user_display_name",
    )
    @classmethod
    def normalize_display_label(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("display labels must not be blank")
        return normalized

    @field_validator("smtp_from_email")
    @classmethod
    def require_plain_smtp_mailbox(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        display_name, mailbox = parseaddr(normalized)
        if display_name or mailbox != normalized or mailbox.count("@") != 1:
            raise ValueError("smtp_from_email must be a plain mailbox address")
        local_part, domain = mailbox.rsplit("@", 1)
        if (
            not local_part
            or any(character.isspace() or ord(character) < 32 for character in local_part)
            or not _is_dns_name(domain)
        ):
            raise ValueError("smtp_from_email must use a valid DNS domain")
        return normalized

    @field_validator("oidc_ca_bundle", "rustfs_ca_bundle", "smtp_ca_bundle")
    @classmethod
    def require_absolute_ca_bundle(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or not Path(normalized).is_absolute():
            raise ValueError("CA bundle must be an absolute PEM path")
        return normalized

    @field_validator("molop_batch_n_jobs")
    @classmethod
    def require_nonzero_molop_batch_jobs(cls, value: int) -> int:
        if value == 0:
            raise ValueError("molop_batch_n_jobs must be -1 or a positive integer")
        return value

    @model_validator(mode="after")
    def validate_authentication(self) -> "Settings":
        if self.environment == "production" and self.auth_mode != "oidc":
            raise ValueError("production requires TRICYCLE_AUTH_MODE=oidc")
        if self.auth_mode == "oidc" and not all(
            (self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url)
        ):
            raise ValueError("OIDC mode requires issuer, audience, and JWKS URL")
        if self.environment == "production":
            if self.debug:
                raise ValueError("production requires TRICYCLE_DEBUG=false")
            if not all((self.oidc_client_id, self.oidc_client_secret, self.oidc_redirect_uri)):
                raise ValueError(
                    "production OIDC requires client ID, client secret, and redirect URI"
                )
            if self.session_secret == "development-session-secret-change-me":
                raise ValueError("production requires a non-default session_secret")
            if not self.session_cookie_secure:
                raise ValueError("production requires a Secure session cookie")
            if self.auth_mode == "oidc":
                for name in (
                    "oidc_issuer",
                    "oidc_redirect_uri",
                    "oidc_jwks_url",
                    "oidc_frontend_url",
                ):
                    value = getattr(self, name)
                    if not isinstance(value, str) or not value.startswith("https://"):
                        raise ValueError(f"production requires HTTPS {name}")
                if not _is_exact_nonlocal_https_origin(self.oidc_frontend_url):
                    raise ValueError(
                        "production oidc_frontend_url must be an exact non-localhost HTTPS origin"
                    )
            database_url = make_url(self.database_url)
            if database_url.query.get("sslmode") != "verify-full":
                raise ValueError("production database_url requires sslmode=verify-full")
            if not self.cors_origins:
                raise ValueError("production requires at least one exact HTTPS CORS origin")
            for origin in self.cors_origins:
                if not _is_exact_nonlocal_https_origin(origin):
                    raise ValueError(
                        "production CORS origins must be exact non-localhost HTTPS origins"
                    )
            rustfs_url = urlsplit(self.rustfs_endpoint_url)
            if rustfs_url.scheme != "https" or not rustfs_url.netloc:
                raise ValueError("production requires an HTTPS RustFS/S3 endpoint")
            if not self.rustfs_verify_tls:
                raise ValueError("production requires RustFS/S3 TLS verification")
            if self.email_delivery_mode != "smtp":
                raise ValueError("production requires SMTP invitation delivery")
            if not self.smtp_starttls:
                raise ValueError("production SMTP requires STARTTLS")
            if self.smtp_port == 465:
                raise ValueError(
                    "production SMTP port 465 is unsupported; use STARTTLS on port 587"
                )
            if self.smtp_from_email is None:
                raise ValueError("production SMTP requires a valid verified sender address")
            if self.rate_limit_backend != "redis":
                raise ValueError("production requires the Redis shared rate-limit backend")
            if not self.rate_limit_redis_url:
                raise ValueError("production requires TRICYCLE_RATE_LIMIT_REDIS_URL")
            redis_url = urlsplit(self.rate_limit_redis_url)
            if redis_url.scheme != "rediss" or not redis_url.hostname:
                raise ValueError("production rate limiting requires a rediss:// URL")
        if self.environment == "production" and self.molop_batch_n_jobs == -1:
            raise ValueError("production must set molop_batch_n_jobs to a positive limit")
        if len(self.session_secret) < 32:
            raise ValueError("session_secret must contain at least 32 characters")
        if self.email_delivery_mode == "smtp" and not self.smtp_host:
            raise ValueError("SMTP mode requires TRICYCLE_SMTP_HOST")
        if self.email_delivery_mode == "smtp" and not self.smtp_from_email:
            raise ValueError("SMTP mode requires TRICYCLE_SMTP_FROM_EMAIL")
        return self


def _is_loopback_host(host: str) -> bool:
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _is_exact_nonlocal_https_origin(value: str) -> bool:
    parsed = urlsplit(value)
    host = parsed.hostname
    return bool(
        parsed.scheme == "https"
        and host
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
        and host != "localhost"
        and not _is_loopback_host(host)
        and "*" not in value
    )


def _is_dns_name(value: str) -> bool:
    try:
        ascii_value = value.rstrip(".").encode("idna").decode("ascii")
    except UnicodeError:
        return False
    labels = ascii_value.split(".")
    return (
        len(labels) >= 2
        and len(ascii_value) <= 253
        and all(
            label
            and len(label) <= 63
            and label[0].isalnum()
            and label[-1].isalnum()
            and all(character.isalnum() or character == "-" for character in label)
            for label in labels
        )
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
