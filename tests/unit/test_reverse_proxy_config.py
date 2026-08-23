from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]


def _host_configuration() -> str:
    return (REPOSITORY_ROOT / "infra/nginx/tricycle.conf").read_text(encoding="utf-8")


def _api_locations() -> str:
    return (REPOSITORY_ROOT / "infra/nginx/api-locations.conf").read_text(encoding="utf-8")


def _caddy_configuration() -> str:
    return (REPOSITORY_ROOT / "infra/caddy/Caddyfile").read_text(encoding="utf-8")


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


def test_nginx_leaves_request_budgets_to_the_application() -> None:
    host = _host_configuration()
    locations = _api_locations()
    api_location = _location(locations, "location ^~ /api/ {")

    assert "limit_req" not in host
    assert "limit_req" not in locations
    assert "client_max_body_size 0;" in host
    assert "proxy_read_timeout 3600s;" in host
    assert "proxy_send_timeout 3600s;" in host
    assert "proxy_request_buffering off;" in api_location
    assert "proxy_buffering off;" in api_location


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
    assert "return 308 https://$host:${NGINX_PUBLIC_HTTPS_PORT}$request_uri;" in configuration
    assert "listen 8443 ssl;" in configuration
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in configuration
    assert "server api:8000" in configuration
    assert "server frontend:8080" in configuration
    assert "include /etc/nginx/includes/api-locations.conf;" in configuration
    assert "proxy_pass http://reaction_database_frontend;" in configuration


def test_caddy_uses_acme_tls_and_explicit_http_redirect() -> None:
    configuration = _caddy_configuration()

    assert "auto_https disable_redirects" in configuration
    assert "http://{$CADDY_SERVER_NAME:localhost}:{$CADDY_HTTP_PORT:80}" in configuration
    assert "redir https://{host}:{$CADDY_HTTPS_PORT:443}{uri} 308" in configuration
    assert "caddy-data" not in configuration


def test_caddy_preserves_api_boundaries_and_nexusx_rewrites() -> None:
    configuration = _caddy_configuration()

    assert "handle @internal" in configuration
    assert "respond 404" in configuration
    assert 'header @api Cache-Control "private, no-store"' in configuration
    assert "flush_interval -1" in configuration
    assert "response_header_timeout 1h" in configuration
    assert "read_timeout 1h" in configuration
    assert "write_timeout 1h" in configuration
    for replacement in (
        "uri replace /nexusx/paginated-graphql /graphql",
        "uri replace /nexusx/mcp /mcp",
        "uri replace /nexusx/voyager /voyager",
    ):
        assert replacement in configuration
