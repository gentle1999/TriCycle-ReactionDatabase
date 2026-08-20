"""Validate production dependencies from an API node and emit one JSON record."""

from __future__ import annotations

import json
import smtplib
import socket
import ssl
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx
from pydantic import ValidationError
from redis import Redis
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tricycle_reaction_db.application.services.email import smtp_tls_context
from tricycle_reaction_db.core.config import Settings
from tricycle_reaction_db.core.tls import verified_tls_context
from tricycle_reaction_db.storage.rustfs import RustFSObjectStore, RustFSSettings

DEPLOYMENT_SMOKE_SCHEMA_VERSION = "deployment-smoke-v1"


@dataclass(frozen=True, slots=True)
class DeploymentCheck:
    name: str
    succeeded: bool
    details: dict[str, object]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DeploymentSmokeResult:
    schema_version: str
    checked_at: str
    node: str
    app_name: str
    checks: tuple[DeploymentCheck, ...]
    succeeded: bool


def _run_check(name: str, operation: Callable[[], dict[str, object]]) -> DeploymentCheck:
    try:
        return DeploymentCheck(name=name, succeeded=True, details=operation())
    except Exception as error:
        return DeploymentCheck(
            name=name,
            succeeded=False,
            details={},
            error=f"{type(error).__name__}: {error}",
        )


def _check_public_health(settings: Settings) -> dict[str, object]:
    base_url = f"{settings.oidc_frontend_url.rstrip('/')}/"
    statuses: dict[str, int] = {}
    # The public edge certificate has a different trust boundary from the
    # private CA that may secure the OIDC issuer.
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        for path in ("health/live", "health/ready"):
            response = client.get(urljoin(base_url, path))
            response.raise_for_status()
            if response.headers.get("content-type", "").partition(";")[0] != "application/json":
                raise RuntimeError(f"/{path} did not return application/json")
            statuses[f"/{path}"] = response.status_code
    return {"origin": base_url.rstrip("/"), "statuses": statuses}


def _check_oidc(settings: Settings) -> dict[str, object]:
    issuer = settings.oidc_issuer
    jwks_url = settings.oidc_jwks_url
    if issuer is None or jwks_url is None:
        raise RuntimeError("OIDC issuer and JWKS URL are required")
    discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    with httpx.Client(
        timeout=10,
        follow_redirects=False,
        verify=verified_tls_context(ca_bundle=settings.oidc_ca_bundle),
    ) as client:
        discovery_response = client.get(discovery_url)
        discovery_response.raise_for_status()
        discovery = discovery_response.json()
        if not isinstance(discovery, dict):
            raise RuntimeError("OIDC discovery response is not an object")
        if discovery.get("issuer") != issuer:
            raise RuntimeError("OIDC discovery issuer does not exactly match configuration")
        methods = discovery.get("code_challenge_methods_supported")
        if not isinstance(methods, list) or "S256" not in methods:
            raise RuntimeError("OIDC provider does not advertise PKCE S256")
        endpoints: dict[str, str] = {}
        for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            value = discovery.get(field)
            if not isinstance(value, str) or urlsplit(value).scheme != "https":
                raise RuntimeError(f"OIDC {field} must be an HTTPS URL")
            endpoints[field] = value
        if endpoints["jwks_uri"] != jwks_url:
            raise RuntimeError("OIDC discovery jwks_uri does not match TRICYCLE_OIDC_JWKS_URL")
        jwks_response = client.get(jwks_url)
        jwks_response.raise_for_status()
        jwks = jwks_response.json()
        if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list) or not jwks["keys"]:
            raise RuntimeError("OIDC JWKS contains no signing keys")
    return {
        "issuer": issuer,
        "discovery_url": discovery_url,
        "pkce_s256": True,
        "signing_key_count": len(jwks["keys"]),
    }


def _check_database(settings: Settings) -> dict[str, object]:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        """
                    SELECT
                        COALESCE(
                            (SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()),
                            false
                        ) AS ssl_in_use,
                        (
                            NOT pg_is_in_recovery()
                            AND current_setting('transaction_read_only') = 'off'
                        ) AS writable_primary,
                        COALESCE(
                            (SELECT extversion FROM pg_extension WHERE extname = 'rdkit'),
                            ''
                        ) AS rdkit_extension_version,
                        current_database() AS database
                    """
                    )
                )
                .mappings()
                .one()
            )
    finally:
        engine.dispose()
    if not row["ssl_in_use"]:
        raise RuntimeError("PostgreSQL connection is not using TLS")
    if not row["writable_primary"]:
        raise RuntimeError("PostgreSQL endpoint resolved to a read-only standby")
    if not row["rdkit_extension_version"]:
        raise RuntimeError("PostgreSQL RDKit extension is unavailable")
    database_url = make_url(settings.database_url)
    return {
        "host": database_url.host or "",
        "database": str(row["database"]),
        "ssl_in_use": True,
        "writable_primary": True,
        "rdkit_extension_version": str(row["rdkit_extension_version"]),
    }


