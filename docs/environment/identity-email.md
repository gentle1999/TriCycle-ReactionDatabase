# 身份认证与邮件环境变量

OIDC、会话、Bootstrap、邮件与跨域。

[返回环境变量索引](../environment-variables.md)

## 后端设置

| 变量 | 代码默认值 | 数值或格式约束 |
| --- | --- | --- |
| `TRICYCLE_OIDC_METADATA_TTL_SECONDS` | `300` | ≥1, ≤86400 |
| `TRICYCLE_SESSION_LAST_SEEN_INTERVAL_SECONDS` | `300` | ≥1, ≤86400 |
| `TRICYCLE_MCP_TOKEN_LAST_USED_INTERVAL_SECONDS` | `300` | ≥1, ≤86400 |
| `TRICYCLE_AUTH_MODE` | `development` | — |
| `TRICYCLE_DEVELOPMENT_USER_ID` | `00000000-0000-7000-8000-000000000002` | — |
| `TRICYCLE_OIDC_ISSUER` | `未设置` | — |
| `TRICYCLE_OIDC_AUDIENCE` | `未设置` | — |
| `TRICYCLE_OIDC_JWKS_URL` | `未设置` | — |
| `TRICYCLE_OIDC_CA_BUNDLE` | `未设置` | — |
| `TRICYCLE_OIDC_ALGORITHM` | `RS256` | — |
| `TRICYCLE_OIDC_CLIENT_ID` | `example-chemistry-database` | — |
| `TRICYCLE_OIDC_CLIENT_SECRET` | `未设置` | — |
| `TRICYCLE_OIDC_REDIRECT_URI` | `未设置` | — |
| `TRICYCLE_OIDC_FRONTEND_URL` | `http://127.0.0.1:5173` | — |
| `TRICYCLE_OIDC_BOOTSTRAP_SUBJECT` | `未设置` | — |
| `TRICYCLE_OIDC_BOOTSTRAP_USER_ID` | `未设置` | — |
| `TRICYCLE_BOOTSTRAP_OIDC_ISSUER` | `未设置` | — |
| `TRICYCLE_BOOTSTRAP_OIDC_SUBJECT` | `未设置` | — |
| `TRICYCLE_BOOTSTRAP_ADMIN_EMAIL` | `未设置` | — |
| `TRICYCLE_BOOTSTRAP_ADMIN_DISPLAY_NAME` | `未设置` | — |
| `TRICYCLE_BOOTSTRAP_ORGANIZATION_SLUG` | `未设置` | — |
| `TRICYCLE_BOOTSTRAP_ORGANIZATION_NAME` | `未设置` | — |
| `TRICYCLE_BOOTSTRAP_PROJECT_SLUG` | `未设置` | — |
| `TRICYCLE_BOOTSTRAP_PROJECT_NAME` | `未设置` | — |
| `TRICYCLE_BOOTSTRAP_SYSTEM_USER_DISPLAY_NAME` | `Application System` | — |
| `TRICYCLE_BOOTSTRAP_DEVELOPMENT_USER_DISPLAY_NAME` | `Development User` | — |
| `TRICYCLE_BOOTSTRAP_DEVELOPMENT_USER_EMAIL` | `developer@localhost` | — |
| `TRICYCLE_SESSION_SECRET` | `development-session-secret-change-me` | — |
| `TRICYCLE_SESSION_COOKIE_NAME` | `example_session` | regex: ^[A-Za-z0-9_-]+$ |
| `TRICYCLE_CSRF_COOKIE_NAME` | `example_csrf` | regex: ^[A-Za-z0-9_-]+$ |
| `TRICYCLE_CSRF_HEADER_NAME` | `x-csrf-token` | regex: ^[A-Za-z0-9_-]+$ |
| `TRICYCLE_SESSION_TTL_SECONDS` | `86400` | ≥300, ≤2592000 |
| `TRICYCLE_MCP_TOKEN_TTL_SECONDS` | `31536000` | ≥300, ≤157680000 |
| `TRICYCLE_SESSION_COOKIE_SECURE` | `false` | — |
| `TRICYCLE_EMAIL_DELIVERY_MODE` | `link` | — |
| `TRICYCLE_SMTP_HOST` | `未设置` | — |
| `TRICYCLE_SMTP_PORT` | `587` | ≥1, ≤65535 |
| `TRICYCLE_SMTP_USERNAME` | `未设置` | — |
| `TRICYCLE_SMTP_PASSWORD` | `未设置` | — |
| `TRICYCLE_SMTP_FROM_EMAIL` | `未设置` | — |
| `TRICYCLE_SMTP_STARTTLS` | `true` | — |
| `TRICYCLE_SMTP_CA_BUNDLE` | `未设置` | — |
| `TRICYCLE_SMTP_TIMEOUT_SECONDS` | `15` | ≥1, ≤120 |
| `TRICYCLE_CORS_ORIGINS` | `["http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:5174", "http://localhost:5174"]` | — |

## Compose、前端与脚本变量

| 变量 | `.env.example` 示例值 | 来源 |
| --- | --- | --- |
| `TRICYCLE_BOOTSTRAP_MODE` | `development` | Compose |

## 配置关系

生产环境的 OIDC、SMTP、Cookie 与 TLS 组合校验见 [部署配置](../deployment-configuration.md)。
