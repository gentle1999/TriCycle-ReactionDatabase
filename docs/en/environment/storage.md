# Object storage and cleanup environment variables

RustFS/S3 connections and garbage collection.

[Back to the environment-variable index](../environment-variables.md)

## Backend settings

| Variable | Code default | Value constraints |
| --- | --- | --- |
| `TRICYCLE_RUSTFS_PARSE_DOWNLOAD_CHUNK_SIZE` | `4194304` | ≥1024 |
| `TRICYCLE_RUSTFS_ENDPOINT_URL` | `http://127.0.0.1:19000` | — |
| `TRICYCLE_RUSTFS_VERIFY_TLS` | `true` | — |
| `TRICYCLE_RUSTFS_CA_BUNDLE` | `unset` | — |
| `TRICYCLE_RUSTFS_ACCESS_KEY` | `example-local-access` | — |
| `TRICYCLE_RUSTFS_SECRET_KEY` | `example-local-secret` | — |
| `TRICYCLE_RUSTFS_BUCKET` | `example-reaction-raw-files` | — |
| `TRICYCLE_RUSTFS_REGION` | `us-east-1` | — |
| `TRICYCLE_RUSTFS_CONNECT_TIMEOUT_SECONDS` | `5` | ≥1, ≤60 |
| `TRICYCLE_RUSTFS_READ_TIMEOUT_SECONDS` | `30` | ≥1, ≤300 |
| `TRICYCLE_RUSTFS_MAX_POOL_CONNECTIONS` | `32` | ≥1, ≤256 |
| `TRICYCLE_STORAGE_GC_ROOT_PREFIX` | `uploads` | — |
| `TRICYCLE_STORAGE_GC_GRACE_PERIOD_SECONDS` | `3600` | ≥60 |
| `TRICYCLE_STORAGE_GC_INITIAL_LOOKBACK_SECONDS` | `86400` | ≥60 |
| `TRICYCLE_STORAGE_GC_PARTITION_CLOCK_SKEW_SECONDS` | `3600` | ≥0, ≤86400 |

## Compose, frontend, and script variables

| Variable | `.env.example` example | Source |
| --- | --- | --- |
| `RUSTFS_API_PORT` | `19000` | Compose |
| `RUSTFS_BIND_ADDRESS` | `127.0.0.1` | Compose |
| `RUSTFS_COMPRESSION_ENABLED` | `true` | Compose |
| `RUSTFS_CONSOLE_BIND_ADDRESS` | `127.0.0.1` | Compose |
| `RUSTFS_CONSOLE_PORT` | `19001` | Compose |
| `RUSTFS_DATA_DIRECTORY` | `unset` | Compose |
| `RUSTFS_LOG_DIRECTORY` | `unset` | Compose |

## Configuration notes

Separate RustFS and storage-GC settings classes read their own prefixes; `TRICYCLE_ENVIRONMENT` also controls production RustFS TLS checks.
