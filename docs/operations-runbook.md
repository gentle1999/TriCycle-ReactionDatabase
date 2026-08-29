# 生产运维与恢复 Runbook

> English edition: [Production operations and recovery runbook](en/operations-runbook.md).

本文覆盖多主机部署的备份、恢复、监控和定时维护。所有恢复命令先在隔离环境执行；不要把
`pg_restore --clean`、bucket 删除或 lifecycle 变更直接指向生产目标。

## 1. 责任边界

| 数据 | 权威位置 | 必须备份的内容 |
| --- | --- | --- |
| 领域事实、身份映射、权限、审计 | PostgreSQL | 数据库、角色/权限、`alembic_version` |
| 原始计算文件 | RustFS/S3 | 私有 bucket、object versions、lifecycle 配置 |
| 登录身份 | OIDC provider | realm/tenant、client、用户、组、签名与加密密钥 |
| 运行 Secret | Secret manager | DB/S3/OIDC/SMTP 凭据、Session secret、CA bundle |
| 前端和应用 | 构建/制品仓库 | Git revision、Python lock、前端 lock、构建 artifact |

PostgreSQL 与对象存储没有分布式事务。每次备份生成同一个 `backup-id`，记录数据库快照时间、
对象存储版本/复制水位和应用 Git revision，恢复时按该清单配对。

## 2. PostgreSQL 备份

在备份节点使用只读备份账户和 `sslmode=verify-full`：

~~~bash
backup_id="$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "/srv/backups/reaction-database/$backup_id"
pg_dump "$TRICYCLE_DATABASE_URL" --format=custom --compress=zstd:9 \
  --file "/srv/backups/reaction-database/$backup_id/database.dump"
pg_dumpall --dbname "$TRICYCLE_DATABASE_URL" --globals-only \
  > "/srv/backups/reaction-database/$backup_id/globals.sql"
sha256sum "/srv/backups/reaction-database/$backup_id/database.dump" \
  "/srv/backups/reaction-database/$backup_id/globals.sql" \
  > "/srv/backups/reaction-database/$backup_id/SHA256SUMS"
~~~

将目录复制到独立故障域并启用保留锁。生产建议同时使用 PostgreSQL 物理备份/WAL 归档实现
PITR；逻辑备份用于可移植恢复演练，不替代连续 WAL。

## 3. RustFS/S3 备份

生产 bucket 必须启用 versioning。优先使用 RustFS/S3 原生跨节点复制到独立故障域，并在
`backup-id` 清单中记录复制水位、源/目标 bucket、versioning 和 lifecycle 状态。若平台只
提供离线快照，先冻结 lifecycle 删除，再对数据卷做一致性快照。

不能只导出对象 key：恢复需要 object bytes、metadata（尤其 `sha256`）、version ID 和删除
标记。每次备份抽样执行 authenticated `HEAD` 和 `GET`，对比 PostgreSQL `artifact_file` 的
`storage_bucket`、`storage_key`、`size_bytes` 与 `content_sha256`。

## 4. OIDC 与 Secret

按身份提供方的受支持方式导出 realm/tenant、client redirect URI、audience mapper、用户/组和
签名密钥。Keycloak 应在停止写入或按厂商一致性流程导出；只保存 realm JSON 而不保存用户和
密钥不构成可恢复备份。

Secret manager 备份必须加密并限制双人恢复权限。恢复到隔离环境后立即轮换 OIDC client、
数据库、S3 和 SMTP 凭据；不要把生产 Session secret 带入普通测试环境。

## 5. 隔离恢复演练

1. 新建隔离 PostgreSQL 和私有 bucket，使用不同 DNS、凭据和网络策略。
2. 校验 `SHA256SUMS`。当前备份包含完整 schema 便于独立审计；恢复到本项目的新环境时先运行
   `alembic upgrade head` 安装 RDKit 和当前 baseline，再从 dump 生成 data-only TOC，排除
   `alembic_version` 后运行 `pg_restore --use-list ... --exit-on-error`。不要把 dump 中的 RDKit
   extension 和项目函数覆盖到已迁移的目标库；恢复窗口需要按数据库运维策略处理外键触发器，
   恢复后必须重新启用并执行约束/行数校验。baseline 会固定 RDKit
   `mol_from_smiles(text)` 的安全 `search_path`，以支持 `pg_restore` 清空 session search path
   时重算生成 fingerprint。
