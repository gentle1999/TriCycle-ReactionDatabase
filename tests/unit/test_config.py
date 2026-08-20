import pytest
from pydantic import ValidationError

from tricycle_reaction_db.core.config import Settings
from tricycle_reaction_db.dev.bootstrap import BootstrapSpec


def _production_settings(**updates: object) -> dict[str, object]:
    return {
        "_env_file": None,
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


def test_settings_accept_psycopg_database_url() -> None:
    settings = Settings(_env_file=None)

    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.api_port == 8000


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

    settings = Settings(_env_file=None)

    assert settings.app_name == "Example Chemistry Database"
    assert settings.brand_name == "Example Lab"
    assert settings.mcp_server_name == "Example Chemistry MCP"
    assert settings.nexusx_app_name == "example-chemistry"
    assert settings.nexusx_database_cluster_name == "chemistry-postgresql"
    assert settings.nexusx_database_cluster_color == "#F3E5F5"
    assert settings.session_cookie_name == "example_session"
    assert settings.csrf_cookie_name == "example_csrf"
    assert settings.csrf_header_name == "x-example-csrf"


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
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize("color", ["blue", "#12345", "#1234567", "#GGGGGG"])
def test_nexusx_database_cluster_color_must_be_six_digit_hex(color: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, nexusx_database_cluster_color=color)


def test_settings_reject_other_database_drivers() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="sqlite:///local.db")


def test_production_requires_oidc_authentication() -> None:
    with pytest.raises(ValidationError, match="production requires"):
        Settings(_env_file=None, environment="production", auth_mode="development")


def test_oidc_authentication_requires_complete_provider_configuration() -> None:
    with pytest.raises(ValidationError, match="OIDC mode requires"):
        Settings(_env_file=None, auth_mode="oidc")

    with pytest.raises(ValidationError, match="production OIDC requires"):
        Settings(
            _env_file=None,
            environment="production",
            auth_mode="oidc",
            oidc_issuer="https://identity.example.test",
            oidc_audience="reaction-database",
            oidc_jwks_url="https://identity.example.test/.well-known/jwks.json",
        )

    settings = Settings(**_production_settings())
    assert settings.auth_mode == "oidc"


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"debug": True}, "TRICYCLE_DEBUG=false"),
        ({"session_secret": "development-session-secret-change-me"}, "non-default"),
        ({"session_cookie_secure": False}, "Secure session cookie"),
        ({"molop_batch_n_jobs": -1}, "positive limit"),
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
        Settings(**_production_settings(**updates))


def test_development_bootstrap_uses_configured_deployment_names() -> None:
    settings = Settings(
        _env_file=None,
        bootstrap_organization_slug="example-lab",
        bootstrap_organization_name="Example Lab",
        bootstrap_project_slug="reaction-catalog",
        bootstrap_project_name="Reaction Catalog",
    )

    spec = BootstrapSpec.from_settings(settings, "development")

    assert spec.organization_slug == "example-lab"
    assert spec.organization_name == "Example Lab"
    assert spec.project_slug == "reaction-catalog"
    assert spec.project_name == "Reaction Catalog"


def test_smtp_ca_bundle_must_be_an_absolute_path() -> None:
    with pytest.raises(ValidationError, match="absolute PEM path"):
        Settings(_env_file=None, smtp_ca_bundle="relative/relay-ca.pem")

    settings = Settings(_env_file=None, smtp_ca_bundle="/etc/reaction-database/relay-ca.pem")

    assert settings.smtp_ca_bundle == "/etc/reaction-database/relay-ca.pem"

    oidc_settings = Settings(
        _env_file=None,
        oidc_ca_bundle="/etc/reaction-database/identity-ca.pem",
    )

    assert oidc_settings.oidc_ca_bundle == "/etc/reaction-database/identity-ca.pem"


def test_production_bootstrap_requires_explicit_identity_and_container_names() -> None:
    settings = Settings(**_production_settings())

    with pytest.raises(ValueError, match="TRICYCLE_BOOTSTRAP_OIDC_ISSUER"):
        BootstrapSpec.from_settings(settings, "production")

    configured = Settings(
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
    settings = Settings(**_production_settings())

    with pytest.raises(ValueError, match="forbidden in production"):
        BootstrapSpec.from_settings(settings, "development")
