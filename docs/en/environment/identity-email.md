# Identity and email environment variables

OIDC, sessions, bootstrap, email, and CORS.

[Back to the environment-variable index](../environment-variables.md)

## Backend settings

| Variable | Code default | Value constraints |
| --- | --- | --- |
| `TRICYCLE_OIDC_METADATA_TTL_SECONDS` | `300` | ≥1, ≤86400 |
| `TRICYCLE_SESSION_LAST_SEEN_INTERVAL_SECONDS` | `300` | ≥1, ≤86400 |
| `TRICYCLE_MCP_TOKEN_LAST_USED_INTERVAL_SECONDS` | `300` | ≥1, ≤86400 |
| `TRICYCLE_AUTH_MODE` | `development` | — |
| `TRICYCLE_DEVELOPMENT_USER_ID` | `00000000-0000-7000-8000-000000000002` | — |
| `TRICYCLE_OIDC_ISSUER` | `unset` | — |
| `TRICYCLE_OIDC_AUDIENCE` | `unset` | — |
| `TRICYCLE_OIDC_JWKS_URL` | `unset` | — |
| `TRICYCLE_OIDC_CA_BUNDLE` | `unset` | — |
| `TRICYCLE_OIDC_ALGORITHM` | `RS256` | — |
| `TRICYCLE_OIDC_CLIENT_ID` | `example-chemistry-database` | — |
| `TRICYCLE_OIDC_CLIENT_SECRET` | `unset` | — |
| `TRICYCLE_OIDC_REDIRECT_URI` | `unset` | — |
| `TRICYCLE_OIDC_FRONTEND_URL` | `http://127.0.0.1:5173` | — |
| `TRICYCLE_OIDC_BOOTSTRAP_SUBJECT` | `unset` | — |
| `TRICYCLE_OIDC_BOOTSTRAP_USER_ID` | `unset` | — |
| `TRICYCLE_BOOTSTRAP_OIDC_ISSUER` | `unset` | — |
| `TRICYCLE_BOOTSTRAP_OIDC_SUBJECT` | `unset` | — |
| `TRICYCLE_BOOTSTRAP_ADMIN_EMAIL` | `unset` | — |
| `TRICYCLE_BOOTSTRAP_ADMIN_DISPLAY_NAME` | `unset` | — |
| `TRICYCLE_BOOTSTRAP_ORGANIZATION_SLUG` | `unset` | — |
| `TRICYCLE_BOOTSTRAP_ORGANIZATION_NAME` | `unset` | — |
| `TRICYCLE_BOOTSTRAP_PROJECT_SLUG` | `unset` | — |
| `TRICYCLE_BOOTSTRAP_PROJECT_NAME` | `unset` | — |
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
| `TRICYCLE_SMTP_HOST` | `unset` | — |
| `TRICYCLE_SMTP_PORT` | `587` | ≥1, ≤65535 |
| `TRICYCLE_SMTP_USERNAME` | `unset` | — |
| `TRICYCLE_SMTP_PASSWORD` | `unset` | — |
| `TRICYCLE_SMTP_FROM_EMAIL` | `unset` | — |
| `TRICYCLE_SMTP_STARTTLS` | `true` | — |
| `TRICYCLE_SMTP_CA_BUNDLE` | `unset` | — |
| `TRICYCLE_SMTP_TIMEOUT_SECONDS` | `15` | ≥1, ≤120 |
| `TRICYCLE_CORS_ORIGINS` | `["http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:5174", "http://localhost:5174"]` | — |

## Compose, frontend, and script variables

| Variable | `.env.example` example | Source |
| --- | --- | --- |
| `TRICYCLE_BOOTSTRAP_MODE` | `development` | Compose |

## Configuration notes

For production OIDC, SMTP, cookie, and TLS checks, see [deployment configuration](../deployment-configuration.md).
