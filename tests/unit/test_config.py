import pytest
from pydantic import ValidationError

from tests.config_helpers import application_settings
from tricycle_reaction_db.core.config import (
    ApplicationSettings,
    ComposeBuildSettings,
    FrontendBuildSettings,
    IdentityEmailSettings,
    IngestionWorkerSettings,
    McpExportSettings,
    ProjectConfiguration,
    RuntimeConfiguration,
    RuntimeSettings,
    RustFSSettings,
    StorageClientSettings,
    StorageGarbageCollectionSettings,
    load_application_settings,
    load_configuration,
    load_project_configuration,
)
from tricycle_reaction_db.dev.bootstrap import BootstrapSpec


def _production_settings(**updates: object) -> dict[str, object]:
    values = {
        "environment": "production",
        "auth_mode": "oidc",
        "oidc_issuer": "https://identity.example.test",
        "oidc_audience": "reaction-database",
        "oidc_jwks_url": "https://identity.example.test/.well-known/jwks.json",
        "oidc_client_id": "reaction-client",
        "oidc_client_secret": "production-secret",
        "oidc_redirect_uri": "https://app.example.test/api/auth/callback",
        "oidc_frontend_url": "https://app.example.test",
        "session_secret": "production-session-secret-0123456789",
        "session_cookie_secure": True,
        "database_url": (
            "postgresql+psycopg://tricycle:secret@db.example.test/tricycle?sslmode=verify-full"
        ),
        "cors_origins": ["https://app.example.test"],
        "rustfs_endpoint_url": "https://objects.example.test",
        "rustfs_verify_tls": True,
        "email_delivery_mode": "smtp",
        "smtp_host": "smtp.example.test",
        "smtp_from_email": "noreply@example.test",
        "smtp_starttls": True,
        "rate_limit_backend": "redis",
        "rate_limit_redis_url": "rediss://redis.example.test:6380/0",
        **updates,
    }
    domains = {
        "runtime": RuntimeSettings,
        "mcp_export": McpExportSettings,
        "ingestion": IngestionWorkerSettings,
        "identity": IdentityEmailSettings,
        "storage": StorageClientSettings,
    }
    grouped: dict[str, object] = {"_env_file": None}
    for domain, model in domains.items():
        grouped[domain] = {
            name: value for name, value in values.items() if name in model.model_fields
        }
    return grouped


def test_settings_accept_psycopg_database_url() -> None:
    settings = application_settings(_env_file=None)

    assert settings.runtime.database_url.startswith("postgresql+psycopg://")
    assert settings.runtime.database_pool_size == 5
    assert settings.runtime.database_max_overflow == 10
    assert settings.runtime.database_pool_timeout_seconds == 30.0
    assert settings.runtime.api_port == 8000
    assert settings.mcp_export.mcp_ts_geometry_export_max_bytes == 512 * 1024
    assert settings.ingestion.molop_capture_source_evidence is True
    assert settings.ingestion.molop_parallel_frame_persistence is True
    assert settings.ingestion.molop_file_parse_timeout_seconds == 60.0
    assert settings.ingestion.molop_file_parse_timeout_size_multiplier == 1.5
    assert settings.runtime.molecular_graph_match_timeout_seconds == 5.0
    assert settings.runtime.molecular_graph_match_isolation_atom_count == 48
    assert settings.runtime.molecular_graph_match_max_results == 1_000