3. 恢复对应 `backup-id` 的对象版本和 metadata，禁止指向生产 bucket。
4. 将隔离 API 的 `.env` 指向恢复后的 DB/bucket 与测试 OIDC/SMTP sink。
5. 在备份切点的源环境先运行一次下面的命令生成 `source-manifest.json`，再在隔离恢复环境运行
   `--expected-manifest` 进行跨环境比对。源清单必须与 PostgreSQL snapshot/WAL 水位和 RustFS
   对象复制水位属于同一个恢复点；暂停写入或记录一致的 cutover watermark，不能提前很久生成
   清单后再做备份。`tricycle-validate-restore` 默认流式读取全部
   `available` Artifact，按数据库记录的 bucket/key/`version_id` 比对 S3 HEAD、长度、metadata
   SHA-256 和实际内容 SHA-256；任何 pending/missing/corrupt、bucket 不可用、版本缺失或 hash
   不一致都会输出结构化 JSON 并以非零退出。输出中的 `artifact_manifest_digest` 对按 ID 排序的
   available Artifact 的 ID、bucket、object key、version ID、content SHA-256 和大小做摘要；恢复
   命令会同时比较 Alembic revision、各表行数、storage-status 计数、Artifact 数量和已校验字节数。
   `--max-artifacts` 只用于快速预检，不能与 `--expected-manifest` 组合，正式演练不要设置。

~~~bash
uv run alembic current
uv run alembic heads
uv run alembic check
uv run tricycle-validate-restore > source-manifest.json  # 备份前，在源环境执行
# 恢复 PostgreSQL/RustFS 后，在隔离环境执行：
uv run tricycle-validate-restore --expected-manifest source-manifest.json > restore-validation.json
curl -fsS https://restore-app.example.test/health/live
curl -fsS https://restore-app.example.test/health/ready
~~~

保存 `source-manifest.json` 和 `restore-validation.json`，并记录其 `schema_version`、
`validation_timestamp`、`artifact_manifest_digest` 和 `manifest_mismatches`。只有后一个文件的
`succeeded=true` 且 `manifest_mismatches=[]` 才能通过清单验收；这会捕获数据库与对象存储同时
丢失同一批数据的自洽恢复。另保存 OIDC 登录、项目权限、公开下载和 MCP token 撤销行为。
任何 hash 不一致或对象缺失都判定演练失败，不通过重新解析来掩盖原始数据损坏。启用 versioning 的 bucket 中，每个新上传 Artifact 必须持久化
`version_id`；缺失版本 ID 的旧数据必须在备份清单中单独迁移和验证，不能默认为 latest object。

## 6. RTO/RPO 记录

RTO/RPO 必须来自演练，不使用预估值冒充结果。每次记录：

| 字段 | 结果 |
| --- | --- |
| `backup-id` / Git revision | 待填写 |
| 故障注入时间 | 待填写 |
| 数据库恢复完成时间 | 待填写 |
| 对象恢复完成时间 | 待填写 |
| 应用验收完成时间 | 待填写 |
| 实测 RTO | 待填写 |
| 最新可恢复事务/对象时间 | 待填写 |
| 实测 RPO | 待填写 |
| 不一致项与处置 | 待填写 |

## 7. API 与定时维护

示例 API service 和维护 unit 位于 `infra/systemd/`。每台 API 主机安装并启用 API service；
只在一个 scheduler 节点启用两个 timer：

