# 运行与查询环境变量

服务名称、数据库、GraphQL、查询预算与统计。

[返回环境变量索引](../environment-variables.md)

## 后端设置

| 变量 | 代码默认值 | 数值或格式约束 |
| --- | --- | --- |
| `TRICYCLE_APP_NAME` | `Example Chemistry Database` | — |
| `TRICYCLE_BRAND_NAME` | `Example Research Platform` | — |
| `TRICYCLE_MCP_SERVER_NAME` | `Example Chemistry Database MCP` | — |
| `TRICYCLE_NEXUSX_APP_NAME` | `example-chemistry-database` | regex: ^[a-z0-9]+(?:-[a-z0-9]+)*$ |
| `TRICYCLE_NEXUSX_PLAYGROUND_NAME` | `example-chemistry-database-playground` | regex: ^[a-z0-9]+(?:-[a-z0-9]+)*$ |
| `TRICYCLE_NEXUSX_DATABASE_CLUSTER_NAME` | `example-chemistry-postgresql` | regex: ^[a-z0-9]+(?:-[a-z0-9]+)*$ |
| `TRICYCLE_NEXUSX_DATABASE_CLUSTER_COLOR` | `#E3F2FD` | regex: ^#[0-9A-Fa-f]{6}$ |
| `TRICYCLE_ENVIRONMENT` | `development` | — |
| `TRICYCLE_DEBUG` | `false` | — |
| `TRICYCLE_API_HOST` | `0.0.0.0` | — |
| `TRICYCLE_API_PORT` | `8000` | ≥1, ≤65535 |
| `TRICYCLE_GEOMETRY_MATCH_INPUT_BATCH_SIZE` | `128` | ≥1, ≤100000 |
| `TRICYCLE_ANALYTICS_HISTOGRAM_BIN_COUNT` | `12` | ≥1, ≤1000 |
| `TRICYCLE_ANALYTICS_MAX_SCATTER_POINTS` | `1000` | ≥1, ≤1000000 |
| `TRICYCLE_DATABASE_URL` | `postgresql+psycopg://example_user:example-local-password@127.0.0.1:5432/example_reaction_db` | — |
| `TRICYCLE_DATABASE_POOL_SIZE` | `5` | ≥1, ≤256 |
| `TRICYCLE_DATABASE_MAX_OVERFLOW` | `10` | ≥0, ≤512 |
| `TRICYCLE_DATABASE_POOL_TIMEOUT_SECONDS` | `30.0` | >0.0, ≤600.0 |
| `TRICYCLE_QUERY_STATEMENT_TIMEOUT_MS` | `15000` | ≥100, ≤300000 |
| `TRICYCLE_SLOW_QUERY_THRESHOLD_MS` | `500` | ≥1, ≤300000 |
| `TRICYCLE_GRAPHQL_MAX_QUERY_CHARACTERS` | `20000` | ≥100, ≤1000000 |
| `TRICYCLE_GRAPHQL_MAX_TOKENS` | `2000` | ≥10, ≤100000 |
| `TRICYCLE_GRAPHQL_MAX_DEPTH` | `12` | ≥1, ≤100 |
| `TRICYCLE_GRAPHQL_MAX_COMPLEXITY` | `250` | ≥1, ≤100000 |
| `TRICYCLE_QUERY_RATE_LIMIT_REQUESTS` | `120` | ≥1, ≤1000000 |
| `TRICYCLE_READ_RATE_LIMIT_REQUESTS` | `10000` | ≥1, ≤1000000 |
| `TRICYCLE_MOLECULAR_GRAPH_MATCH_TIMEOUT_SECONDS` | `5.0` | >0.0, ≤300.0 |
| `TRICYCLE_MOLECULAR_GRAPH_MATCH_ISOLATION_ATOM_COUNT` | `48` | ≥1, ≤100000 |
| `TRICYCLE_MOLECULAR_GRAPH_MATCH_MAX_RESULTS` | `1000` | ≥1, ≤100000 |
| `TRICYCLE_MOLECULE_QUERY_RATE_LIMIT_REQUESTS` | `10000` | ≥1, ≤1000000 |
| `TRICYCLE_DEPICTION_RATE_LIMIT_REQUESTS` | `10000` | ≥1, ≤1000000 |
| `TRICYCLE_QUERY_RATE_LIMIT_WINDOW_SECONDS` | `60` | ≥1, ≤86400 |
| `TRICYCLE_RATE_LIMIT_BACKEND` | `memory` | — |
| `TRICYCLE_RATE_LIMIT_REDIS_URL` | `未设置` | — |
| `TRICYCLE_RATE_LIMIT_KEY_PREFIX` | `reaction-database` | regex: ^[a-z0-9]+(?:[-_:][a-z0-9]+)*$ |
| `TRICYCLE_STRUCTURE_QUERY_MAX_CHARACTERS` | `16384` | ≥100, ≤1000000 |
| `TRICYCLE_STRUCTURE_CANDIDATE_LIMIT` | `50000` | ≥1, ≤10000000 |

## 配置关系

后端设置在进程启动时读取；修改后重启相关服务。
