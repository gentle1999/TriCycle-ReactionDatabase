# 项目重构与上线计划

> English edition: [Refactor and release plan](en/refactor-plan.md). This is a dated plan and acceptance record.

> 状态：已固化，执行状态以本文的阶段表和验收记录为准
>
> 建立日期：2026-08-19
>
> 适用范围：后端 API、查询授权、OIDC/Session/MCP、前端 E2E、生产配置、反向代理、
> PostgreSQL/RDKit、RustFS、CI、备份恢复和 Alembic baseline。

本文把第三方评审结论转为可执行的重构顺序。它不替代领域架构、部署变量或前端专项文档：

- 领域边界和不可变科学事实以[技术方案与实施路线图](technical-roadmap.md)为准。
- 具体查询、安全专项以[安全、查询与身份服务重构修复计划](security-query-identity-remediation-plan.md)为准。
- 环境变量、OIDC、SMTP 和部署拓扑以[部署与配置指南](deployment-configuration.md)为准。
- 前端路由和组件计划以[前端重构计划](frontend-refactor-plan.md)为准。
- Alembic 只有 `0001_initial_schema` 一个主线 head；开发期往返迁移不进入主线。

## 1. 当前判断

代码和隔离基础设施验收已达到预生产门槛，但在目标生产拓扑完成外部依赖演练前，不应直接作为公网多租户生产系统。当前剩余风险是：

| 优先级 | 当前风险 | 目标 |
| --- | --- | --- |
| P0 | visibility scope、REST/GraphQL/MCP/depiction 授权矩阵已实现并有 integration 回归；真实公网缓存绕过仍需目标代理演练 | 所有传输面共享 artifact-rooted visibility；跨项目对象不可枚举 |
| P1 | Caddy 路径、MCP 流式代理和内部 metrics 已通过本地真实运行探针；真实公网 TLS 入口仍需部署验证 | 生产入口与应用实际路由一致，MCP 可稳定长连接/流式返回 |
| P1 | 生产安全配置和 Redis 共享限流已在代码中 fail-closed，但真实 PostgreSQL/RustFS/Redis TLS、SMTP STARTTLS 和多节点切换尚未部署验证 | 在目标拓扑验证 TLS、共享额度、容量和故障切换 |
| P1 | 代码、CI、前端 fixture 和运维 unit 已形成发布门；真实生产恢复、告警触发和 RTO/RPO 仍缺记录 | 每次发布有可重复的代码、数据库、前端和运维证据 |
| P2 | OIDC provision、`create_reaction` 全局 curator 和 Cookie CSRF 策略已书面化并有回归测试；真实 OIDC authorization-code 仍需部署验证 | 认证、provision、写入和会话安全策略明确且有回归测试 |

已有能力不重复建设：OIDC authorization-code + PKCE、JWT issuer/audience/JWKS 校验、
HttpOnly Session Cookie、MCP token 一次性展示/撤销、统一查询服务、RustFS pending/GC
生命周期和生产配置基础校验均已存在，但仍需按本文完成边界验证。

## 2. 不可违反的原则

1. **授权先于表示。** 详情、列表、反向查询、下载、SVG/MOL 和 MCP 必须使用同一 visibility scope；无权与不存在对象统一为 404。
2. **科学事实不可变。** 不重写既有 Artifact、ParseRevision、Frame、Geometry、Reaction；重解析只创建新 revision。
3. **迁移可审计。** 所有 schema 变化通过 Alembic；纯 DTO、查询或配置变更不创建迁移。
4. **生产 fail-closed。** 生产不接受开发认证、默认 secret、localhost CORS、非 HTTPS 外部服务或无限解析并发。
5. **先独立验证再切换。** 不把身份提供方替换、数据库清理和生产发布绑定在同一次不可回滚操作中。

## 3. 阶段计划

### R0：冻结基线、授权矩阵和清理边界

**目标：** 在修改前固定事实、数据范围和决策，避免把测试库状态误当成生产能力。

**修改范围：**

- 在测试数据中建立公开 Artifact、两个互不授权的私有项目、共享 Topology 和项目独有 Topology。
- 固化 REST、GraphQL、MCP、depiction、download、反向查询和 `GET /api/topologies` 的授权矩阵。
- 记录当前命令的通过/失败/跳过/环境阻塞，不把跳过视为通过。
- 盘点数据库与 RustFS 测试数据；确认旧数据全部为测试数据后，开发环境允许重建，不操作生产数据。
- 记录 baseline 迁移前后的表清单、bootstrap 用户/组织/项目是否保留，以及清库审批记录。

