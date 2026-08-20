import ssl

from tricycle_reaction_db.application.services.email import smtp_tls_context
from tricycle_reaction_db.core.config import Settings
from tricycle_reaction_db.dev import deployment_smoke
from tricycle_reaction_db.storage.rustfs import RustFSSettings


def _production_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="production",
        auth_mode="oidc",
        oidc_issuer="https://identity.example.test",
        oidc_audience="reaction-database",
        oidc_jwks_url="https://identity.example.test/jwks.json",
        oidc_client_id="reaction-client",
        oidc_client_secret="production-secret",
        oidc_redirect_uri="https://app.example.test/api/auth/callback",
        oidc_frontend_url="https://app.example.test",
        session_secret="production-session-secret-0123456789",
        session_cookie_secure=True,
        database_url=(
            "postgresql+psycopg://tricycle:secret@db.example.test/tricycle?sslmode=verify-full"
        ),
        cors_origins=["https://app.example.test"],
        rustfs_endpoint_url="https://objects.example.test",
        rustfs_verify_tls=True,
        email_delivery_mode="smtp",
        smtp_host="smtp.example.test",
        smtp_from_email="noreply@example.test",
        smtp_starttls=True,
        rate_limit_backend="redis",
        rate_limit_redis_url="rediss://redis.example.test:6380/0",
    )


def test_smtp_tls_context_verifies_certificates_and_hostnames() -> None:
    context = smtp_tls_context(ca_bundle=None)

    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_deployment_smoke_runs_every_external_dependency_check(monkeypatch) -> None:
    called: list[str] = []

    def successful(name: str):
        def check(*_args):
            called.append(name)
            return {"checked": name}

        return check

    monkeypatch.setattr(deployment_smoke, "_check_public_health", successful("public_health"))
    monkeypatch.setattr(deployment_smoke, "_check_oidc", successful("oidc"))
    monkeypatch.setattr(deployment_smoke, "_check_database", successful("postgresql"))
    monkeypatch.setattr(deployment_smoke, "_check_object_storage", successful("rustfs_s3"))
    monkeypatch.setattr(deployment_smoke, "_check_redis", successful("redis"))
    monkeypatch.setattr(deployment_smoke, "_check_smtp", successful("smtp_starttls"))

    result = deployment_smoke.run_deployment_smoke(
        _production_settings(),
        RustFSSettings(
            _env_file=None,
            endpoint_url="https://objects.example.test",
            access_key="access",
            secret_key="secret",
            bucket="artifacts",
        ),
    )

    assert result.succeeded is True
    assert result.schema_version == "deployment-smoke-v1"
    assert called == [
        "public_health",
        "oidc",
        "postgresql",
        "rustfs_s3",
        "redis",
        "smtp_starttls",
    ]
    assert [check.name for check in result.checks] == called


def test_public_health_does_not_reuse_the_private_oidc_ca(monkeypatch) -> None:
    observed: dict[str, object] = {}

    class Response:
        headers = {"content-type": "application/json"}
        status_code = 200

        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self, **kwargs: object) -> None:
            observed.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, _url: str) -> Response:
            return Response()

    monkeypatch.setattr(deployment_smoke.httpx, "Client", Client)

    settings = _production_settings().model_copy(
        update={"oidc_ca_bundle": "/etc/reaction-database/identity-ca.pem"}
    )
    deployment_smoke._check_public_health(settings)

    assert "verify" not in observed


def test_oidc_smoke_uses_the_configured_private_ca(monkeypatch) -> None:
    tls_context = object()
    observed: dict[str, object] = {}

    class Response:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self.payload

    class Client:
        def __init__(self, **kwargs: object) -> None:
            observed.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, url: str) -> Response:
            if url.endswith("/.well-known/openid-configuration"):
                return Response(
                    {
                        "issuer": "https://identity.example.test",
                        "authorization_endpoint": "https://identity.example.test/authorize",
                        "token_endpoint": "https://identity.example.test/token",
                        "jwks_uri": "https://identity.example.test/jwks.json",
                        "code_challenge_methods_supported": ["S256"],
                    }
                )
            return Response({"keys": [{"kid": "signing-key"}]})

    monkeypatch.setattr(deployment_smoke.httpx, "Client", Client)
    monkeypatch.setattr(
        deployment_smoke,
        "verified_tls_context",
        lambda **_kwargs: tls_context,
    )

    settings = _production_settings().model_copy(
        update={"oidc_ca_bundle": "/etc/reaction-database/identity-ca.pem"}
    )
    details = deployment_smoke._check_oidc(settings)

    assert observed["verify"] is tls_context
    assert details["pkce_s256"] is True


def test_deployment_smoke_records_a_failed_check_without_skipping_the_rest(monkeypatch) -> None:
    monkeypatch.setattr(
        deployment_smoke,
        "_check_public_health",
        lambda _settings: (_ for _ in ()).throw(OSError("edge unavailable")),
    )
    for name in (
        "_check_oidc",
        "_check_database",
        "_check_object_storage",
        "_check_redis",
        "_check_smtp",
    ):
        monkeypatch.setattr(deployment_smoke, name, lambda *_args: {"checked": True})

    result = deployment_smoke.run_deployment_smoke(
        _production_settings(),
        RustFSSettings(_env_file=None),
    )

    assert result.succeeded is False
    assert result.checks[0].succeeded is False
    assert result.checks[0].error == "OSError: edge unavailable"
    assert all(check.succeeded for check in result.checks[1:])
