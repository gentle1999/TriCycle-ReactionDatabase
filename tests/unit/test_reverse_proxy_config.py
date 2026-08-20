from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]


def _host_configuration() -> str:
    return (REPOSITORY_ROOT / "infra/nginx/tricycle.conf").read_text(encoding="utf-8")


def _api_locations() -> str:
    return (REPOSITORY_ROOT / "infra/nginx/api-locations.conf").read_text(encoding="utf-8")


def _location(configuration: str, declaration: str) -> str:
    return configuration.split(declaration, 1)[1].split("\n}", 1)[0]


def test_nginx_api_proxy_cannot_use_shared_cache() -> None:
    api_location = _location(_api_locations(), "location ^~ /api/ {")

    assert "proxy_cache off;" in api_location
    assert "proxy_no_cache 1;" in api_location
    assert "proxy_cache_bypass 1;" in api_location
    assert "proxy_hide_header Cache-Control;" in api_location
    assert 'add_header Cache-Control "private, no-store" always;' in api_location


def test_nginx_editor_document_receives_frame_ancestor_csp_header() -> None:
    configuration = _host_configuration()
    editor_location = configuration.split(
        "location = /editor/chemdoodle-editor.html {",
        1,
    )[1].split("\n    }", 1)[0]

    assert "add_header Content-Security-Policy" in editor_location
    assert "frame-ancestors 'self'" in editor_location


def test_nginx_proxies_health_docs_and_streaming_mcp_explicitly() -> None:
    configuration = _api_locations()

    for location in (
        "location = /health/live {",
        "location = /health/ready {",
        "location = /redoc {",
        "location = /docs/oauth2-redirect {",
        "location = /openapi.json {",
        "location = /graphql {",
    ):
        assert location in configuration

    for prefix in ("/mcp", "/nexusx/mcp"):
        mcp_location = _location(configuration, f"location ^~ {prefix} {{")
        assert "proxy_http_version 1.1;" in mcp_location
        assert 'proxy_set_header Connection "";' in mcp_location
        assert "proxy_buffering off;" in mcp_location
        assert "proxy_read_timeout 3600s;" in mcp_location


def test_nginx_does_not_publish_internal_metrics() -> None:
    internal_location = _location(_api_locations(), "location ^~ /internal/ {")

    assert "return 404;" in internal_location
    assert "proxy_pass" not in internal_location


def test_nginx_enforces_one_shared_gateway_rate_budget() -> None:
    host = _host_configuration()
    locations = _api_locations()

    assert "limit_req_zone $binary_remote_addr zone=reaction_database_api:10m rate=120r/m;" in host
    assert locations.count("limit_req zone=reaction_database_api burst=60 nodelay;") >= 4


def test_nginx_api_upstream_supports_multiple_hosts() -> None:
    configuration = _host_configuration()
    upstream = configuration.split("upstream reaction_database_api {", 1)[1].split(
        "\n}",
        1,
    )[0]

    assert "least_conn;" in upstream
    assert "server api01.internal.example:8000" in upstream
    assert "server api02.internal.example:8000" in upstream
    assert "proxy_pass http://reaction_database_api" in _api_locations()


def test_container_nginx_terminates_tls_and_proxies_frontend_and_api() -> None:
    configuration = (REPOSITORY_ROOT / "infra/nginx/container.conf.template").read_text()

    assert "listen 8080;" in configuration
    assert "return 308 https://$host$request_uri;" in configuration
    assert "listen 8443 ssl;" in configuration
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in configuration
    assert "server api:8000" in configuration
    assert "server frontend:8080" in configuration
    assert "include /etc/nginx/includes/api-locations.conf;" in configuration
    assert "proxy_pass http://reaction_database_frontend;" in configuration
