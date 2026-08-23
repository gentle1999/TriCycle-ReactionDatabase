from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]


def _read(path: str) -> str:
    return (REPOSITORY_ROOT / path).read_text(encoding="utf-8")


def test_api_and_frontend_images_are_reproducible_and_unprivileged() -> None:
    api = _read("Dockerfile.api")
    frontend = _read("Dockerfile.frontend")

    assert "python:3.12-slim-bookworm@sha256:" in api
    assert "uv sync --frozen --no-dev" in api
    assert "USER tricycle" in api
    assert "/health/ready" in api

    assert "node:22-bookworm-slim@sha256:" in frontend
    assert "npm ci" in frontend
    assert "npm run build" in frontend
    assert "nginx:1.28-alpine@sha256:" in frontend
    assert "USER nginx" in frontend


def test_compose_gates_api_and_https_edge_on_health_and_initialization() -> None:
    compose = _read("compose.yaml")

    for service in ("migrate:", "bootstrap:", "api:", "frontend:", "caddy:"):
        assert service in compose
    assert "condition: service_completed_successfully" in compose
    assert compose.count("condition: service_healthy") >= 4
    assert "TRICYCLE_API_HOST: 0.0.0.0" in compose
    caddyfile = _read("infra/caddy/Caddyfile")
    assert "CADDY_API_UPSTREAM: ${CADDY_API_UPSTREAM:-api:8000}" in compose
    assert "CADDY_FRONTEND_UPSTREAM: ${CADDY_FRONTEND_UPSTREAM:-frontend:8080}" in compose
    assert (
        '"${CADDY_BIND_ADDRESS:-0.0.0.0}:${CADDY_HTTPS_PORT:-443}:${CADDY_HTTPS_PORT:-443}"'
        in compose
    )
    assert "caddy-data:" in compose
    assert "caddy-config:" in compose
    assert "auto_https disable_redirects" in caddyfile
    assert '"${CADDY_SERVER_NAME:-localhost}:127.0.0.1"' in compose
    assert "https://$${CADDY_SERVER_NAME}:$${CADDY_HTTPS_PORT}/health/live" in compose


def test_compose_data_service_bindings_keep_storage_console_separate() -> None:
    compose = _read("compose.yaml")

    assert '"${POSTGRES_BIND_ADDRESS:-127.0.0.1}:${POSTGRES_PORT:-5432}:5432"' in compose
    assert '"${RUSTFS_BIND_ADDRESS:-127.0.0.1}:${RUSTFS_API_PORT:-19000}:9000"' in compose
    assert (
        '"${RUSTFS_CONSOLE_BIND_ADDRESS:-127.0.0.1}:${RUSTFS_CONSOLE_PORT:-19001}:9001"' in compose
    )


def test_compute_compose_overlay_removes_local_data_dependencies() -> None:
    overlay = _read("compose.compute.yaml")

    assert "profiles:" in overlay
    assert "- local-data" in overlay
    assert "depends_on: !reset []" in overlay
    assert "depends_on: !override" in overlay
    assert "TRICYCLE_COMPOSE_CA_DIRECTORY" in overlay


def test_nginx_generates_only_missing_development_certificate() -> None:
    entrypoint = _read("infra/nginx/docker-entrypoint.sh")

    assert 'if [ ! -f "$certificate" ]; then' in entrypoint
    assert "openssl req" in entrypoint
    assert "-x509" in entrypoint
    assert "subjectAltName=" in entrypoint
    assert "TLS certificate and private key must be provided together" in entrypoint
    assert "NGINX_REQUIRE_PROVIDED_CERTIFICATE=true but" in entrypoint
