# 安全与查询重构基线

记录日期：2026-08-17

本文件保存 `security-query-identity-remediation-plan.md` 的 F0 可复现证据。结果来自本地开发
PostgreSQL/RDKit、RustFS 和 Keycloak `start-dev`，不代表生产容量或生产灾备认证。

## 授权和查询次数

自动测试固定以下热路径 SQL 次数：

| 路径 | SQL |
| --- | ---: |
| `require_project_permission()` | 1 条 `SELECT EXISTS` |
| `project_accesses()` | 1 条数据库过滤查询 |
| 热 `authenticate_session()` | 1 条 SELECT、0 条写入 |
| 跨越 5 分钟窗口的 Session | 1 条 SELECT、1 条条件 UPDATE |
| 已 provisioning 的 Bearer identity | 1 条 SELECT、0 条写入 |
| Artifact keyset 首页/后续页 | 每页 1 条 SELECT、0 条 count |
| Artifact offset 兼容页 | 1 条 count + 1 条 SELECT |

`tests/integration/test_authorization_query_cost.py` 使用 1 个有权项目和 96 个无关项目，断言
单项目权限和访问列表仍各发 1 条 SQL，并保存 `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`；计划
必须对 `project` 和 `project_membership` 使用索引访问，直接成员路径命中
`ix_project_membership_user_id`，且不得退化为顺序扫描。查询成本测试另外固定 Session、Artifact
filename trigram 和 Artifact keyset 索引。`test_authentication_hotpath.py` 连续执行
100 次热 Session 认证，结果为 100 条 SELECT、0 条 UPDATE/INSERT 和 0 条新增 `auth.login`。

## 代表性 SQL 执行计划

`tests/integration/test_query_cost_database.py` 对 Topology SMARTS/fingerprint、Artifact filename/
project keyset、Geometry topology 和 MappedReaction SMARTS/fingerprint 执行
`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`。每条查询必须出现迁移中声明的 GiST/GIN/B-tree
索引节点、包含 PostgreSQL buffer 统计且在本地 fixture 数据库中低于 500 ms。测试通过
`enable_seqscan=off` 证明目标索引路径可执行；它不等同于生产数据分布下的 planner 选择，生产
上线前仍须对恢复后的真实统计信息运行相同语句，并审查意外全表扫描。

目标环境使用版本化证据生成器，不得只粘贴终端片段：

```bash
make capture-query-plan-evidence \
  DATASET_SCALE=<immutable-snapshot-id-and-row-scale> \
  QUERY_PLAN_OUTPUT=/srv/reaction-database/acceptance/capacity/query-plans.json
```

输出使用 `query-plan-evidence-v1`，记录数据库/PostgreSQL 版本、四张核心表行数、10 条查询的
完整 `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`、命中索引和顺序扫描例外。默认只允许估算行数
不超过 10000 的表作为显式 sequential-scan capacity exception；更大的未预期顺序扫描使命令
非零退出。验收记录会核对 `dataset_scale`、10 个查询标签、每项 `accepted=true` 和空的
`unexpected_sequential_scans`。本地 fixture 结果不能替代恢复后目标规模结果。

2026-08-19 在本地开发 PostgreSQL/RDKit 数据库采集结果如下；时间只用于发现本地回归，不是
生产 SLO。`hits`/`reads` 是整棵计划树的 shared buffer 节点值之和。

| 查询 | 执行时间 | hits/reads | 命中索引 |
| --- | ---: | ---: | --- |
| Topology SMARTS | 2.222 ms | 23/0 | `ix_molecular_topology_mol_gist` |
| Topology fingerprint KNN | 0.068 ms | 56/0 | `ix_molecular_topology_morgan_bfp_gist` |
| Artifact filename contains | 0.309 ms | 15/0 | `ix_artifact_file_original_filename_trgm` |
| Artifact project keyset | 0.077 ms | 27/0 | `ix_artifact_file_project_status_created_id` |
| Geometry topology | 0.025 ms | 3/0 | `ix_geometry_topology_id` |
| MappedReaction SMARTS | 0.151 ms | 8/0 | `ix_mapped_reaction_reaction_gist` |
| MappedReaction fingerprint KNN | 0.140 ms | 26/0 | `ix_mapped_reaction_structural_bfp_gist` |

