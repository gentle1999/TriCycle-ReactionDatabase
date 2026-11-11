# Compose 与基础设施环境变量

容器镜像、PostgreSQL、Caddy、Keycloak 与构建代理。

[返回环境变量索引](../environment-variables.md)

## Compose、前端与脚本变量

| 变量 | `.env.example` 示例值 | 来源 |
| --- | --- | --- |
| `CADDY_API_UPSTREAM` | `api:8000` | Compose |
| `CADDY_BIND_ADDRESS` | `0.0.0.0` | Compose |
| `CADDY_CERT_NAMES` | `localhost, 127.0.0.1` | Compose |
| `CADDY_FRONTEND_UPSTREAM` | `frontend:8080` | Compose |
| `CADDY_HTTPS_PORT` | `443` | Compose |
| `CADDY_HTTP_PORT` | `80` | Compose |
| `CADDY_KEYCLOAK_UPSTREAM` | `未设置` | Compose |
| `CADDY_SERVER_NAME` | `localhost` | Compose |
| `COMPOSE_PROJECT_NAME` | `reaction-database-development` | Compose |
| `HTTPS_PROXY` | `未设置` | Compose |
| `HTTP_PROXY` | `未设置` | Compose |
| `KEYCLOAK_ADMIN_PASSWORD` | `未设置` | Compose |
| `KEYCLOAK_ADMIN_USERNAME` | `未设置` | Compose |
| `KEYCLOAK_BIND_ADDRESS` | `未设置` | Compose |
| `KEYCLOAK_HOSTNAME` | `未设置` | Compose |
| `KEYCLOAK_PORT` | `未设置` | Compose |
| `NO_PROXY` | `未设置` | 脚本/进程 |
| `POSTGRES_BIND_ADDRESS` | `127.0.0.1` | Compose |
| `POSTGRES_DATA_DIRECTORY` | `未设置` | Compose |
| `POSTGRES_DB` | `example_reaction_db` | Compose |
| `POSTGRES_PASSWORD` | `example-local-password` | Compose |
| `POSTGRES_PORT` | `5432` | Compose |
| `POSTGRES_USER` | `example_user` | Compose |
| `TRICYCLE_ACCEPTANCE_BEARER_TOKEN` | `未设置` | 脚本/进程 |
| `TRICYCLE_API_IMAGE` | `未设置` | Compose |
| `TRICYCLE_CADDY_IMAGE` | `未设置` | Compose |
| `TRICYCLE_COMPOSE_CA_DIRECTORY` | `/etc/reaction-database/ca` | Compose |
| `TRICYCLE_COMPOSE_DATABASE_URL` | `postgresql+psycopg://example_user:example-local-password@postgres:5432/example_reaction_db` | Compose |
| `TRICYCLE_COMPOSE_RUSTFS_ENDPOINT_URL` | `http://rustfs:9000` | Compose |
| `TRICYCLE_DEBIAN_MIRROR` | `https://mirrors.zju.edu.cn/debian` | Compose |
| `TRICYCLE_DEBIAN_SECURITY_MIRROR` | `https://mirrors.zju.edu.cn/debian-security` | Compose |
| `TRICYCLE_FRONTEND_IMAGE` | `未设置` | Compose |
| `TRICYCLE_NGINX_BASE_IMAGE` | `registry.example/nginx:1.28-alpine@sha256:...` | Compose |
| `TRICYCLE_NODE_BASE_IMAGE` | `registry.example/node:22-bookworm-slim@sha256:...` | Compose |
| `TRICYCLE_NPM_REGISTRY` | `https://registry.npmmirror.com` | Compose |
| `TRICYCLE_PYPI_INDEX_URL` | `https://mirrors.zju.edu.cn/pypi/web/simple` | Compose |
| `TRICYCLE_PYTHON_BASE_IMAGE` | `registry.example/python:3.12-slim-bookworm@sha256:...` | Compose |
| `UV_VERSION` | `未设置` | Compose |
| `http_proxy` | `未设置` | Compose |
| `https_proxy` | `未设置` | Compose |
| `no_proxy` | `未设置` | 脚本/进程 |

## 配置关系

`TRICYCLE_COMPOSE_DATABASE_URL` 和 `TRICYCLE_COMPOSE_RUSTFS_ENDPOINT_URL` 是容器内覆盖地址；宿主机进程分别使用应用设置中的地址。`make validate-config` 用独立的 `ComposeBuildSettings` 校验端口、worker 超时和连接地址。`.env.example` 中的密钥仅为开发示例。