**验收命令：**

```bash
git status --short
uv run alembic current
uv run alembic heads
uv run alembic check
make test
make test-infra
make lint
make type
make frontend-check
make frontend-build
make frontend-test-e2e
```

**通过标准：** 产生一份带日期的基线记录；每个失败都有 issue/阶段归属；测试 fixture 不含生产 secret；清理范围明确为开发数据库和 RustFS bucket。

**阻塞条件：** 无法区分测试数据和生产数据、无法恢复数据库/RustFS、或发现存在第二个 Alembic head。

**产物：** `docs/security-query-baseline.md` 更新记录、授权矩阵 fixture、清理清单和发布 issue。

### R1：修复 Topology 及所有 transport 的 visibility contract（P0）

**目标：** 消除跨项目拓扑结构摘要和表示泄漏。

**修改范围：**

- `src/tricycle_reaction_db/api/core.py` 的 Topology 列表改为调用共享查询 service/scope，不允许直接全库 `select(MolecularTopology)`。
- 审计 `session.get(MolecularTopology, ...)`、Topology detail、SVG/MOL、Geometry/Frame 反向查询、ScientificArray 下载和 MCP 入口。
- 公开对象与项目对象分别验证；私有表示响应使用 `Cache-Control: private, no-store`，代理层默认 bypass cache。
- 不在 REST、GraphQL、MCP 各自复制授权 SQL。

**验收命令：**

```bash
uv run pytest -q tests -k 'topolog or visibility or depiction or authorization'
TRICYCLE_RUN_DATABASE_TESTS=1 uv run pytest -q tests/integration -k 'topolog or visibility or depiction or authorization'
```

**通过标准：** A 项目用户无法通过列表、详情、UUID、反向查询、下载或 MCP 读取 B 项目对象；无权/不存在均为 404；REST/GraphQL/MCP 返回集合一致；新增回归测试覆盖 `GET /api/topologies`。

**阻塞条件：** 任何读取入口仍可绕过 scope，或共享缓存能复现跨用户响应。

**产物：** `tests/integration/` 授权回归、OpenAPI/GraphQL/MCP schema 快照和代理缓存测试记录。

### R2：统一 OIDC、Session、MCP、CSRF 与用户 provision 策略

**目标：** 将认证身份、授权主体和写入权限明确分层。

**待决策（必须由项目负责人确认并写入测试）：**

- 有效 OIDC Bearer 首次出现是否允许自动创建本地用户。默认建议：允许建立最小 inactive/pending 用户，但未被组织/项目授予权限前不得读取或写入私有数据；若保留当前 active provision，必须增加审计和管理员可见性。
- `create_reaction` 是全局 curator 操作还是项目级操作。默认建议：要求目标 topology 所属项目的 `reaction:write`/curator 权限，禁止仅凭登录创建全局数据；若维持全局操作，必须限制为 system curator 并写明原因。
- Cookie Session 是否继续只依赖 SameSite。默认建议：所有改变状态的 Cookie 请求增加 CSRF token（double-submit 或服务端 token），Bearer/MCP 请求不需要 CSRF。

**修改范围：**

- 保留 Keycloak 决策；OIDC client secret、JWKS、issuer/audience、回调 URI 和用户邮箱映射走部署文档。
- 区分 login/provision/audit 事件；普通 Bearer 请求不得重复写 `auth.login`。
- MCP token 仅允许 `/mcp/`，列表只返回 metadata，原文只展示一次；撤销立即生效。
- 对项目管理、上传、删除 Artifact、邀请和 `create_reaction` 建立权限矩阵。

**验收命令：**

```bash
uv run pytest -q tests -k 'auth or session or mcp or csrf or reaction_command'
TRICYCLE_RUN_DATABASE_TESTS=1 uv run pytest -q tests/integration -k 'auth or session or mcp or reaction_command'
```

**通过标准：** 新用户、已 provision 用户、未授权项目用户、项目角色和 system curator 的行为可被测试区分；会话热路径无逐请求写放大；MCP token 不能访问 REST/GraphQL；状态变更请求具备 CSRF 防护或有可审计的等价证明。

**阻塞条件：** 无法明确全局写入的 owner、OIDC 首次登录策略或 Cookie 状态变更保护。

**产物：** `docs/identity-provider-decision.md` 补充决策、认证/授权矩阵和回归测试。

### R3：生产配置 fail-closed 与 Caddy/MCP/health 代理

**目标：** 让生产配置错误在启动或部署检查阶段失败，而不是上线后暴露。

**修改范围：**