def _check_object_storage(storage: RustFSSettings) -> dict[str, object]:
    with RustFSObjectStore(storage) as store:
        versioning = store.bucket_versioning_status()
    if versioning != "Enabled":
        raise RuntimeError("production RustFS/S3 bucket versioning is not enabled")
    return {
        "endpoint": storage.endpoint_url,
        "bucket": storage.bucket,
        "versioning": versioning,
    }


_REDIS_WRITE_PROBE = """
local created = redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2], 'NX')
local ttl = redis.call('TTL', KEYS[1])
return {created, ttl}
"""


def _check_redis(settings: Settings) -> dict[str, object]:
    redis_url = settings.rate_limit_redis_url
    if redis_url is None:
        raise RuntimeError("Redis URL is required")
    client: Redis = Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    key = f"{settings.rate_limit_key_prefix}:deployment-smoke:{uuid4()}"
    try:
        response: Any = client.eval(_REDIS_WRITE_PROBE, 1, key, "1", "60")
        if not isinstance(response, list) or response[0] != "OK" or int(response[1]) <= 0:
            raise RuntimeError("Redis atomic write/expiry probe returned an invalid result")
    finally:
        client.delete(key)
        client.close()
    return {
        "host": urlsplit(redis_url).hostname or "",
        "tls": urlsplit(redis_url).scheme == "rediss",
        "atomic_write": True,
        "probe_key_removed": True,
    }


def _check_smtp(settings: Settings) -> dict[str, object]:
    host = settings.smtp_host
    sender = settings.smtp_from_email
    if host is None or sender is None:
        raise RuntimeError("SMTP host and sender are required")
    context = smtp_tls_context(ca_bundle=settings.smtp_ca_bundle)
    with smtplib.SMTP(host, settings.smtp_port, timeout=settings.smtp_timeout_seconds) as client:
        client.ehlo_or_helo_if_needed()
        if not client.has_extn("starttls"):
            raise RuntimeError("SMTP relay does not advertise STARTTLS")
        client.starttls(context=context)
        client.ehlo()
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password or "")
        tls_socket = client.sock
        cipher_info = tls_socket.cipher() if isinstance(tls_socket, ssl.SSLSocket) else None
        cipher = cipher_info[0] if cipher_info is not None else "unknown"
    return {
        "host": host,
        "port": settings.smtp_port,
        "starttls": True,
        "authenticated": bool(settings.smtp_username),
        "sender_domain": sender.rsplit("@", 1)[1],
        "cipher": cipher,
    }


def run_deployment_smoke(
    settings: Settings | None = None,
    storage: RustFSSettings | None = None,
) -> DeploymentSmokeResult:
    resolved_settings = settings or Settings()
    resolved_storage = storage or RustFSSettings()
    if resolved_settings.environment != "production":
        raise RuntimeError("deployment smoke requires TRICYCLE_ENVIRONMENT=production")
    checks = (
        _run_check("public_health", lambda: _check_public_health(resolved_settings)),
        _run_check("oidc", lambda: _check_oidc(resolved_settings)),
        _run_check("postgresql", lambda: _check_database(resolved_settings)),
        _run_check("rustfs_s3", lambda: _check_object_storage(resolved_storage)),
        _run_check("redis", lambda: _check_redis(resolved_settings)),
        _run_check("smtp_starttls", lambda: _check_smtp(resolved_settings)),
    )
    return DeploymentSmokeResult(
        schema_version=DEPLOYMENT_SMOKE_SCHEMA_VERSION,
        checked_at=datetime.now(UTC).isoformat(),
        node=socket.gethostname(),
        app_name=resolved_settings.app_name,
        checks=checks,
        succeeded=all(check.succeeded for check in checks),
    )


def main() -> None:
    try:
        result = run_deployment_smoke()
    except ValidationError as error:
        errors = [
            {
                "field": ".".join(str(part) for part in item["loc"]),
                "message": item["msg"],
            }
            for item in error.errors(include_input=False, include_url=False)
        ]
        print(
            json.dumps(
                {"succeeded": False, "configuration_errors": errors},
                sort_keys=True,
            )
        )
        raise SystemExit(1) from None
    except Exception as error:
        print(
            json.dumps(
                {
                    "succeeded": False,
                    "configuration_error": f"{type(error).__name__}: {error}",
                },
                sort_keys=True,
            )
        )
        raise SystemExit(1) from None
    print(json.dumps(asdict(result), sort_keys=True))
    if not result.succeeded:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
