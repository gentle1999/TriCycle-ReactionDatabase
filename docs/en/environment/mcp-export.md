# MCP and data export environment variables

MCP response budgets, TS geometry, and UniTS exports.

[Back to the environment-variable index](../environment-variables.md)

## Backend settings

| Variable | Code default | Value constraints |
| --- | --- | --- |
| `TRICYCLE_MCP_TS_GEOMETRY_EXPORT_MAX_BYTES` | `524288` | ≥1024, ≤67108864 |
| `TRICYCLE_MCP_TS_GEOMETRY_EXPORT_MAX_RECORDS` | `100` | ≥1, ≤10000 |
| `TRICYCLE_MCP_EXPORT_MAX_RECORDS` | `100` | ≥1, ≤10000 |
| `TRICYCLE_MCP_EXPORT_MAX_BYTES` | `524288` | ≥1024, ≤67108864 |
| `TRICYCLE_MCP_ARTIFACT_PREVIEW_MAX_BYTES` | `262144` | ≥1024, ≤67108864 |
| `TRICYCLE_MCP_BATCH_MAX_FILES` | `100` | ≥1, ≤10000 |
| `TRICYCLE_MCP_ARRAY_PREVIEW_MAX_ELEMENTS` | `4096` | ≥1, ≤1000000 |
| `TRICYCLE_MCP_BATCH_DOWNLOAD_CONCURRENCY` | `16` | ≥1, ≤128 |
| `TRICYCLE_MCP_CSV_MAX_OFFSET` | `1000000` | ≥0, ≤100000000 |
| `TRICYCLE_MAPPED_GEOMETRY_EXPORT_PAGE_SIZE` | `64` | ≥1, ≤10000 |
| `TRICYCLE_UNITS_TS_EXPORT_PAGE_SIZE` | `128` | ≥1, ≤10000 |
| `TRICYCLE_UNITS_TS_DOWNLOAD_TTL_SECONDS` | `604800` | ≥60, ≤31536000 |
| `TRICYCLE_UNITS_TS_JOB_LEASE_SECONDS` | `1800` | ≥60, ≤86400 |
| `TRICYCLE_UNITS_TS_MAX_REACTIVE_ATOMS` | `20` | ≥1, ≤10000 |
| `TRICYCLE_UNITS_TS_MAX_RC_BONDS` | `25` | ≥1, ≤1000000 |
| `TRICYCLE_UNITS_TS_MAX_RC_ANGLES` | `25` | ≥1, ≤1000000 |
| `TRICYCLE_UNITS_TS_LEGACY_EXPORT_PURGE_INTERVAL_SECONDS` | `300.0` | >0.0, ≤86400.0 |
| `TRICYCLE_UNITS_DATASET_WORKER_POLL_INTERVAL_SECONDS` | `2.0` | >0.05, ≤300.0 |

## Compose, frontend, and script variables

| Variable | `.env.example` example | Source |
| --- | --- | --- |
| `TRICYCLE_UNITS_DATASET_WORKER_STATEMENT_TIMEOUT_MS` | `unset` | Compose |

## Configuration notes

TS geometry has separate MCP byte and record budgets; other JSONL/CSV exports use the general budgets. UniTS reaction-center capacities change array shapes and must be coordinated with consumers.
