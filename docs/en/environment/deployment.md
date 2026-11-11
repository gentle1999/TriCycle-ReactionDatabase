# Compose and infrastructure environment variables

Container images, PostgreSQL, Caddy, Keycloak, and build proxies.

[Back to the environment-variable index](../environment-variables.md)

## Compose, frontend, and script variables

| Variable | `.env.example` example | Source |
| --- | --- | --- |
| `CADDY_API_UPSTREAM` | `api:8000` | Compose |
| `CADDY_BIND_ADDRESS` | `0.0.0.0` | Compose |
| `CADDY_CERT_NAMES` | `localhost, 127.0.0.1` | Compose |
| `CADDY_FRONTEND_UPSTREAM` | `frontend:8080` | Compose |
| `CADDY_HTTPS_PORT` | `443` | Compose |
| `CADDY_HTTP_PORT` | `80` | Compose |
| `CADDY_KEYCLOAK_UPSTREAM` | `unset` | Compose |
| `CADDY_SERVER_NAME` | `localhost` | Compose |
| `COMPOSE_PROJECT_NAME` | `reaction-database-development` | Compose |
| `HTTPS_PROXY` | `unset` | Compose |
| `HTTP_PROXY` | `unset` | Compose |
| `KEYCLOAK_ADMIN_PASSWORD` | `unset` | Compose |
| `KEYCLOAK_ADMIN_USERNAME` | `unset` | Compose |
| `KEYCLOAK_BIND_ADDRESS` | `unset` | Compose |
| `KEYCLOAK_HOSTNAME` | `unset` | Compose |
| `KEYCLOAK_PORT` | `unset` | Compose |
| `NO_PROXY` | `unset` | script/process |
| `POSTGRES_BIND_ADDRESS` | `127.0.0.1` | Compose |
| `POSTGRES_DATA_DIRECTORY` | `unset` | Compose |
| `POSTGRES_DB` | `example_reaction_db` | Compose |
| `POSTGRES_PASSWORD` | `example-local-password` | Compose |
| `POSTGRES_PORT` | `5432` | Compose |
| `POSTGRES_USER` | `example_user` | Compose |
| `TRICYCLE_ACCEPTANCE_BEARER_TOKEN` | `unset` | script/process |
| `TRICYCLE_API_IMAGE` | `unset` | Compose |
| `TRICYCLE_CADDY_IMAGE` | `unset` | Compose |
| `TRICYCLE_COMPOSE_CA_DIRECTORY` | `/etc/reaction-database/ca` | Compose |
| `TRICYCLE_COMPOSE_DATABASE_URL` | `postgresql+psycopg://example_user:example-local-password@postgres:5432/example_reaction_db` | Compose |
| `TRICYCLE_COMPOSE_RUSTFS_ENDPOINT_URL` | `http://rustfs:9000` | Compose |
| `TRICYCLE_DEBIAN_MIRROR` | `https://mirrors.zju.edu.cn/debian` | Compose |
| `TRICYCLE_DEBIAN_SECURITY_MIRROR` | `https://mirrors.zju.edu.cn/debian-security` | Compose |
| `TRICYCLE_FRONTEND_IMAGE` | `unset` | Compose |
| `TRICYCLE_NGINX_BASE_IMAGE` | `registry.example/nginx:1.28-alpine@sha256:...` | Compose |
| `TRICYCLE_NODE_BASE_IMAGE` | `registry.example/node:22-bookworm-slim@sha256:...` | Compose |
| `TRICYCLE_NPM_REGISTRY` | `https://registry.npmmirror.com` | Compose |
| `TRICYCLE_PYPI_INDEX_URL` | `https://mirrors.zju.edu.cn/pypi/web/simple` | Compose |
| `TRICYCLE_PYTHON_BASE_IMAGE` | `registry.example/python:3.12-slim-bookworm@sha256:...` | Compose |
| `UV_VERSION` | `unset` | Compose |
| `http_proxy` | `unset` | Compose |
| `https_proxy` | `unset` | Compose |
| `no_proxy` | `unset` | script/process |

## Configuration notes

The Compose database and RustFS URLs override container endpoints; host processes use the application settings. `make validate-config` checks ports, worker timeouts, and connection URLs through a separate `ComposeBuildSettings` model. Keys in `.env.example` are development examples only.