def test_application_settings_domains_load_independently() -> None:
    domains = (
        RuntimeSettings,
        McpExportSettings,
        IngestionWorkerSettings,
        IdentityEmailSettings,
        StorageClientSettings,
    )
    field_sets = [set(domain.model_fields) for domain in domains]
    assert sum(map(len, field_sets)) == len(set.union(*field_sets)) == 151
    application = load_application_settings(_env_file=None)
    assert isinstance(application, ApplicationSettings)
    assert not hasattr(application, "database_url")

    with pytest.raises(ValidationError, match="database_url"):
        RuntimeSettings(_env_file=None, database_url="sqlite:///local.db")
    with pytest.raises(ValidationError, match="mcp_export_max_records"):
        McpExportSettings(_env_file=None, mcp_export_max_records=0)
    with pytest.raises(ValidationError, match="reconcile statement timeout"):
        IngestionWorkerSettings(
            _env_file=None,
            reconcile_default_statement_timeout_ms=300_000,
            reconcile_max_statement_timeout_ms=200_000,
        )
    with pytest.raises(ValidationError, match="SMTP mode requires"):
        IdentityEmailSettings(_env_file=None, email_delivery_mode="smtp")
    with pytest.raises(ValidationError, match="CA bundle"):
        StorageClientSettings(_env_file=None, rustfs_ca_bundle="relative/ca.pem")


def test_unified_configuration_loads_and_validates_all_backend_domains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = load_configuration(_env_file=None)
    assert configuration.application.runtime.environment == configuration.rustfs.environment
    assert (
        configuration.application.storage.rustfs_endpoint_url == configuration.rustfs.endpoint_url
    )
    assert (
        configuration.storage_gc.initial_lookback_seconds
        > configuration.storage_gc.grace_period_seconds
    )

    monkeypatch.setenv("TRICYCLE_STORAGE_GC_INITIAL_LOOKBACK_SECONDS", "3600")
    with pytest.raises(ValidationError, match="initial_lookback_seconds"):
        load_configuration(_env_file=None)


def test_unified_configuration_rejects_inconsistent_shared_storage() -> None:
    with pytest.raises(ValidationError, match="endpoint URLs differ"):
        RuntimeConfiguration(
            application=application_settings(_env_file=None),
            rustfs=RustFSSettings(_env_file=None, endpoint_url="https://objects.example.test"),
            storage_gc=StorageGarbageCollectionSettings(_env_file=None),
        )


def test_project_configuration_groups_separate_settings_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = load_project_configuration(_env_file=None)
    assert isinstance(configuration, ProjectConfiguration)
    assert isinstance(configuration.frontend, FrontendBuildSettings)
    assert isinstance(configuration.compose, ComposeBuildSettings)
    assert (
        configuration.frontend.csrf_cookie_name
        == configuration.runtime.application.identity.csrf_cookie_name
    )

    monkeypatch.setenv("VITE_CSRF_COOKIE_NAME", "browser_only")
    with pytest.raises(ValidationError, match="CSRF cookie names differ"):
        load_project_configuration(_env_file=None)


def test_build_settings_reject_invalid_pagination_and_compose_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VITE_REACTION_PAGE_SIZE_MAX", "0")
    with pytest.raises(ValidationError, match="reaction_page_size_max"):
        FrontendBuildSettings(_env_file=None)

    monkeypatch.delenv("VITE_REACTION_PAGE_SIZE_MAX")
    monkeypatch.setenv("VITE_DEFAULT_REACTION_PAGE_SIZE", "13")
    with pytest.raises(ValidationError, match="default reaction page size exceeds"):
        FrontendBuildSettings(_env_file=None)

    monkeypatch.setenv("CADDY_HTTPS_PORT", "70000")
    with pytest.raises(ValidationError, match="caddy_https_port"):
        ComposeBuildSettings(_env_file=None)

    monkeypatch.delenv("CADDY_HTTPS_PORT")
    monkeypatch.setenv("TRICYCLE_COMPOSE_DATABASE_URL", "invalid")
    with pytest.raises(ValidationError, match="Compose database URL is malformed"):
        ComposeBuildSettings(_env_file=None)


def test_mcp_ts_geometry_export_budget_uses_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRICYCLE_MCP_TS_GEOMETRY_EXPORT_MAX_BYTES", "2097152")
    assert (
        application_settings(_env_file=None).mcp_export.mcp_ts_geometry_export_max_bytes
        == 2 * 1024 * 1024
    )

    for invalid_value in ("1023", str(64 * 1024 * 1024 + 1)):
        monkeypatch.setenv("TRICYCLE_MCP_TS_GEOMETRY_EXPORT_MAX_BYTES", invalid_value)
        with pytest.raises(ValidationError):
            application_settings(_env_file=None)


