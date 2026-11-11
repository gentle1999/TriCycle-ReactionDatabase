# Frontend build and pagination environment variables

Vite build variables, pagination, and prefetch.

[Back to the environment-variable index](../environment-variables.md)

## Compose, frontend, and script variables

| Variable | `.env.example` example | Source |
| --- | --- | --- |
| `VITE_API_BASE_URL` | `(empty)` | Compose, Vite |
| `VITE_API_PROXY_TARGET` | `unset` | Vite |
| `VITE_APP_NAME` | `Example Chemistry Database` | Compose, Vite |
| `VITE_APP_TAGLINE` | `Computational data browser` | Compose, Vite |
| `VITE_BRAND_NAME` | `Example Research Platform` | Compose, Vite |
| `VITE_CATALOG_PAGE_SIZE_MAX` | `500` | Compose, Vite |
| `VITE_CSRF_COOKIE_NAME` | `example_csrf` | Compose, Vite |
| `VITE_CSRF_HEADER_NAME` | `x-csrf-token` | Compose, Vite |
| `VITE_DEFAULT_CATALOG_PAGE_SIZE` | `50` | Compose, Vite |
| `VITE_DEFAULT_REACTION_PAGE_SIZE` | `12` | Compose, Vite |
| `VITE_MCP_SERVER_NAME` | `example-chemistry-database` | Compose, Vite |
| `VITE_NEXUSX_CORE_URL` | `unset` | Vite |
| `VITE_NEXUSX_GRAPHQL_URL` | `unset` | Vite |
| `VITE_NEXUSX_MCP_URL` | `unset` | Vite |
| `VITE_NEXUSX_PAGINATED_GRAPHQL_URL` | `unset` | Vite |
| `VITE_NEXUSX_REST_URL` | `unset` | Vite |
| `VITE_NEXUSX_VOYAGER_URL` | `unset` | Vite |
| `VITE_PAGE_PREFETCH_RADIUS` | `3` | Compose, Vite |
| `VITE_REACTION_PAGE_SIZE_MAX` | `12` | Compose, Vite |

## Configuration notes

`VITE_*` values are injected at build time; rebuild the frontend after changes. `make frontend-build` validates pagination values and their limits with `FrontendBuildSettings`; `VITE_PAGE_PREFETCH_RADIUS=0` disables adjacent-page prefetch.