- 生产强制精确 HTTPS CORS origin；拒绝 localhost 默认值。
- PostgreSQL 连接启用并验证 SSL（托管服务 CA/`sslmode=verify-full` 或等价 psycopg 参数）。
- RustFS/S3 强制 HTTPS 和 TLS 校验；私有 bucket，不暴露 Console/API。
- SMTP 生产强制 STARTTLS，验证发件人域名；对仅支持 465 的服务商明确不兼容或增加 SMTP_SSL 适配器。
- Caddy 明确代理 `/health/live`、`/health/ready`、`/redoc`、`/docs/oauth2-redirect`、`/openapi.json`、`/graphql*` 和 `/nexusx/*`；MCP 关闭缓冲、设置长超时并保留 HTTP/1.1 长连接。
- 代理 body 限制不小于应用批次上限；API 默认 `private, no-store`，SPA history fallback 不得接管健康检查。

**验收命令：**

```bash
uv run pytest -q tests -k 'config or proxy or health or mcp'
caddy validate --config "$PWD/infra/caddy/Caddyfile" --adapter caddyfile
python scripts/validate_caddy_runtime.py
promtool check rules infra/monitoring/prometheus-rules.yml
curl -fsS http://127.0.0.1:8000/health/live
curl -fsS http://127.0.0.1:8000/health/ready
```

**通过标准：** production 配置缺少任一安全项时启动失败；Caddy 每个路径返回真实上游状态而非 index.html；MCP 流式响应不被缓冲或短超时截断；真实 HTTPS OIDC callback、SMTP 邀请和 RustFS HEAD/GET 验证通过。

**阻塞条件：** 上游托管 PostgreSQL/S3/SMTP 无法提供 TLS 验证、OIDC issuer 不支持 PKCE，或反向代理无法保留同源 Cookie。

**产物：** `docs/deployment-configuration.md` 的生产检查表、Caddy 配置测试和部署 smoke log。

### R4：查询性能、限流、上传资源和多 worker 容量

**目标：** 证明在目标数据规模下不会因无限查询、上传或进程内限流导致失控。

**修改范围：**

- 继续使用数据库 statement timeout、GraphQL 深度/复杂度、结构候选集和分页上限。
- 多 worker 使用 Redis 共享固定窗口限流；HTTP 与 MCP 共用 Lua 原子 `INCR + EXPIRE` 预算，生产只接受 `rediss://`，后端故障返回 503。
- 按 `Uvicorn worker 数 × n_jobs` 计算 MolOP 解析池的 CPU/内存；并发只由阶段进程池控制。
  压缩输入同时限制解压后大小；上传采用受控 spool，避免整批 bytes 常驻内存。
- 为 Topology、Artifact、Geometry 和 reaction 搜索保存代表性 `EXPLAIN (ANALYZE, BUFFERS)`。

**验收命令：**

```bash
make benchmark-upload-resources
TRICYCLE_RUN_DATABASE_TESTS=1 uv run pytest -q tests/integration -k 'query_cost or pagination or upload'
make test-redis
```

**通过标准：** 1/8/32 文件批次的峰值内存、进程数、失败补偿和延迟有记录；超限请求在进入 RustFS/解析前返回稳定 413；多 worker 限流按全机预算生效；代表性查询无意外全表扫描或有明确容量例外。

**阻塞条件：** 只能依赖进程内限流、无法测量解压后资源、或目标数据规模没有容量预算。

**产物：** `docs/security-query-baseline.md` 资源与 SQL 基线、容量报告、限流配置。

### R5：前端 E2E、fixture 和 CI 发布门

**目标：** 让前后端契约和基础设施测试在干净环境可重复运行。

**修改范围：**

- 将 DA-bench/最小化 fixture 的获取、SHA-256、版本和准备步骤写入 CI；不依赖本机 `.tmp` 或未声明数据。
- 修复 E2E 失败的依赖数据问题，区分真实产品 bug 与 fixture/环境阻塞。
- CI 分离 lint、type、默认 pytest、PostgreSQL/RDKit integration、RustFS、frontend build、Playwright、Alembic 和 audit job。
- 统一上传、登录、无权项目、详情深链、MCP token 和公开 Artifact 的 smoke tests。

**验收命令：**

```bash
make check
make test-infra
make frontend-test-e2e
uv run alembic check
make audit
```

**通过标准：** 干净 checkout 可完成 fixture 准备和全量门禁；CI 报告明确通过/失败/跳过/外部阻塞；Playwright 不因缺少数据而失败；任何合并请求不能绕过 P0 授权、Alembic 或安全审计 job。

