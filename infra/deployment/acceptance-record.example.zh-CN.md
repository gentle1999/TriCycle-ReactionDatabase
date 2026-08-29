# 生产部署验收记录

[English](acceptance-record.example.md) | [简体中文](acceptance-record.example.zh-CN.md)

这是空白证据模板。必须以目标部署的实际结果替换每一个 `PENDING` 值；未填写的副本不是发布批准。

填充证据后，创建机器可读的 `deployment-acceptance-v1` JSON record，并执行：

```bash
make validate-deployment-acceptance ACCEPTANCE_RECORD=/path/to/record.json
```

validator 要求两份 API node smoke attachment、六种 dependency failover、六个 workflow check、
五组 monitoring trigger/recovery、1/8/32 capacity result、稳定的 413 upload-limit evidence、
indexed query-plan evidence、匹配的 source/restore manifest，以及实测而非占位的 RTO/RPO。Markdown
表格用于评审；JSON record 和 hash attachment 才是验收证据。

在目标 dataset 和 API node 上生成 capacity attachment：

```bash
uv run --frozen python scripts/benchmark_upload_resources.py \
  --output evidence/capacity/upload-benchmark.json
make probe-upload-limit \
  UPLOAD_API_URL=https://api-01.example.test \
  UPLOAD_PROJECT_ID=<authorized-project-uuid> \
  UPLOAD_MAX_BYTES=67108864 \
  ACCEPTANCE_BEARER_TOKEN="$TRICYCLE_ACCEPTANCE_BEARER_TOKEN" \
  UPLOAD_LIMIT_OUTPUT=evidence/capacity/upload-limit.json
make probe-shared-rate-limit \
  RATE_LIMIT_MODE=shared \
  API_URLS="https://api-01.example.test https://api-02.example.test" \
  ACCEPTANCE_BEARER_TOKEN="$TRICYCLE_ACCEPTANCE_BEARER_TOKEN" \
  RATE_LIMIT_OUTPUT=evidence/capacity/shared-rate-limit.json
make probe-shared-rate-limit \
  RATE_LIMIT_MODE=fail-closed \
  API_URLS="https://api-01.example.test https://api-02.example.test" \
  ACCEPTANCE_BEARER_TOKEN="$TRICYCLE_ACCEPTANCE_BEARER_TOKEN" \
  RATE_LIMIT_OUTPUT=evidence/capacity/rate-limit-fail-closed.json
make capture-query-plan-evidence \
  DATASET_SCALE=<immutable-snapshot-id-and-row-scale> \
  QUERY_PLAN_OUTPUT=evidence/capacity/query-plans.json
```

## 发布身份

| 字段 | 证据 |
| --- | --- |
| Deployment / change ID | PENDING |
| Git revision 和 immutable artifact digest | PENDING |
| Public origin | PENDING |
| UTC start / finish | PENDING |
| Operator / reviewer | PENDING |

## Node 与依赖 smoke

| Node | 变更前 JSON | 变更后 JSON | 结果 |
| --- | --- | --- | --- |
| API-01 | PENDING | PENDING | PENDING |
| API-02 | PENDING | PENDING | PENDING |

在每个 API node 运行 `tricycle-deployment-smoke`。附上 JSON 文件；不要粘贴 secret 或未脱敏
connection URL。即使另一 API node 健康，任何一个 failed check 都是部署失败。

## 计划内切换

| Dependency | Stable endpoint | Injection / switch | Start UTC | Recovered UTC | Client errors | Result |
| --- | --- | --- | --- | --- | --- | --- |
| PostgreSQL writer | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| RustFS/S3 entrypoint | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Redis writer | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| OIDC node | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| SMTP relay | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| EDGE/API upstream | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |

记录 logical DNS/issuer 是否保持稳定。PostgreSQL 必须附上新 endpoint 可写且具备预期 RDKit
extension 的证据。Redis 必须附上至少两个 API process 的 shared limiter result。

## 用户流程

| 检查 | 证据 | 结果 |
| --- | --- | --- |
| OIDC authorization-code + PKCE login/logout | PENDING | PENDING |
| Same-origin Session 和 CSRF state change | PENDING | PENDING |
| Project invitation sent、received 和 accepted | PENDING | PENDING |
| Artifact upload 和 exact-version download hash | PENDING | PENDING |
| MCP streaming request held through switch | PENDING | PENDING |
| Cross-project private object remains 404 | PENDING | PENDING |

## Backup 与 restore

| 字段 | 证据 |
| --- | --- |
| Backup ID / database snapshot / object replication watermarks | PENDING |
| OIDC realm、user store、signing key 和 secret backup reference | PENDING |
| Failure injection UTC | PENDING |
| Database restore complete UTC | PENDING |
| Object restore complete UTC | PENDING |
| Application acceptance complete UTC | PENDING |
| Source `source-manifest.json` 和 restore `restore-validation.json`（`schema_version`、digest、mismatch） | PENDING |
| Measured RTO | PENDING |
| Latest recoverable transaction/object UTC | PENDING |
| Measured RPO | PENDING |
| Mismatch 和 disposition | PENDING |

## Monitoring 与批准

| 检查 | Trigger evidence | Recovery evidence | 结果 |
| --- | --- | --- | --- |
| Public live/ready | PENDING | PENDING | PENDING |
| PostgreSQL / RustFS / Redis | PENDING | PENDING | PENDING |
| OIDC / SMTP | PENDING | PENDING | PENDING |
| Upload pending/failure 和 object integrity | PENDING | PENDING | PENDING |
| Maintenance unit failure | PENDING | PENDING | PENDING |

发布决定：`PENDING`

批准人和 UTC timestamp：`PENDING`