def test_operational_limits_can_be_overridden_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overrides = {
        "mcp_ts_geometry_export_max_records": 150,
        "mcp_export_max_records": 151,
        "mcp_export_max_bytes": 2 * 1024 * 1024,
        "mcp_artifact_preview_max_bytes": 512 * 1024,
        "mcp_batch_max_files": 120,
        "mcp_array_preview_max_elements": 8192,
        "mcp_batch_download_concurrency": 8,
        "mcp_csv_max_offset": 2_000_000,
        "mapped_geometry_export_page_size": 32,
        "units_ts_export_page_size": 64,
        "units_ts_download_ttl_seconds": 86_400,
        "units_ts_job_lease_seconds": 900,
        "units_ts_max_reactive_atoms": 24,
        "units_ts_max_rc_bonds": 30,
        "units_ts_max_rc_angles": 35,
        "frame_conversion_chunk_size": 16,
        "inference_persist_batch_size": 8,
        "transaction_retry_limit": 5,
        "molop_parse_timeout_reference_bytes": 5 * 1024 * 1024,
        "rustfs_parse_download_chunk_size": 2 * 1024 * 1024,
        "geometry_match_input_batch_size": 64,
        "analytics_histogram_bin_count": 20,
        "analytics_max_scatter_points": 2_000,
        "units_ts_legacy_export_purge_interval_seconds": 120,
        "import_hash_chunk_bytes": 2 * 1024 * 1024,
        "import_max_fingerprint_workers": 16,
        "import_commit_batch_files": 8,
        "import_pipeline_window_files": 32,
        "import_stream_queue_size": 32,
        "import_max_transient_retries": 4,
        "import_transient_retry_backoff_seconds": 0.5,
        "reconcile_default_batch_size": 50,
        "reconcile_default_statement_timeout_ms": 200_000,
        "reconcile_max_statement_timeout_ms": 1_000_000,
        "reconcile_default_reaction_timeout_seconds": 200,
        "reconcile_max_reaction_timeout_seconds": 1_000,
        "profile_refresh_error_max_length": 2_000,
        "profile_refresh_retry_max_seconds": 120,
        "oidc_metadata_ttl_seconds": 600,
        "session_last_seen_interval_seconds": 600,
        "mcp_token_last_used_interval_seconds": 600,
        "fast_copy_min_rows": 64,
    }
    for name, value in overrides.items():
        monkeypatch.setenv(f"TRICYCLE_{name.upper()}", str(value))

    settings = application_settings(_env_file=None)
    for name, value in overrides.items():
        domain = next(
            domain
            for domain in ("runtime", "mcp_export", "ingestion", "identity", "storage")
            if name in type(getattr(settings, domain)).model_fields
        )
        assert getattr(getattr(settings, domain), name) == value


def test_database_pool_settings_can_match_worker_concurrency() -> None:
    settings = application_settings(
        _env_file=None,
        runtime={
            "database_pool_size": 16,
            "database_max_overflow": 16,
            "database_pool_timeout_seconds": 60.0,
        },
    )

    assert settings.runtime.database_pool_size == 16
    assert settings.runtime.database_max_overflow == 16
    assert settings.runtime.database_pool_timeout_seconds == 60.0


def test_molop_source_evidence_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRICYCLE_MOLOP_CAPTURE_SOURCE_EVIDENCE", "true")

    assert application_settings(_env_file=None).ingestion.molop_capture_source_evidence is True

    monkeypatch.setenv("TRICYCLE_MOLOP_CAPTURE_SOURCE_EVIDENCE", "false")
    with pytest.raises(ValidationError, match="must remain enabled"):
        application_settings(_env_file=None)


def test_molop_parallel_frame_persistence_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRICYCLE_MOLOP_PARALLEL_FRAME_PERSISTENCE", "false")

    assert application_settings(_env_file=None).ingestion.molop_parallel_frame_persistence is False