**阻塞条件：** 依赖未声明的本地文件、不可下载的 fixture、或 CI 与本地使用不同的迁移/配置路径。

**产物：** `.github/workflows/` 工作流、fixture manifest、Playwright 报告和 CI artifact。

### R6：备份恢复、监控、GC/session cleanup 与上线演练

**目标：** 证明系统可恢复、可观测、可维护，而不只是请求成功。

**修改范围：**

- PostgreSQL（含 Alembic version）、RustFS/S3 对象/version/lifecycle、OIDC realm/client/签名密钥和生产 secrets 分别备份。
- 配置 `tricycle-auth-session-cleanup`、`tricycle-rustfs-gc`，不在多 worker API 内启动常驻 GC。
- 监控 live/ready、数据库连接池、statement timeout、上传失败/pending、RustFS missing/corrupt、OIDC callback、SMTP failed 和 MCP 长连接。
- 做一次删除、备份、恢复、对象校验、登录和 artifact 下载的完整演练。

**验收命令：**

```bash
curl -fsS https://<app-host>/health/live
curl -fsS https://<app-host>/health/ready
uv run alembic current
uv run alembic check
uv run tricycle-validate-restore > source-manifest.json  # 备份切点的源环境
# 在隔离恢复环境：
uv run tricycle-validate-restore --expected-manifest source-manifest.json > restore-validation.json
uv run tricycle-auth-session-cleanup
uv run tricycle-rustfs-gc
```

**通过标准：** 在备份切点保存源清单，并在隔离环境从备份恢复 PostgreSQL 和 RustFS 后使用
`--expected-manifest`；输出中的 `succeeded=true`、`manifest_mismatches=[]`、Artifact
清单摘要、Alembic revision、各表行数、storage-status 计数和已校验字节数必须一致。OIDC 登录、
公开下载和告警触发/恢复也必须通过，RTO/RPO 有书面结果。

**阻塞条件：** 只有数据库备份而无对象备份、恢复后 hash 不一致、或无法撤销泄露的 OIDC/MCP/SMTP secret。

**产物：** 备份清单、恢复记录、监控/告警规则、RTO/RPO 和上线 runbook。

### R7：Alembic baseline、bootstrap 与数据清理收尾

**目标：** 让主线只保留当前可部署 schema，并把旧测试数据与开发 bootstrap 的处理说清楚。

**修改范围：**

- 继续保留 `migrations/versions/0001_initial_schema.py` 作为唯一 head；开发期迁移不 squash 进主线。
- 在全新空库执行 `uv run alembic upgrade head`，核对表、索引、RDKit extension 和 `alembic_version`。
- 不手工删除或改写 `alembic_version`，不把旧 revision 文件移动后宣称完成 baseline；baseline 的可信状态必须来自全新空库升级和 `alembic heads/current/check` 三项结果。
- 旧开发数据库和 RustFS 测试 bucket 在确认无生产数据、完成备份后清空重建；不执行生产库清空命令。
- 确认 baseline 自动创建的 system/development bootstrap 用户、组织和项目：保留则改为明确的 development-only 标识，生产通过受控 OIDC 首次登录和管理员流程 provision；删除则提供独立 bootstrap/seed 步骤。
- 对 baseline 的 schema-only 变更运行 `alembic check`；任何新字段/索引单独新增可审计 revision。

**验收命令：**

```bash
uv run alembic upgrade head
uv run alembic current
uv run alembic heads
uv run alembic check
TRICYCLE_RUN_DATABASE_TESTS=1 uv run pytest -q tests/integration
```

**通过标准：** 新环境可按部署文档重建；只有一个 `0001_initial_schema (head)`；无开发迁移文件进入主线；清库后重新 seed/上传得到预期数据；生产 bootstrap 不会自动获得超出策略的全局权限。

**阻塞条件：** baseline 与 ORM 漂移、需要依赖旧 revision 才能升级、或无法证明数据库和 RustFS 清理对象仅为测试数据。

**产物：** `migrations/README.md`、baseline 验收日志、清理确认单、生产 bootstrap runbook。

## 4. 发布门禁与状态规则

阶段只有同时满足“代码、测试、文档、运行记录”四项才可标记 `done`。建议状态：

| 状态 | 含义 |
| --- | --- |
| `todo` | 尚未开始 |
| `in_progress` | 有实现但验收不完整 |
| `blocked` | 有明确外部阻塞，已记录替代方案 |
| `done` | 阶段验收全部通过 |