~~~bash
systemd-analyze verify infra/systemd/*.service infra/systemd/*.timer
systemctl enable --now reaction-database-api.service
systemctl list-timers 'reaction-database-*'
systemctl enable --now reaction-database-session-cleanup.timer
systemctl enable --now reaction-database-rustfs-gc.timer
~~~

执行 `systemd-analyze verify` 前，应用必须已经安装到 unit 中约定的
`/opt/reaction-database/.venv`，否则校验会按预期报告 `ExecStart` 不存在。CI 在隔离 runner
中为三个命令创建 `/bin/true` 静态替身，只验证 unit 语法和依赖；部署节点仍必须检查 API
service 的 live/ready，并手工执行两个真实 oneshot、检查 JSON 结果。

只在一个 scheduler 节点启用，避免多实例同时扫描。命令输出 JSON，应进入集中日志；失败 unit
由监控告警。更新代码或 `.env` 后先手工运行对应 oneshot，再恢复 timer。

## 8. 监控与告警

`infra/monitoring/prometheus-rules.yml` 使用应用内部 Prometheus 指标、blackbox exporter、
postgres_exporter 和 node_exporter。Prometheus 从监控网直接抓取每个 API 节点的
`/internal/metrics`；公网 Nginx 对 `/internal/` 固定返回 404，防火墙也不得允许公网访问 API
内网端口。部署时配置以下 job label：

- `reaction-database-live`：公网或边缘 `/health/live`。
- `reaction-database-ready`：每个 API 节点 `/health/ready`。
- `reaction-database-postgresql`：PostgreSQL exporter。
- `reaction-database-rustfs`：authenticated S3/RustFS synthetic probe。
- `reaction-database-oidc-discovery`：OIDC discovery HTTPS。
- `reaction-database-smtp-starttls`：SMTP 587 STARTTLS probe。

应用指标覆盖数据库池、statement timeout、上传成功/失败和 pending、storage missing/corrupt、
OIDC callback、SMTP、MCP 活跃请求及共享限流后端故障。告警阈值是初始容量预算，上线压测后
按 worker 数、SQLAlchemy pool 和 MCP 客户端规模调整；日志仍保留具体错误和请求上下文。

## 9. 多节点切换与验收记录

依赖 smoke 必须从每个 API 节点各运行一次，并在 PostgreSQL writer、RustFS 入口、Redis
writer、OIDC 和 SMTP relay 的计划切换前后重复执行：

~~~bash
uv run --frozen tricycle-deployment-smoke
~~~

每次输出是单行 JSON；任一检查失败时命令非零退出，但仍会继续检查其他依赖。该命令的 Redis
检查会创建并立即删除一个带 60 秒 TTL 的随机 key，其他检查为只读。它只验证依赖端点契约；
切换期间还必须保持一个真实 MCP 流式请求，并由浏览器验证同源 Session/CSRF、OIDC 登录和
公开 Artifact 下载。

使用 [`infra/deployment/acceptance-record.example.md`](../infra/deployment/acceptance-record.example.md)
记录切换时间、各节点 smoke 文件、告警触发/恢复、备份恢复水位以及实测 RTO/RPO。空白模板、
本地 Compose 结果或没有时间戳的口头确认都不能作为生产验收证据。

### 9.1 机器校验验收记录

生产演练完成后，将同目录的证据附件按 SHA-256 写入一个 JSON 验收记录。记录必须使用
`deployment-acceptance-v1`，包含至少两个 API 节点、六类依赖切换、六项用户流程、五项
监控触发/恢复、`upload-resource-benchmark-v2` 的 1/8/32 文件容量报告、
`upload-limit-probe-v1` 的稳定 413 预检报告、目标规模 `query-plan-evidence-v1`、
源/恢复清单和实测 RTO/RPO。
附件路径只能位于验收记录目录下；validator 会重新读取并计算每个文件的字节大小和 hash，
验证每个 `deployment-smoke-v1` 的六项检查，以及 restore manifest 的行数、Artifact 摘要和
`manifest_mismatches=[]`。`PENDING`、缺失附件、hash 漂移或未批准记录都会以非零状态结束。

~~~bash
make validate-deployment-acceptance \
  ACCEPTANCE_RECORD=/srv/reaction-database/acceptance/deployment-acceptance.json
# 等价命令：
uv run --frozen tricycle-validate-deployment-acceptance \
  /srv/reaction-database/acceptance/deployment-acceptance.json
# 生成当前版本的 JSON Schema：
uv run --frozen tricycle-validate-deployment-acceptance --print-schema \
  > /srv/reaction-database/acceptance/deployment-acceptance.schema.json
~~~

共享限流需要在两个 API 节点上交替发送请求，并单独记录 Redis 故障后的 fail-closed 结果：

~~~bash
make probe-shared-rate-limit \
  RATE_LIMIT_MODE=shared \
  API_URLS="https://api-01.example.test https://api-02.example.test" \
  ACCEPTANCE_BEARER_TOKEN="$TRICYCLE_ACCEPTANCE_BEARER_TOKEN" \
  RATE_LIMIT_OUTPUT=/srv/reaction-database/acceptance/capacity/shared-rate-limit.json

# 运维侧切换/隔离 Redis writer 后重复：
make probe-shared-rate-limit \
  RATE_LIMIT_MODE=fail-closed \
  API_URLS="https://api-01.example.test https://api-02.example.test" \
  ACCEPTANCE_BEARER_TOKEN="$TRICYCLE_ACCEPTANCE_BEARER_TOKEN" \
  RATE_LIMIT_OUTPUT=/srv/reaction-database/acceptance/capacity/rate-limit-fail-closed.json
~~~

命令只把 token 放在进程环境中，不会写入 JSON；validator 要求共享探针在每个节点看到同一
递减预算并最终 429，故障探针在每个节点都得到 503、`rate_limit_backend_unavailable`、
`Retry-After: 1` 和 `Cache-Control: no-store`。

上传超限也必须在真实 API 节点上重复验证，请求要在进入 RustFS 或解析器前稳定返回 413：

~~~bash
make probe-upload-limit \
  UPLOAD_API_URL=https://api-01.example.test \
  UPLOAD_PROJECT_ID=<authorized-project-uuid> \
  UPLOAD_MAX_BYTES=67108864 \
  ACCEPTANCE_BEARER_TOKEN="$TRICYCLE_ACCEPTANCE_BEARER_TOKEN" \
  UPLOAD_LIMIT_OUTPUT=/srv/reaction-database/acceptance/capacity/upload-limit.json
~~~

`upload-limit-probe-v1` 要求至少两次相同的超限请求均为 HTTP 413、响应头
`X-Upload-Rejection-Stage: preflight`，且错误消息包含配置的字节上限。

该校验只证明提交的生产证据满足结构和一致性契约，不会把本地 Compose、空白模板或未执行
的切换变成通过；`acceptance-record.example.md` 仍需附上每个目标环境的原始 JSON 和时间戳。