def test_deployment_names_are_overridable_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRICYCLE_APP_NAME", "Example Chemistry Database")
    monkeypatch.setenv("TRICYCLE_BRAND_NAME", "Example Lab")
    monkeypatch.setenv("TRICYCLE_MCP_SERVER_NAME", "Example Chemistry MCP")
    monkeypatch.setenv("TRICYCLE_NEXUSX_APP_NAME", "example-chemistry")
    monkeypatch.setenv("TRICYCLE_NEXUSX_DATABASE_CLUSTER_NAME", "chemistry-postgresql")
    monkeypatch.setenv("TRICYCLE_NEXUSX_DATABASE_CLUSTER_COLOR", "#F3E5F5")
    monkeypatch.setenv("TRICYCLE_SESSION_COOKIE_NAME", "example_session")
    monkeypatch.setenv("TRICYCLE_CSRF_COOKIE_NAME", "example_csrf")
    monkeypatch.setenv("TRICYCLE_CSRF_HEADER_NAME", "x-example-csrf")

    settings = application_settings(_env_file=None)

    assert settings.runtime.app_name == "Example Chemistry Database"
    assert settings.runtime.brand_name == "Example Lab"
    assert settings.runtime.mcp_server_name == "Example Chemistry MCP"
    assert settings.runtime.nexusx_app_name == "example-chemistry"
    assert settings.runtime.nexusx_database_cluster_name == "chemistry-postgresql"
    assert settings.runtime.nexusx_database_cluster_color == "#F3E5F5"
    assert settings.identity.session_cookie_name == "example_session"
    assert settings.identity.csrf_cookie_name == "example_csrf"
    assert settings.identity.csrf_header_name == "x-example-csrf"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_cookie_name", "contains space"),
        ("csrf_cookie_name", ""),
        ("csrf_header_name", "x-invalid:header"),
    ],
)
def test_deployment_cookie_and_header_names_must_be_http_tokens(
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError):
        application_settings(_env_file=None, identity={field: value})


@pytest.mark.parametrize("color", ["blue", "#12345", "#1234567", "#GGGGGG"])
def test_nexusx_database_cluster_color_must_be_six_digit_hex(color: str) -> None:
    with pytest.raises(ValidationError):
        application_settings(_env_file=None, runtime={"nexusx_database_cluster_color": color})


def test_settings_reject_other_database_drivers() -> None:
    with pytest.raises(ValidationError):
        application_settings(_env_file=None, runtime={"database_url": "sqlite:///local.db"})


def test_production_requires_oidc_authentication() -> None:
    with pytest.raises(ValidationError, match="production requires"):
        application_settings(
            _env_file=None,
            runtime={"environment": "production"},
            identity={"auth_mode": "development"},
        )