不得用以下结果替代发布门禁：只通过默认 pytest、只通过前端 build、只通过 `/health/live`、只通过 `alembic check`、或把 skipped integration 当成通过。

### 发布前最小命令集

```bash
make lint
make type
make test
make test-infra
make test-redis
make frontend-check
make frontend-build
make frontend-test-e2e
make audit
uv run alembic check
```

### 评审决策记录

R2 已采用以下策略，并由认证、授权和 reaction command 回归测试固定：

1. OIDC authorization-code 或 Bearer 首次出现时，按 `issuer + subject` 幂等创建 active 本地
   用户并写 `auth.provision` 审计；新用户没有组织/项目 membership，因此在管理员或邀请流程
   授权前不能读取或写入任何私有项目数据。
2. `create_reaction` 保持全局 curated reaction 操作，只允许 system organization 的
   owner/admin（system curator）调用，并写 `reaction.curated` 审计；普通项目 manager 不具备
   该全局写入权限。
3. 所有使用 Cookie Session 的 `POST`/`PUT`/`PATCH`/`DELETE` 要求双提交 CSRF token；token
   由 opaque Session 派生并同时出现在 CSRF Cookie 和请求 header。Bearer/MCP 请求不走 Cookie
   CSRF；分域部署必须同时满足精确 HTTPS CORS、credentials 和 Secure Cookie 约束。

## 5. 初始验收快照

2026-08-19 第三方评审快照，作为 R0 起点，不代表发布通过：

- `make test`：230 passed，107 skipped。
- `make test-infra`：94 passed，4 failed，1 skipped，9 errors。
- `make lint`：Ruff check 通过，format check 仍有 2 个文件失败。
- `make type`：mypy 5 个错误。
- 前端 build 通过；Playwright 28 passed、17 failed，失败主要与 E2E 依赖数据缺失有关。
- Alembic：`0001_initial_schema (head)`；`alembic check` 通过，但有 RDKit custom type/computed default 警告。

下一次更新本文时，必须按阶段写入日期、命令、通过/失败/跳过数量、阻塞原因和产物路径；不得只写“已修复”。

## 6. 2026-08-19 验收记录

以下结果来自隔离数据库 `reaction_db_integration_validation_20260819`、
`reaction_db_e2e_refactor_20260819`、`reaction_db_e2e_restore_20260819` 和专用 RustFS
bucket `reaction-db-integration-validation-20260819`，以及本轮从 baseline 新建的
`tricycle_integration_20260819_r6` / `tricycle-integration-20260819-r6`；没有清理或写入默认
`tricycle`
数据库和开发 raw-files bucket。名称覆盖验证使用独立环境/构建变量，不改变稳定包名、
路由、GraphQL 类型或表名。

本轮增量复核新增 `tricycle-deployment-smoke`：从每个 API 节点检查公网 live/ready、OIDC
discovery/JWKS/PKCE、PostgreSQL TLS/writer/RDKit、RustFS bucket versioning、Redis TLS/Lua
临时写入和 SMTP STARTTLS。OIDC、RustFS、SMTP 的私有 CA 通过绝对 PEM 配置并启用证书/主机名
校验；smoke 结果只有在目标环境执行后才可作为部署证据。

