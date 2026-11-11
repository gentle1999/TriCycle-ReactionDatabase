# 对象存储与清理环境变量

RustFS/S3 连接与垃圾回收。

[返回环境变量索引](../environment-variables.md)

## 后端设置

| 变量 | 代码默认值 | 数值或格式约束 |
| --- | --- | --- |
| `TRICYCLE_RUSTFS_PARSE_DOWNLOAD_CHUNK_SIZE` | `4194304` | ≥1024 |
| `TRICYCLE_RUSTFS_ENDPOINT_URL` | `http://127.0.0.1:19000` | — |
| `TRICYCLE_RUSTFS_VERIFY_TLS` | `true` | — |
| `TRICYCLE_RUSTFS_CA_BUNDLE` | `未设置` | — |
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

## Compose、前端与脚本变量

| 变量 | `.env.example` 示例值 | 来源 |
| --- | --- | --- |
| `RUSTFS_API_PORT` | `19000` | Compose |
| `RUSTFS_BIND_ADDRESS` | `127.0.0.1` | Compose |
| `RUSTFS_COMPRESSION_ENABLED` | `true` | Compose |
| `RUSTFS_CONSOLE_BIND_ADDRESS` | `127.0.0.1` | Compose |
| `RUSTFS_CONSOLE_PORT` | `19001` | Compose |
| `RUSTFS_DATA_DIRECTORY` | `未设置` | Compose |
| `RUSTFS_LOG_DIRECTORY` | `未设置` | Compose |

## 配置关系

独立的 RustFS 与存储垃圾回收设置类读取各自前缀的变量；`TRICYCLE_ENVIRONMENT` 也决定 RustFS 的生产 TLS 校验。