def test_oidc_authentication_requires_complete_provider_configuration() -> None:
    with pytest.raises(ValidationError, match="OIDC mode requires"):
        application_settings(_env_file=None, identity={"auth_mode": "oidc"})

    with pytest.raises(ValidationError, match="production OIDC requires"):
        application_settings(
            _env_file=None,
            runtime={"environment": "production"},
            identity={
                "auth_mode": "oidc",
                "oidc_issuer": "https://identity.example.test",
                "oidc_audience": "reaction-database",
                "oidc_jwks_url": "https://identity.example.test/.well-known/jwks.json",
            },
        )

    settings = application_settings(**_production_settings())
    assert settings.identity.auth_mode == "oidc"


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"debug": True}, "TRICYCLE_DEBUG=false"),
        ({"session_secret": "development-session-secret-change-me"}, "non-default"),
        ({"session_cookie_secure": False}, "Secure session cookie"),
        ({"molop_batch_n_jobs": 0}, "positive integer"),
        ({"oidc_issuer": "http://identity.example.test"}, "HTTPS oidc_issuer"),
        (
            {"oidc_redirect_uri": "http://app.example.test/api/auth/callback"},
            "HTTPS oidc_redirect_uri",
        ),
        (
            {"oidc_jwks_url": "http://identity.example.test/.well-known/jwks.json"},
            "HTTPS oidc_jwks_url",
        ),
        ({"oidc_frontend_url": "http://app.example.test"}, "HTTPS oidc_frontend_url"),
        (
            {"oidc_frontend_url": "https://localhost:5173"},
            "exact non-localhost HTTPS origin",
        ),
        (
            {"database_url": "postgresql+psycopg://tricycle:secret@db.example.test/tricycle"},
            "sslmode=verify-full",
        ),
        ({"cors_origins": ["http://localhost:5173"]}, "exact non-localhost HTTPS"),
        ({"rustfs_endpoint_url": "http://objects.example.test"}, "HTTPS RustFS/S3"),
        ({"rustfs_verify_tls": False}, "TLS verification"),
        ({"email_delivery_mode": "link"}, "SMTP invitation delivery"),
        ({"smtp_starttls": False}, "SMTP requires STARTTLS"),
        ({"smtp_port": 465}, "port 465 is unsupported"),
        ({"smtp_from_email": "noreply@localhost"}, "valid DNS domain"),
        ({"rate_limit_backend": "memory"}, "Redis shared rate-limit"),
        ({"rate_limit_redis_url": None}, "TRICYCLE_RATE_LIMIT_REDIS_URL"),
        (
            {"rate_limit_redis_url": "redis://redis.example.test:6379/0"},
            "rediss:// URL",
        ),
    ],
)
def test_production_rejects_each_unsafe_authentication_or_worker_setting(
    updates: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        application_settings(**_production_settings(**updates))


def test_development_bootstrap_uses_configured_deployment_names() -> None:
    settings = application_settings(
        _env_file=None,
        identity={
            "bootstrap_organization_slug": "example-lab",
            "bootstrap_organization_name": "Example Lab",
            "bootstrap_project_slug": "reaction-catalog",
            "bootstrap_project_name": "Reaction Catalog",
        },
    )

    spec = BootstrapSpec.from_settings(settings, "development")

    assert spec.organization_slug == "example-lab"
    assert spec.organization_name == "Example Lab"
    assert spec.project_slug == "reaction-catalog"
    assert spec.project_name == "Reaction Catalog"


def test_smtp_ca_bundle_must_be_an_absolute_path() -> None:
    with pytest.raises(ValidationError, match="absolute PEM path"):
        application_settings(_env_file=None, identity={"smtp_ca_bundle": "relative/relay-ca.pem"})

    settings = application_settings(
        _env_file=None, identity={"smtp_ca_bundle": "/etc/reaction-database/relay-ca.pem"}
    )

    assert settings.identity.smtp_ca_bundle == "/etc/reaction-database/relay-ca.pem"

    oidc_settings = application_settings(
        _env_file=None, identity={"oidc_ca_bundle": "/etc/reaction-database/identity-ca.pem"}
    )

    assert oidc_settings.identity.oidc_ca_bundle == "/etc/reaction-database/identity-ca.pem"


def test_production_bootstrap_requires_explicit_identity_and_container_names() -> None:
    settings = application_settings(**_production_settings())

    with pytest.raises(ValueError, match="TRICYCLE_BOOTSTRAP_OIDC_ISSUER"):
        BootstrapSpec.from_settings(settings, "production")

    configured = application_settings(
        **_production_settings(
            bootstrap_oidc_issuer="https://identity.example.test",
            bootstrap_oidc_subject="administrator-subject",
            bootstrap_admin_display_name="Database Administrator",
            bootstrap_admin_email="administrator@example.test",
            bootstrap_organization_slug="example-lab",
            bootstrap_organization_name="Example Lab",
            bootstrap_project_slug="reaction-catalog",
            bootstrap_project_name="Reaction Catalog",
        )
    )
    spec = BootstrapSpec.from_settings(configured, "production")

    assert spec.subject == "administrator-subject"
    assert spec.organization_slug == "example-lab"
    assert spec.project_slug == "reaction-catalog"


def test_development_bootstrap_is_forbidden_in_production() -> None:
    settings = application_settings(**_production_settings())

    with pytest.raises(ValueError, match="forbidden in production"):
        BootstrapSpec.from_settings(settings, "development")