| 阶段 | 状态 | 本次证据 | 未完成/边界 |
| --- | --- | --- | --- |
| R0 | `done` | `git status --short`；隔离 bootstrap；`uv run --frozen pytest -q tests/integration` 中授权 fixture 与清理边界通过 | 生产数据分类和审批仍需部署方在上线单中确认 |
| R1 | `done` | Topology 列表改用 visibility-scoped 分页服务；完整 integration `113 passed`，覆盖 REST/GraphQL/MCP/depiction 授权 | 真实公网共享缓存绕过仍需在目标代理演练 |
| R2 | `done` | `uv run --frozen pytest -q`：`269 passed, 112 skipped`；integration：`113 passed`；OIDC provision、system-curator `create_reaction`、Session CSRF 和 MCP 仅 `/mcp/` 的决策见 `docs/identity-provider-decision.md` 与 `docs/security-query-identity-remediation-plan.md` | 真实 OIDC authorization-code、邀请邮件和跨域 Cookie 仍需生产身份服务验证 |
| R3 | `in_progress` | config/reverse-proxy focused tests通过；Caddy 2.10 `validate` 通过；运行探针通过 17 条代理/重写路径、HTTPS 跳转、`/internal` 404、API 缓存边界和 MCP 首块直出；Prometheus 规则由 promtool 2.45 检查通过；部署显示名、Cookie/header 名可由环境变量覆盖，多节点 DB/S3/Redis/OIDC/SMTP 通过稳定逻辑 endpoint 接入的模板已由 production Settings 实际解析；API 与 scheduler 使用的 RustFSSettings 均在 production 拒绝 HTTP/关闭 TLS（scheduler fail-closed 单测覆盖 HTTP endpoint 和 `verify_tls=false` 两种路径）；新增 `tricycle-deployment-smoke`，SMTP 强制验证 TLS context/主机名，OIDC discovery/token/JWKS 支持私有 CA | 真实 HTTPS OIDC authorization-code、SMTP 邀请、PostgreSQL/RustFS/Redis 私有 CA、多节点切换和公网 edge 仍需部署演练 |
| R4 | `in_progress` | `make benchmark-upload-resources` 仍保留本地快速测量；`upload-resource-benchmark-v2` 输出节点、UTC、fixture SHA-256、1/8/32 结果及输入准备/MolOP 解析/总耗时分布，validator 复核每个阶段非负；新增 `upload-limit-probe-v1` 记录重复超限请求在解析/RustFS 前稳定返回 413，新增 `query-plan-evidence-v1` 生成器保存目标数据库版本、核心表行数、10 条完整 `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`、命中索引和顺序扫描例外；新增双 API 节点 `shared-rate-limit-v1` 与 Redis 故障 `rate-limit-fail-closed-v1` HTTPS 探针；validator 重新计算观察值、计划树、行数和响应契约；真实 Redis 7.0 明文 loopback 及本轮 Redis 6.0.16 + 临时 CA/SAN 的 `rediss://` 共享 limiter 均为 `2 passed`，HTTP/MCP 共用 Lua 原子固定窗口，生产要求 TLS、零客户端重试且故障 fail-closed | 当前 PostgreSQL 容器凭据与 `.env.example` 不一致，query-plan capture 未能在默认环境运行；真实多 API 节点 Redis/TLS 故障切换、上传超限探针和目标规模 planner 结果仍需部署环境证据 |
| R5 | `done` | 默认前端 typecheck/build 通过；自定义应用/品牌/MCP 名称、HTML title 和 CSRF Cookie/header 的生产构建通过；DA seed integration `1 passed`；隔离 API `18001` + 干净 DA fixture 的完整 Playwright `45 passed`；独立 validator 在 seed 前校验 manifest v3、4 个日志的压缩/解压 hash 与大小，以及 3 个 metadata JSON 的 hash 与大小；CI 已分离默认测试、PostgreSQL/RDKit、RustFS、frontend build、Playwright、Alembic、Redis、operations config 和 audit job | 远端 GitHub Actions 首次执行报告仍需随 pull request 留存；不能退回默认空库运行 E2E |
| R6 | `in_progress` | 隔离恢复后领域记录和 4/4 Artifact 对象校验一致；新增只读 `tricycle-validate-restore` 全量校验表计数与精确 S3 `VersionId`/长度/metadata/content SHA-256，支持备份切点 `source-manifest.json` 与恢复侧 `--expected-manifest` 的确定性 Artifact 清单摘要/行数比对，versioned RustFS 真实契约 `2 passed`；恢复清单单测 `5 passed`，部分校验明确返回 `succeeded=false`；`/internal/metrics` 指标齐全，公网 Caddy 禁止 `/internal/`；21 条告警经 promtool 触发/恢复测试通过，三个 service 和两个 timer 隔离校验通过；新增 `deployment-acceptance-v1` JSON Schema/validator，校验两节点 smoke、六类切换、六项用户流程、五类告警、OIDC/secret/backup receipt、source/restore manifests 和由时间戳推导的 RTO/RPO | 本地恢复未测量生产 RTO/RPO；真实 OIDC realm/签名密钥、SMTP、告警触发恢复和生产备份对象版本恢复仍须部署演练；空白 Markdown 模板和本地 Compose 结果不能通过 validator |
| R7 | `done` | `tricycle_integration_20260819_r6` 从空库执行 `uv run --frozen alembic upgrade head`；`alembic current`/`heads` 均为 `0001_initial_schema (head)`；当前开发库 `alembic check`：`No new upgrade operations detected`；bootstrap 双跑幂等 | `alembic check` 仍有 RDKit custom type/computed default 警告，但没有漂移操作 |

### 全树门禁

2026-08-19 的可重复命令和结果：

