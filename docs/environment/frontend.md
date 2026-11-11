# 前端构建与分页环境变量

Vite 构建变量、分页与预取。

[返回环境变量索引](../environment-variables.md)

## Compose、前端与脚本变量

| 变量 | `.env.example` 示例值 | 来源 |
| --- | --- | --- |
| `VITE_API_BASE_URL` | `（空）` | Compose, Vite |
| `VITE_API_PROXY_TARGET` | `未设置` | Vite |
| `VITE_APP_NAME` | `Example Chemistry Database` | Compose, Vite |
| `VITE_APP_TAGLINE` | `Computational data browser` | Compose, Vite |
| `VITE_BRAND_NAME` | `Example Research Platform` | Compose, Vite |
| `VITE_CATALOG_PAGE_SIZE_MAX` | `500` | Compose, Vite |
| `VITE_CSRF_COOKIE_NAME` | `example_csrf` | Compose, Vite |
| `VITE_CSRF_HEADER_NAME` | `x-csrf-token` | Compose, Vite |
| `VITE_DEFAULT_CATALOG_PAGE_SIZE` | `50` | Compose, Vite |
| `VITE_DEFAULT_REACTION_PAGE_SIZE` | `12` | Compose, Vite |
| `VITE_MCP_SERVER_NAME` | `example-chemistry-database` | Compose, Vite |
| `VITE_NEXUSX_CORE_URL` | `未设置` | Vite |
| `VITE_NEXUSX_GRAPHQL_URL` | `未设置` | Vite |
| `VITE_NEXUSX_MCP_URL` | `未设置` | Vite |
| `VITE_NEXUSX_PAGINATED_GRAPHQL_URL` | `未设置` | Vite |
| `VITE_NEXUSX_REST_URL` | `未设置` | Vite |
| `VITE_NEXUSX_VOYAGER_URL` | `未设置` | Vite |
| `VITE_PAGE_PREFETCH_RADIUS` | `3` | Compose, Vite |
| `VITE_REACTION_PAGE_SIZE_MAX` | `12` | Compose, Vite |

## 配置关系

`VITE_*` 在构建时注入，修改后重新构建前端。`make frontend-build` 会用 `FrontendBuildSettings` 校验分页值及默认值与上限的关系；`VITE_PAGE_PREFETCH_RADIUS=0` 关闭邻页预取。
