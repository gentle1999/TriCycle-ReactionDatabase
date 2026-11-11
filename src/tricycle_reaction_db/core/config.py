from email.utils import parseaddr
from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from tricycle_reaction_db.domain.identity import DEVELOPMENT_USER_ID


class _TricycleSettingsBase(BaseSettings):
    """Shared environment source for independently loadable application domains."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TRICYCLE_",
        extra="ignore",
    )


class RuntimeSettings(_TricycleSettingsBase):
    """Runtime query environment settings."""

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
    geometry_match_input_batch_size: int = Field(default=128, ge=1, le=100_000)
    analytics_histogram_bin_count: int = Field(default=12, ge=1, le=1_000)
    analytics_max_scatter_points: int = Field(default=1_000, ge=1, le=1_000_000)
    database_url: str = (
        "postgresql+psycopg://example_user:example-local-password@127.0.0.1:5432/"
        "example_reaction_db"
    )
    # Size the shared SQLAlchemy pool explicitly when the durable worker is
    # configured to process more than the conservative default batch.
    database_pool_size: int = Field(default=5, ge=1, le=256)
    database_max_overflow: int = Field(default=10, ge=0, le=512)
    database_pool_timeout_seconds: float = Field(default=30.0, gt=0.0, le=600.0)
    query_statement_timeout_ms: int = Field(default=15_000, ge=100, le=300_000)
    slow_query_threshold_ms: int = Field(default=500, ge=1, le=300_000)
    graphql_max_query_characters: int = Field(default=20_000, ge=100, le=1_000_000)
    graphql_max_tokens: int = Field(default=2_000, ge=10, le=100_000)
    graphql_max_depth: int = Field(default=12, ge=1, le=100)
    graphql_max_complexity: int = Field(default=250, ge=1, le=100_000)
    query_rate_limit_requests: int = Field(default=120, ge=1, le=1_000_000)
    read_rate_limit_requests: int = Field(default=10_000, ge=1, le=1_000_000)
    # RDKit does not expose a native wall-clock timeout. Large full-graph
    # matches are therefore isolated in a terminable child process.
    molecular_graph_match_timeout_seconds: float = Field(default=5.0, gt=0.0, le=300.0)
    molecular_graph_match_isolation_atom_count: int = Field(default=48, ge=1, le=100_000)
    molecular_graph_match_max_results: int = Field(default=1_000, ge=1, le=100_000)
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

    @field_validator("database_url")
    @classmethod
    def require_psycopg_postgresql(cls, value: str) -> str:
        if not value.startswith("postgresql+psycopg://"):
            raise ValueError("database_url must use the postgresql+psycopg driver")
        return value

    @field_validator("app_name", "brand_name", "mcp_server_name")
    @classmethod
    def normalize_display_label(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("display labels must not be blank")
        return normalized


class McpExportSettings(_TricycleSettingsBase):
    """Mcp export environment settings."""

    mcp_ts_geometry_export_max_bytes: int = Field(default=512 * 1024, ge=1024, le=64 * 1024 * 1024)
    mcp_ts_geometry_export_max_records: int = Field(default=100, ge=1, le=10_000)
    mcp_export_max_records: int = Field(default=100, ge=1, le=10_000)
    mcp_export_max_bytes: int = Field(default=512 * 1024, ge=1024, le=64 * 1024 * 1024)
    mcp_artifact_preview_max_bytes: int = Field(default=256 * 1024, ge=1024, le=64 * 1024 * 1024)
    mcp_batch_max_files: int = Field(default=100, ge=1, le=10_000)
    mcp_array_preview_max_elements: int = Field(default=4096, ge=1, le=1_000_000)
    mcp_batch_download_concurrency: int = Field(default=16, ge=1, le=128)
    mcp_csv_max_offset: int = Field(default=1_000_000, ge=0, le=100_000_000)
    mapped_geometry_export_page_size: int = Field(default=64, ge=1, le=10_000)
    units_ts_export_page_size: int = Field(default=128, ge=1, le=10_000)
    units_ts_download_ttl_seconds: int = Field(default=7 * 86_400, ge=60, le=31_536_000)
    units_ts_job_lease_seconds: int = Field(default=30 * 60, ge=60, le=86_400)
    units_ts_max_reactive_atoms: int = Field(default=20, ge=1, le=10_000)
    units_ts_max_rc_bonds: int = Field(default=25, ge=1, le=1_000_000)
    units_ts_max_rc_angles: int = Field(default=25, ge=1, le=1_000_000)
    units_ts_legacy_export_purge_interval_seconds: float = Field(default=300.0, gt=0.0, le=86_400.0)
    units_dataset_worker_poll_interval_seconds: float = Field(
        default=2.0,
        gt=0.05,
        le=300.0,
    )


class IngestionWorkerSettings(_TricycleSettingsBase):
    """Ingestion workers environment settings."""

    frame_conversion_chunk_size: int = Field(default=32, ge=1, le=10_000)
    inference_persist_batch_size: int = Field(default=16, ge=1, le=10_000)
    transaction_retry_limit: int = Field(default=3, ge=0, le=20)
    molop_parse_timeout_reference_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    import_hash_chunk_bytes: int = Field(default=1024 * 1024, ge=1024)
    import_max_fingerprint_workers: int = Field(default=32, ge=1, le=256)
    import_commit_batch_files: int = Field(default=16, ge=1, le=10_000)
    import_pipeline_window_files: int = Field(default=64, ge=1, le=100_000)
    import_stream_queue_size: int = Field(default=64, ge=1, le=100_000)
    import_max_transient_retries: int = Field(default=3, ge=0, le=20)
    import_transient_retry_backoff_seconds: float = Field(default=0.25, ge=0.0, le=60.0)
    reconcile_default_batch_size: int = Field(default=100, ge=1, le=100_000)
    reconcile_default_statement_timeout_ms: int = Field(default=300_000, ge=100, le=3_600_000)
    reconcile_max_statement_timeout_ms: int = Field(default=3_600_000, ge=100, le=86_400_000)
    reconcile_default_reaction_timeout_seconds: float = Field(default=300.0, gt=0.0, le=86_400.0)
    reconcile_max_reaction_timeout_seconds: float = Field(default=86_400.0, gt=0.0, le=604_800.0)
    profile_refresh_error_max_length: int = Field(default=4_000, ge=100, le=100_000)
    profile_refresh_retry_max_seconds: int = Field(default=300, ge=1, le=86_400)
    fast_copy_min_rows: int = Field(default=32, ge=1, le=100_000)
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
    # MCP manifest controls may read only from this operator-configured root;
    # clients cannot widen it by submitting an arbitrary server path.
    import_staging_root: Path | None = None
    upload_rate_limit_requests: int = Field(default=1_000, ge=1, le=1_000_000)
    upload_max_concurrency: int = Field(default=8, ge=1, le=128)
    # Browser requests only stage bytes. A separate durable worker owns MolOP
    # parsing so a page reload or API restart cannot discard accepted files.
    upload_worker_poll_interval_seconds: float = Field(default=1.0, gt=0.05, le=300.0)
    upload_worker_lease_seconds: int = Field(default=3_600, ge=60, le=86_400)
    # Deferred thermodynamic profiles are coalesced in a durable queue.  A
    # short debounce joins adjacent source writes, while the max delay keeps
    # a continuously busy import from starving profile refreshes.
    upload_worker_profile_refresh_debounce_seconds: float = Field(
        default=2.0,
        gt=0.0,
        le=300.0,
    )
    upload_worker_profile_refresh_max_delay_seconds: float = Field(
        default=60.0,
        gt=0.0,
        le=86_400.0,
    )
    # Keep profile source selection and replacement in short independent
    # transactions.  This is deliberately smaller than upload persistence
    # batches because the profile path may still inspect many source rows.
    upload_worker_profile_refresh_batch_size: int = Field(default=32, ge=1, le=2_048)
    upload_worker_profile_refresh_lease_seconds: int = Field(
        default=1_800,
        ge=60,
        le=86_400,
    )
    # Profile refresh is a separate maintenance process.  It is not started
    # by the upload-worker; the optional Compose service uses this interval.
    profile_refresh_worker_poll_interval_seconds: float = Field(
        default=2.0,
        gt=0.05,
        le=300.0,
    )
    profile_refresh_worker_audit_batch_size: int = Field(default=256, ge=1, le=2_048)
    upload_client_lease_seconds: int = Field(default=900, ge=60, le=86_400)
    # Compatibility recovery for calculation ingestions created before the
    # durable UploadBatch queue. Current API, MCP, and local-import paths all
    # create an active queue item and are claimed through claim_processing().
    # Only unowned pending ingestions older than this grace period are treated
    # as legacy orphans.
    upload_pending_recovery_seconds: int = Field(default=900, ge=60, le=86_400)
    # Zero derives a bounded claim window from the shared MolOP process pool;
    # it is a prefetch limit, not a parser or database concurrency setting.
    upload_worker_prefetch_files: int = Field(default=0, ge=0, le=100_000)
    # The worker keeps parsing ahead of the database but commits one
    # project/user microbatch at these limits.  They are deliberately
    # configurable because database latency and frame density vary by host.
    upload_worker_persistence_batch_files: int = Field(default=8, ge=1, le=64)
    upload_worker_persistence_frame_limit: int = Field(default=256, ge=1, le=10_000)
    upload_worker_concurrency: int = Field(default=2, ge=1, le=32)
    # Source spans, block hashes, segment boundaries, and frame roles are
    # required for lossless calculation ingestion.  Keep the setting for
    # backwards-compatible deployment configuration, but reject attempts to
    # disable it rather than silently losing source evidence.
    molop_capture_source_evidence: bool = True
    # Fast ingestion batches revision-local frame rows in one transaction.
    # Evidence capture no longer disables deferred topology reconstruction.
    molop_parallel_frame_persistence: bool = True
    ts_endpoint_openbabel_fallback: bool = False
    # Baseline end-to-end budget for a 10 MiB source; larger files receive an
    # additional size-derived allowance while smaller files retain the baseline.
    molop_file_parse_timeout_seconds: float = Field(default=60.0, gt=0.0, le=86400.0)
    # Extra allowance applied per reference-size unit above 10 MiB. At 1.5,
    # each additional 10 MiB adds 90 seconds to the configured baseline.
    molop_file_parse_timeout_size_multiplier: float = Field(default=1.5, ge=1.0, le=10.0)
    # -1 means all CPU cores visible to this service process.  Set a positive
    # value only when an operator deliberately wants to reserve CPU for other
    # workloads.
    molop_batch_n_jobs: int = Field(default=-1, ge=-1)

    @field_validator("import_staging_root")
    @classmethod
    def require_absolute_import_staging_root(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        if not value.is_absolute():
            raise ValueError("import_staging_root must be an absolute path")
        return value

    @field_validator("molop_batch_n_jobs")
    @classmethod
    def require_nonzero_molop_batch_jobs(cls, value: int) -> int:
        if value == 0:
            raise ValueError("molop_batch_n_jobs must be -1 or a positive integer")
        return value

    @field_validator("molop_capture_source_evidence")
    @classmethod
    def require_molop_source_evidence(cls, value: bool) -> bool:
        if not value:
            raise ValueError("molop_capture_source_evidence must remain enabled")
        return value

    @model_validator(mode="after")
    def validate_reconcile_limits(self) -> "IngestionWorkerSettings":
        if self.reconcile_default_statement_timeout_ms > self.reconcile_max_statement_timeout_ms:
            raise ValueError("reconcile statement timeout default exceeds its configured maximum")
        if (
            self.reconcile_default_reaction_timeout_seconds
            > self.reconcile_max_reaction_timeout_seconds
        ):
            raise ValueError("reconcile reaction timeout default exceeds its configured maximum")
        return self


class IdentityEmailSettings(_TricycleSettingsBase):
    """Identity email environment settings."""

    oidc_metadata_ttl_seconds: int = Field(default=300, ge=1, le=86_400)
    session_last_seen_interval_seconds: int = Field(default=300, ge=1, le=86_400)
    mcp_token_last_used_interval_seconds: int = Field(default=300, ge=1, le=86_400)
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
    cors_origins: list[str] = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:5174",
        "http://localhost:5174",
    ]

    @field_validator(
        "bootstrap_system_user_display_name", "bootstrap_development_user_display_name"
    )
    @classmethod
    def normalize_bootstrap_display_label(cls, value: str) -> str:
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

    @field_validator("oidc_ca_bundle", "smtp_ca_bundle")
    @classmethod
    def require_absolute_identity_ca_bundle(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or not Path(normalized).is_absolute():
            raise ValueError("CA bundle must be an absolute PEM path")
        return normalized

    @model_validator(mode="after")
    def validate_identity(self) -> "IdentityEmailSettings":
        if self.auth_mode == "oidc" and not all(
            (self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url)
        ):
            raise ValueError("OIDC mode requires issuer, audience, and JWKS URL")
        if len(self.session_secret) < 32:
            raise ValueError("session_secret must contain at least 32 characters")
        if self.email_delivery_mode == "smtp" and not self.smtp_host:
            raise ValueError("SMTP mode requires TRICYCLE_SMTP_HOST")
        if self.email_delivery_mode == "smtp" and not self.smtp_from_email:
            raise ValueError("SMTP mode requires TRICYCLE_SMTP_FROM_EMAIL")
        return self


class StorageClientSettings(_TricycleSettingsBase):
    """Storage environment settings."""

    rustfs_parse_download_chunk_size: int = Field(default=4 * 1024 * 1024, ge=1024)
    rustfs_endpoint_url: str = "http://127.0.0.1:19000"
    rustfs_verify_tls: bool = True
    rustfs_ca_bundle: str | None = None

    @field_validator("rustfs_ca_bundle")
    @classmethod
    def require_absolute_storage_ca_bundle(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or not Path(normalized).is_absolute():
            raise ValueError("CA bundle must be an absolute PEM path")
        return normalized


class ApplicationSettings(BaseModel):
    """Application configuration assembled from independent functional settings."""

    runtime: RuntimeSettings
    mcp_export: McpExportSettings
    ingestion: IngestionWorkerSettings
    identity: IdentityEmailSettings
    storage: StorageClientSettings

    @model_validator(mode="after")
    def validate_consistency(self) -> "ApplicationSettings":
        _validate_application_configuration(self)
        return self


def _validate_application_configuration(settings: ApplicationSettings) -> None:
    """Check requirements that span multiple application setting domains."""

    if settings.runtime.environment == "production" and settings.identity.auth_mode != "oidc":
        raise ValueError("production requires TRICYCLE_AUTH_MODE=oidc")
    if settings.runtime.environment == "production":
        if settings.runtime.debug:
            raise ValueError("production requires TRICYCLE_DEBUG=false")
        if not all(
            (
                settings.identity.oidc_client_id,
                settings.identity.oidc_client_secret,
                settings.identity.oidc_redirect_uri,
            )
        ):
            raise ValueError("production OIDC requires client ID, client secret, and redirect URI")
        if settings.identity.session_secret == "development-session-secret-change-me":
            raise ValueError("production requires a non-default session_secret")
        if not settings.identity.session_cookie_secure:
            raise ValueError("production requires a Secure session cookie")
        if settings.identity.auth_mode == "oidc":
            for name in (
                "oidc_issuer",
                "oidc_redirect_uri",
                "oidc_jwks_url",
                "oidc_frontend_url",
            ):
                value = getattr(settings.identity, name)
                if not isinstance(value, str) or not value.startswith("https://"):
                    raise ValueError(f"production requires HTTPS {name}")
            if not _is_exact_nonlocal_https_origin(settings.identity.oidc_frontend_url):
                raise ValueError(
                    "production oidc_frontend_url must be an exact non-localhost HTTPS origin"
                )
        database_url = make_url(settings.runtime.database_url)
        if database_url.query.get("sslmode") != "verify-full":
            raise ValueError("production database_url requires sslmode=verify-full")
        if not settings.identity.cors_origins:
            raise ValueError("production requires at least one exact HTTPS CORS origin")
        for origin in settings.identity.cors_origins:
            if not _is_exact_nonlocal_https_origin(origin):
                raise ValueError(
                    "production CORS origins must be exact non-localhost HTTPS origins"
                )
        rustfs_url = urlsplit(settings.storage.rustfs_endpoint_url)
        if rustfs_url.scheme != "https" or not rustfs_url.netloc:
            raise ValueError("production requires an HTTPS RustFS/S3 endpoint")
        if not settings.storage.rustfs_verify_tls:
            raise ValueError("production requires RustFS/S3 TLS verification")
        if settings.identity.email_delivery_mode != "smtp":
            raise ValueError("production requires SMTP invitation delivery")
        if not settings.identity.smtp_starttls:
            raise ValueError("production SMTP requires STARTTLS")
        if settings.identity.smtp_port == 465:
            raise ValueError("production SMTP port 465 is unsupported; use STARTTLS on port 587")
        if settings.identity.smtp_from_email is None:
            raise ValueError("production SMTP requires a valid verified sender address")
        if settings.runtime.rate_limit_backend != "redis":
            raise ValueError("production requires the Redis shared rate-limit backend")
        if not settings.runtime.rate_limit_redis_url:
            raise ValueError("production requires TRICYCLE_RATE_LIMIT_REDIS_URL")
        redis_url = urlsplit(settings.runtime.rate_limit_redis_url)
        if redis_url.scheme != "rediss" or not redis_url.hostname:
            raise ValueError("production rate limiting requires a rediss:// URL")


class RustFSSettings(BaseSettings):
    """Object-store settings, independently loadable by storage-only commands."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TRICYCLE_RUSTFS_",
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = Field(
        default="development",
        validation_alias="TRICYCLE_ENVIRONMENT",
    )
    endpoint_url: str = "http://127.0.0.1:19000"
    access_key: str = "example-local-access"
    secret_key: str = "example-local-secret"
    bucket: str = "example-reaction-raw-files"
    region: str = "us-east-1"
    verify_tls: bool = True
    ca_bundle: str | None = None
    connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    read_timeout_seconds: int = Field(default=30, ge=1, le=300)
    max_pool_connections: int = Field(default=32, ge=1, le=256)

    @field_validator("endpoint_url")
    @classmethod
    def validate_endpoint_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("endpoint_url must be an absolute HTTP(S) URL")
        return value.rstrip("/")

    @field_validator("ca_bundle")
    @classmethod
    def require_absolute_ca_bundle(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or not Path(normalized).is_absolute():
            raise ValueError("ca_bundle must be an absolute PEM path")
        return normalized

    @model_validator(mode="after")
    def validate_production_transport(self) -> "RustFSSettings":
        if self.environment == "production":
            if urlsplit(self.endpoint_url).scheme != "https":
                raise ValueError("production RustFS requires an HTTPS endpoint")
            if not self.verify_tls:
                raise ValueError("production RustFS requires TLS verification")
        return self


class StorageGarbageCollectionSettings(BaseSettings):
    """Storage-cleanup settings, independently loadable by the GC command."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TRICYCLE_STORAGE_GC_",
        extra="ignore",
    )

    root_prefix: str = "uploads"
    grace_period_seconds: int = Field(default=60 * 60, ge=60)
    initial_lookback_seconds: int = Field(default=24 * 60 * 60, ge=60)
    partition_clock_skew_seconds: int = Field(default=60 * 60, ge=0, le=24 * 60 * 60)

    @model_validator(mode="after")
    def validate_gc_window(self) -> "StorageGarbageCollectionSettings":
        if not self.root_prefix.strip("/"):
            raise ValueError("root_prefix must not be empty")
        if self.initial_lookback_seconds <= self.grace_period_seconds:
            raise ValueError("initial_lookback_seconds must exceed grace_period_seconds")
        return self


class RuntimeConfiguration(BaseModel):
    """One validated view of all backend environment settings."""

    application: ApplicationSettings
    rustfs: RustFSSettings
    storage_gc: StorageGarbageCollectionSettings

    @model_validator(mode="after")
    def validate_shared_storage(self) -> "RuntimeConfiguration":
        if self.application.runtime.environment != self.rustfs.environment:
            raise ValueError("application and RustFS deployment environments differ")
        if self.application.storage.rustfs_endpoint_url.rstrip("/") != self.rustfs.endpoint_url:
            raise ValueError("application and RustFS endpoint URLs differ")
        if self.application.storage.rustfs_verify_tls != self.rustfs.verify_tls:
            raise ValueError("application and RustFS TLS verification settings differ")
        if self.application.storage.rustfs_ca_bundle != self.rustfs.ca_bundle:
            raise ValueError("application and RustFS CA bundle settings differ")
        return self


class FrontendBuildSettings(BaseSettings):
    """Validate Vite values before a local or Compose frontend build."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="VITE_", extra="ignore")

    api_base_url: str = ""
    api_proxy_target: str = "http://127.0.0.1:8000"
    app_name: str = Field(default="Example Chemistry Database", min_length=1)
    brand_name: str = Field(default="Example Research Platform", min_length=1)
    app_tagline: str = Field(default="Computational data browser", min_length=1)
    mcp_server_name: str = Field(default="example-chemistry-database", min_length=1)
    csrf_cookie_name: str = Field(default="example_csrf", pattern=r"^[A-Za-z0-9_-]+$")
    csrf_header_name: str = Field(default="x-csrf-token", pattern=r"^[A-Za-z0-9-]+$")
    reaction_page_size_max: int = Field(default=12, ge=1, le=10_000)
    catalog_page_size_max: int = Field(default=500, ge=1, le=10_000)
    default_reaction_page_size: int = Field(default=12, ge=1, le=10_000)
    default_catalog_page_size: int = Field(default=50, ge=1, le=10_000)
    page_prefetch_radius: int = Field(default=3, ge=0, le=100)
    nexusx_graphql_url: str | None = None
    nexusx_core_url: str | None = None
    nexusx_paginated_graphql_url: str | None = None
    nexusx_mcp_url: str | None = None
    nexusx_rest_url: str | None = None
    nexusx_voyager_url: str | None = None

    @model_validator(mode="after")
    def validate_page_sizes(self) -> "FrontendBuildSettings":
        if self.default_reaction_page_size > self.reaction_page_size_max:
            raise ValueError("default reaction page size exceeds its maximum")
        if self.default_catalog_page_size > self.catalog_page_size_max:
            raise ValueError("default catalog page size exceeds its maximum")
        return self


class ComposeBuildSettings(BaseSettings):
    """Validate Compose interpolation values that have operational bounds."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    compose_project_name: str = Field(default="reaction-database-development", min_length=1)
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    rustfs_api_port: int = Field(default=19000, ge=1, le=65535)
    rustfs_console_port: int = Field(default=19001, ge=1, le=65535)
    keycloak_port: int = Field(default=8080, ge=1, le=65535)
    caddy_http_port: int = Field(default=80, ge=1, le=65535)
    caddy_https_port: int = Field(default=443, ge=1, le=65535)
    rustfs_compression_enabled: bool = True
    tricycle_bootstrap_mode: Literal["development", "production"] = "development"
    tricycle_compose_database_url: str = (
        "postgresql+psycopg://example_user:example-local-password@postgres:5432/example_reaction_db"
    )
    tricycle_compose_rustfs_endpoint_url: str = "http://rustfs:9000"
    tricycle_upload_worker_statement_timeout_ms: int = Field(default=120_000, ge=100, le=300_000)
    tricycle_profile_refresh_statement_timeout_ms: int = Field(default=120_000, ge=100, le=300_000)
    tricycle_units_dataset_worker_statement_timeout_ms: int = Field(
        default=120_000, ge=100, le=300_000
    )

    @field_validator("tricycle_compose_database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        try:
            parsed = make_url(value)
        except ArgumentError as exc:
            raise ValueError("Compose database URL is malformed") from exc
        if parsed.drivername != "postgresql+psycopg" or not parsed.host or not parsed.database:
            raise ValueError(
                "Compose database URL must use postgresql+psycopg with host and database"
            )
        return value

    @field_validator("tricycle_compose_rustfs_endpoint_url")
    @classmethod
    def validate_rustfs_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Compose RustFS endpoint must be an absolute HTTP(S) URL")
        return value


class ProjectConfiguration(BaseModel):
    """Validated project settings assembled from separate functional models."""

    runtime: RuntimeConfiguration
    frontend: FrontendBuildSettings
    compose: ComposeBuildSettings

    @model_validator(mode="after")
    def validate_browser_security_names(self) -> "ProjectConfiguration":
        application = self.runtime.application
        if application.identity.csrf_cookie_name != self.frontend.csrf_cookie_name:
            raise ValueError("frontend and backend CSRF cookie names differ")
        if application.identity.csrf_header_name != self.frontend.csrf_header_name:
            raise ValueError("frontend and backend CSRF header names differ")
        return self


def load_settings_model[T: BaseSettings](model: type[T], env_file: Path | str | None) -> T:
    """Load one settings domain with Pydantic's environment-file override."""

    return model(_env_file=env_file)


def load_application_settings(_env_file: Path | str | None = ".env") -> ApplicationSettings:
    """Load each application setting domain from the same environment source."""

    return ApplicationSettings(
        runtime=load_settings_model(RuntimeSettings, _env_file),
        mcp_export=load_settings_model(McpExportSettings, _env_file),
        ingestion=load_settings_model(IngestionWorkerSettings, _env_file),
        identity=load_settings_model(IdentityEmailSettings, _env_file),
        storage=load_settings_model(StorageClientSettings, _env_file),
    )


def load_configuration(_env_file: Path | str | None = ".env") -> RuntimeConfiguration:
    """Load every backend settings domain from the same environment source."""

    return RuntimeConfiguration(
        application=load_application_settings(_env_file=_env_file),
        rustfs=load_settings_model(RustFSSettings, _env_file),
        storage_gc=load_settings_model(StorageGarbageCollectionSettings, _env_file),
    )


def load_project_configuration(_env_file: Path | str | None = ".env") -> ProjectConfiguration:
    """Load backend, frontend, and Compose settings from one environment source."""

    return ProjectConfiguration(
        runtime=load_configuration(_env_file=_env_file),
        frontend=load_settings_model(FrontendBuildSettings, _env_file),
        compose=load_settings_model(ComposeBuildSettings, _env_file),
    )


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
def get_settings() -> ApplicationSettings:
    return load_configuration().application