```text
uv run --frozen ruff check .                         PASS
uv run --frozen ruff format --check .                PASS (194 files)
uv run --frozen mypy src scripts                     PASS (116 source files)
uv run --frozen pyright src scripts                  PASS (0 errors, 0 warnings)
uv run --frozen pytest -q                            PASS (286 passed, 112 skipped)
uv run --frozen pytest -q tests/integration           PASS (113 passed, 515 warnings)
TRICYCLE_RUN_REDIS_TESTS=1 ... pytest -m redis        PASS (2 passed)
npm --prefix frontend run typecheck                  PASS
npm --prefix frontend run build                      PASS
npm ... VITE_APP_NAME/VITE_CSRF_* build              PASS (deployment values emitted)
npm --prefix frontend audit --audit-level=high       PASS (0 vulnerabilities)
uvx --from pip-audit==2.10.1 pip-audit ...            PASS (no known vulnerabilities)
python scripts/audit_vendored_assets.py              PASS (4 assets)
uv lock --check                                      PASS
docker compose config --quiet                       PASS
Caddy 2.10 syntax + runtime proxy probe              PASS (17 paths + HTTPS redirect + MCP streaming)
promtool 2.45 check/test rules                       PASS (21 rules)
systemd-analyze verify infra/systemd/*                PASS (3 services + 2 timers)
```

`frontend/e2e` 已移除固定数据库 ID、外部分页数据和 source-less 电荷 fixture 假设；在上述
隔离 DA fixture 上为 `45 passed`。TS seed 会在 elementary edge 建立后刷新完整活化/反应
热力学，并持久化正负两个 signed-mode endpoint。外部部署仍必须补跑真实 OIDC/SMTP/TLS
smoke、生产对象版本恢复、告警触发以及 RTO/RPO 演练。

本轮增量（同日当前工作树）重新执行默认 pytest 得到 `286 passed, 112 skipped`，数据库/RustFS
integration 得到 `111 passed, 2 skipped, 277 deselected`，前端 check/build、Alembic check、
Ruff、Mypy、Pyright 和 uv lock check 均通过。Redis 7 image 从本机镜像代理拉取时返回 Docker
Hub 500，随后改用 apt 包临时解包的 Redis 6.0.16，以本地 CA 和含 `127.0.0.1` SAN 的短期证书
启动仅 TLS 端口，`rediss://` 集成得到 `2 passed, 396 deselected`；测试后服务、证书与解包目录
均已删除。该证据证明真实 Redis TLS、Lua/TTL 和 fail-closed 客户端契约，不证明生产多节点切换；
目标环境仍必须按 `acceptance-record.example.md` 补齐切换和跨 API 节点共享 limiter 证据。

本轮 integration 完成后已删除 `tricycle-integration-20260819-r6` 的 14 个对象版本和
14 个 delete marker，并删除隔离数据库；`r5`/`r6` 隔离数据库残留计数为 0。默认开发数据库、
默认 bucket 及其既有内容未清理。

本次恢复清单增量复核（2026-08-19）结果：`uv run --frozen pytest -q` 为 `291 passed,
112 skipped`；`make test-infra` 为 `111 passed, 2 skipped, 290 deselected`；Ruff、mypy 和
pyright 仍全部通过。`tricycle-validate-restore --max-artifacts 1` 在当前开发库明确输出
`succeeded=false` 和 `partial validation requested`，证明快速预检不会被误当作正式恢复验收。
随后复跑 `make frontend-check`、`make frontend-build`、`uv run --frozen alembic check`、
`uv lock --check`、`docker compose config --quiet` 和 `make audit` 均通过；供应链审计为
vendored assets 4/4、Python runtime 无已知漏洞、npm 0 vulnerabilities。以上仍是本地/隔离
证据，不替代目标生产 OIDC、SMTP、TLS、切换和 RTO/RPO 记录。

## 7. 2026-08-20 增量复核

本轮重新执行 `make check`、`make test-infra`、`make test-redis`、`make audit`、前端
build/typecheck 和 `alembic check`。默认测试结果为 `291 passed, 112 skipped`；数据库/RustFS
集成为 `111 passed, 2 skipped, 290 deselected`；Redis 6.0.16 临时实例上的真实 Lua/TTL
共享 limiter 为 `2 passed`；静态检查、供应链审计和 Alembic 均通过。临时 Redis 进程、动态库
解包目录和测试 key 已停止/删除，未改变现有 PostgreSQL、RustFS 或 Keycloak 容器。

当前 API 进程连接的是没有 DA-bench 完整数据的默认开发数据库，因此直接运行
`make frontend-test-e2e` 得到 `27 passed, 18 failed`。失败集中在固定数据前提（mapped
reaction、多帧 artifact、DA bench label 等），不是把页面错误藏在跳过里；隔离 API + 完整
fixture 的 `45 passed` 记录仍保留在 R5，但默认库这次结果不能作为 E2E 发布证据。