## 表示授权矩阵

`test_depiction_authorization_matrix_covers_all_roles_and_formats` 对 Geometry SVG/SDF、Topology
SVG/MOL 和 TS negative/center/positive SDF 使用同一数据集验证匿名、项目外、Viewer、
Contributor、Manager。Viewer/Contributor/Manager 的项目内响应均为 200 且
`Cache-Control: private, no-store`；匿名、项目外和不存在 UUID 均为 404。Nginx 示例和静态
契约测试同时要求 `/api/*` 关闭 shared cache。

## 上传资源

运行命令：

```bash
uv run --frozen python scripts/benchmark_upload_resources.py \
  --fixture /srv/reaction-database/real-data/gaussian-orca \
  --output /srv/reaction-database/acceptance/capacity/upload-benchmark.json
```

输出使用 `upload-resource-benchmark-v2`，包含节点、UTC 时间、输入 fixture 路径/hash、`n_jobs`
和 1/8/32 批次结果。每个结果记录输入准备、MolOP 解析和总耗时的毫秒分布；部署验收
validator 会复核三个批次均为零失败、`n_jobs` 与容量配置一致，以及每个阶段均为非负数。

生产验收还必须运行 `make probe-upload-limit` 生成 `upload-limit-probe-v1`。该 HTTPS 探针发送
至少两次大于 `TRICYCLE_MAX_UPLOAD_BYTES` 的请求，并记录稳定 HTTP 413、
`X-Upload-Rejection-Stage: preflight` 和包含配置上限的错误消息，证明请求未进入 RustFS 或解析器。

固定输入必须是部署方提供的真实 Gaussian/ORCA 文件或目录，`n_jobs=2`。每个批次在独立父
进程运行，监测 `/proc` 中整个进程树；峰值子进程包含 loky worker 和辅助进程。

本地仓库不提供合成吞吐基线；部署验收必须用上面指定的真实文件重新生成 1/8/32
结果，并将报告作为验收附件。

资源边界由 service 入口在授权、RustFS 和解析前检查文件数、单文件和累计字节；解析 semaphore
测试证明解析进程池按配置的 worker 数限制并发。数据库/RustFS 集成测试覆盖逐文件
失败保留、pending reservation 删除、对象写入失败即时补偿和 stale object GC。原始计算文件
内容不写入日志。

## Redis 共享限流

`tests/integration/test_rate_limit_redis.py` 连接真实 Redis 7.0.15，让两个独立
`RedisFixedWindowRateLimiter` 实例使用同一个 key prefix 和 subject。第一次/第二次请求分别
剩余 1/0，第三次被拒绝，Redis 中原子计数为 3；一秒 TTL 到期后额度恢复。另一个测试连接
不可用端口，验证请求 fail-closed 为 `RateLimitBackendUnavailable`。客户端显式使用 2 秒连接/
命令 timeout 和零重试，避免共享后端故障时在 API worker 内累计长时间重试。

初始本地测试使用明文 loopback Redis，只证明共享原子语义和故障行为。后续增量测试使用真实
Redis 6.0.16、临时本地 CA、含 loopback IP SAN 的服务器证书和仅 TLS 端口，通过
`rediss://...?ssl_ca_certs=<absolute-pem>` 得到 `2 passed`，验证证书链、Lua 原子窗口和 TTL。
生产仍必须使用实际认证、私有 CA，并在至少两个 API 进程上完成多节点 endpoint 切换演练。

## Keycloak

Compose 固定 Keycloak 26.3.2 digest。本次从停止状态执行 `docker compose up -d --wait
keycloak`，包含容器 recreate 到 health check 通过约 27 秒；稳定后观察到约 670.4 MiB 容器
内存。开发 realm 的 password grant token endpoint 为 HTTP 200 / 0.792s，对相同 refresh token
调用 logout endpoint 为 HTTP 204 / 0.046s。

这是 loopback、H2、`start-dev` 的协议端点基线，不是浏览器 authorization-code 交互耗时，
也不是生产登录容量承诺。隔离 realm export/restore 证据仍以
`identity-provider-decision.md` 为准；测量过程没有输出 token、client secret 或 claims。