V1-06 的仓库实现现已完成：`.github/workflows/ci.yml` 使用固定 digest 的 PostgreSQL/RustFS
service，并在 Playwright seed 前运行 `scripts/validate_da_bench_fixture.py`。manifest v3 明确记录
4 个日志的 gzip/source SHA-256 与 source size，以及 3 个 metadata JSON 的 SHA-256 与 size；
validator 同时拒绝 schema 漂移、重复路径和越界路径。`make validate-da-bench-fixture` 输出
`logs=4, metadata_files=3`，validator/CI 契约专项为 `12 passed`；`make lint type test` 为
`299 passed, 112 skipped`，`uv lock --check` 通过。此处是仓库与本地验证结果，首次远端 GitHub
Actions 报告仍须随 pull request 留存。R3/R4/R6 的生产 OIDC、SMTP、TLS、多节点切换、告警和
RTO/RPO 缺口保持不变。

R3/R4/R6 的仓库验收工具增量（2026-08-20）：`tricycle-deployment-smoke` 输出
`deployment-smoke-v1`；`tricycle-validate-deployment-acceptance` 使用严格的
`deployment-acceptance-v1` 模型，禁止 `PENDING`、重复/越界附件和不一致 RTO/RPO，并重新计算
所有附件 SHA-256/大小。R4 证据必须分别由
`scripts/benchmark_upload_resources.py --output`、`scripts/capture_query_plan_evidence.py` 和
`scripts/probe_shared_rate_limit.py` 生成；validator 要求 `upload-resource-benchmark-v2`、
`upload-limit-probe-v1`、`query-plan-evidence-v1`、`shared-rate-limit-v1`、
`rate-limit-fail-closed-v1`，不接受人工自报字段。validator 会从完整计划树和逐节点 HTTP 观察
重新计算 accepted/index/scan、共享预算和 503 fail-closed，而不是信任布尔字段。相关
validator/probe/capture 测试为 `26 passed`；上传报告必须使用部署方真实 Gaussian/ORCA 数据，
不接受仓库合成 fixture 结果；
默认 PostgreSQL 连接因运行容器仍使用 `tricycle` 而示例配置使用 `example_user`，目标 query-plan
capture 明确失败并保留该环境阻塞，未修改容器或数据库。

## 8. 2026-08-20 NexusX 版本增量

上游 <https://github.com/KLR-Pattern/nexusx> 当前最新稳定发布为 `v6.1.2`，已将
`pyproject.toml` 和 `uv.lock` 从 `6.1.1` 精确升级到 `6.1.2`，并在 `.venv` 中确认实际
distribution 版本为 `6.1.2`。本次升级保留并验证项目已有的 DTO-first Compose executor、
`UseCaseAppConfig`、四层 progressive-disclosure MCP、Streamable HTTP 和
`ComposedErManager`/Voyager 集成，同时采用 6.1.2 的严格 selection 校验；未知字段和对
标量附加子选择现在都返回结构化 `SelectionError`，不会静默丢弃字段。6.1.2 的 federation
`page_by_*_in` 默认排序能力已由依赖提供；当前项目仍是单 PostgreSQL 逻辑 engine，未把
HA 节点误建为多个 NexusX member，未来新增独立 engine 时才启用该能力。

本轮证据：

- NexusX/GraphQL/MCP/Voyager 专项：`25 passed`。
- `make check`：前端 typecheck/build、Ruff、mypy、Pyright，以及 `317 passed, 112 skipped`；
  跳过项均为显式要求数据库、RustFS 或 Redis 的集成测试。
- `make audit`：vendored assets `4/4`、Python `pip-audit` 无已知漏洞、npm `0 vulnerabilities`。
- `uv lock --check`、`git diff --check`：通过。

以上是仓库和本地运行时证据，不改变 R3/R4/R6 的外部验收状态。真实 HTTPS OIDC
authorization-code/PKCE、SMTP STARTTLS 邀请、PostgreSQL/RustFS/Redis 私有 CA、多 API 节点
切换、目标数据规模 query-plan、生产对象版本恢复、告警触发/恢复和实测 RTO/RPO，仍必须
在目标多主机环境按 `deployment-acceptance-v1` 记录并通过 validator；当前默认 PostgreSQL
容器仍使用 `tricycle` 凭据，示例配置的 `example_user` 不能用于默认 query-plan capture。
