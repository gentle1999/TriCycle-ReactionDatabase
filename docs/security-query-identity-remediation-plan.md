# 安全、查询与身份服务重构修复计划

> English edition: [Security, query, and identity remediation plan](en/security-query-identity-remediation-plan.md). This is a dated plan.

> 状态：S1-S4、Q1-Q3、D1、D2、I1 已实现并完成全量验证；身份服务决策为保留 Keycloak（复查日期：2026-11-16）
>
> 建立日期：2026-08-16
>
> 适用范围：`src/tricycle_reaction_db/`、`frontend/`、`migrations/`、`infra/`、
> `compose.yaml`、依赖清单和相关测试
>
> 文档职责：本文件固定 2026-08-16 审查发现的安全、查询成本、依赖和 OIDC 身份服务
> 修复顺序。领域模型、不可变科学事实和前端资源边界仍以
> [技术方案与实施路线图](technical-roadmap.md)、[数据模型与存储边界](data-model.md) 和
> [前端重构计划](frontend-refactor-plan.md) 为准。

## 1. 目标

本轮重构的目标不是扩展业务功能，而是在保持现有 API 和科学数据语义的前提下完成以下
工作：

- 关闭跨项目结构表示的授权旁路和共享缓存泄漏；
- 对上传文件数、总字节、解析并发和 MolOP worker 建立硬上限；
- 将项目授权从全库读取后 Python 过滤改为数据库内的定向查询；
- 消除浏览器 Session 和 Bearer Token 认证路径的逐请求写放大；
- 修复已知 Python 和前端供应链公告，并建立可重复的审计门；
- 通过独立 PoC 判断是否以服务端 npm 身份服务替换本地 Keycloak；
- 为上述行为补齐跨项目、缓存、查询数、资源上限和 OIDC 回归测试。

完成后不得改变 Formula、Topology、Geometry、CalculationFrame、Reaction、Artifact 的
业务身份，不得重新解析或重写既有科学事实。

## 2. 审查基线

### 2.1 已确认问题

| ID | 优先级 | 状态 | 问题 |
| --- | --- | --- | --- |
| `S1` | P0 | `done` | Topology SVG/MOL 仅按 UUID 读取，未使用项目可见性谓词 |
| `S2` | P0 | `done` | 受保护的 Geometry/Topology 表示响应带有 `Cache-Control: public` |
| `S3` | P0 | `done` | 批量上传没有文件数和总字节上限，默认 MolOP `n_jobs=-1` |
| `S4` | P0 | `done` | 生产配置允许默认 Session secret 和非 Secure Session Cookie |
| `Q1` | P1 | `done` | `project_accesses()` 读取全部活动项目后在 Python 中过滤 |
| `Q2` | P1 | `done` | Session 每请求更新；Bearer 每请求更新身份并写 `auth.login` |
| `Q3` | P1 | `done` | 文件名包含搜索缺少 trigram 索引，并固定执行 count + offset |
| `D1` | P1 | `done` | `fastmcp 3.1.1` 和 `cryptography 49.0.0` 命中已知公告 |
| `D2` | P1 | `done` | ChemDoodle 专用 jQuery 链被全局加载，尚未与 Vue 业务 UI 隔离 |
| `I1` | P2 | `done` | 已完成服务端 npm OIDC 协议 PoC、Keycloak 基线与隔离恢复演练；决定保留 Keycloak，见[身份提供方决策记录](identity-provider-decision.md)，复查日期 2026-11-16 |

### 2.2 依赖和最终验证快照

- PyPI 最新 MolOP 与当前锁定版本均为 `0.2.5.post1`；定向执行
  `uv lock --upgrade-package molop` 没有产生版本变化。
- Python 审计发现 `cryptography 49.0.0` 的 PKCS#7 oracle 公告，以及 FastMCP 3.1.1
  的命令注入和 OAuth confused-deputy 公告；是否可利用仍取决于项目启用的具体功能。
- `package-lock.json` 的 npm audit 无公告，但该结果不包含 `frontend/public/vendor/` 下的
  手工 vendored 脚本。
- 本轮定向验证：认证、授权、配置、上传、查询成本、反向代理、MCP、NexusX 和前端契约单元
  `83 passed`；数据库授权/游标/认证热路径/查询成本聚焦集合 `23 passed`；vendored asset
  审计为 `4 assets` 且 OSV 查询通过。
- 全量默认 pytest 为 `218 passed, 105 skipped`；数据库 integration 为
  `97 passed, 9 skipped, 217 deselected`，RustFS 单独为 `1 passed, 8 skipped, 314 deselected`，
  数据库与 RustFS 同时开启的 integration 集合为 `106 passed, 217 deselected`。跳过项均由
  显式基础设施开关控制。
- `uv lock --check`、`alembic check`、全量 Ruff check/format check、`pyright src scripts`、
  `mypy src scripts`、前端 `npm run check`、Playwright `34 passed`、pip-audit 和高危级别
  npm audit 均通过。Playwright 确认父页面和官方 ChemDoodle 编辑器 sandbox 均未暴露
  `$`/`jQuery`，且编辑器资源没有 jQuery 请求。
- 数据库处于 Alembic `20260817_0051 (head)`，`alembic check` 无 schema 漂移；活跃 Session
  部分索引 revision 已完成一次 `0051 -> 0050 -> 0051` downgrade/upgrade 可逆验证。

以上快照记录本轮最终回归证据。每个任务均以新增的失败测试或可复现查询作为起点，并以
通过相同测试作为完成条件。

## 3. 重构原则

1. **授权先于缓存。** 只有能证明内容公开的响应才能进入共享缓存；项目数据默认
   `private, no-store`。
2. **数据库完成集合过滤。** 禁止为了检查一个项目权限而加载全库项目，也禁止把大量
   项目 UUID 展开成应用层 `IN (...)` 参数。
3. **读取路径默认无写入。** 登录、资料同步和审计是事件；普通 API 请求不是登录事件。
4. **资源预算必须是硬边界。** 单文件限制不能替代批次总量、并发、解压后大小和 worker
   上限。
5. **Vue 拥有前端交互。** 页面状态、工具栏、对话框、错误、响应式尺寸和组件生命周期由
   Vue 组件管理；第三方化学引擎只能位于一个 typed adapter 后。ChemDoodle 编辑器不得向
   主应用的 `window`、业务组件或通用 composable 暴露 legacy 全局依赖。
6. **身份提供方只能运行在服务端。** npm 包不得打入 Vite 浏览器 bundle，不得把客户端
   secret、签名私钥或密码校验放到前端。
7. **先兼容再切换。** Keycloak 替换必须经过独立 PoC、身份映射、备份恢复和回滚演练，
   不与 P0 安全修复绑定发布。
8. **保持科学事实不可变。** 本计划允许修改访问、查询、会话、索引和基础设施，不允许
   原地改写既有 Artifact、Frame、Geometry 或 Reaction 事实。

## 4. 阶段 F0：冻结契约和回归基线

### F0.1 授权矩阵

为以下表示接口建立匿名用户、项目外用户、Viewer、Contributor、Manager 的矩阵：

```text
/api/depictions/geometry/{geometry_id}.svg
/api/depictions/geometry/{geometry_id}.sdf
/api/depictions/topology/{topology_id}.svg
/api/depictions/topology/{topology_id}.mol
/api/depictions/calculation-frame/{frame_id}/transition-state/{anchor}.sdf
```

同一测试数据必须至少包含一个公开 Artifact、两个互不授权的私有项目、共享 Topology 和
各项目独有 Topology。无权和不存在对象统一返回 404，避免通过状态码枚举私有 UUID。

### F0.2 查询与资源基线

本阶段的可复现命令、SQL 次数、授权矩阵、1/8/32 文件资源数据和 Keycloak 开发基线保存于
[安全与查询重构基线](security-query-baseline.md)。

- 记录 `project_accesses()`、`require_project_permission()`、`authenticate_session()`、
  Bearer principal 解析和 Artifact 搜索的 SQL 次数。
- 为代表性项目数、成员数、Session 数和 Artifact 数保存 `EXPLAIN (ANALYZE, BUFFERS)`。
- 记录 1、8、32 个文件批次的进程内存峰值、解析进程数和失败补偿结果。
- 保存 Keycloak 冷启动、稳定内存、登录和登出基线，作为 OIDC PoC 对照。

**阻断验收：** 基线测试可以稳定复现 `S1`、`S2`、`Q1` 和 `Q2`；测试不得依赖生产数据，
不得把 secret、Token、OIDC claims 或原始计算文件内容写入日志。

## 5. 阶段 F1：P0 安全边界

### S1：统一 Topology 可见性

**实现范围：**

- 在 `application/services/query_visibility.py` 增加共享的 Topology 可见性谓词；可见性必须
  从可见 Artifact 的 ParseRevision/CalculationFrame，经 Geometry 追溯到 Topology。
- `get_topology_depiction()` 和 `get_topology_molfile()` 使用与 Geometry 查询相同的
  `QueryVisibilityScope`，不在路由层复制授权 SQL。
- 审计所有直接 `select(MolecularTopology)` 和 `session.get(MolecularTopology, ...)`，区分
  ingestion 内部写路径与面向请求的读取路径。
- REST、GraphQL、MCP 和独立 UseCase 入口继续共享 application service，不新增旁路路由。

**阻断验收：**

- 项目外用户无法读取私有 Topology 的 SVG、MOL 或详情；
- 通过共享 Geometry、Frame、反向查询和直接 UUID 请求得到相同授权结果；
- 公开和项目内对象保持现有可用行为；
- 新增跨项目 Topology 集成测试，且无权与不存在对象均返回 404。

### S2：修正表示缓存策略

**实现范围：**

- 第一阶段将所有经过项目可见性判断的 Geometry、Topology 和 TS anchor 响应统一设置为
  `Cache-Control: private, no-store`。
- Cloudflare、Nginx 或 Caddy 对 `/api/*` 默认 bypass cache；不得只依靠应用响应头纠正已
  配置的强制边缘缓存规则。
- 后续如需公开 immutable cache，必须让 application service 返回明确的公开可见性分类，
  只对“至少存在公开来源且不依赖用户权限”的表示返回 `public, immutable`。
- 不允许仅增加 `Vary: Cookie` 后继续缓存项目私有结构。

**阻断验收：** 项目私有表示的响应和反向代理结果均不可进入共享缓存；自动测试断言
`Cache-Control`，并模拟用户 A 请求后用户 B 请求同一 URL 的行为。

### S3：限制上传和 MolOP 解析资源

**实现范围：**

- 新增 `TRICYCLE_MAX_BATCH_FILES` 和 `TRICYCLE_MAX_BATCH_BYTES`，同时检查文件数、单文件
  字节和批次累计字节；超限返回稳定的 413 错误码，不进入 RustFS 或解析阶段。
- 初始默认值固定为 32 个文件、256 MiB 总请求载荷；若代理层使用更小上限，以更小值为准。
- 生产默认 `TRICYCLE_MOLOP_BATCH_N_JOBS=2`；禁止生产配置使用 `-1`。开发环境可以显式
  选择 `-1`，但不得写入生产示例。
- 增加进程级 MolOP 解析 semaphore，默认同时只运行一个解析请求。每个 Uvicorn worker
  维护一个可复用的 MolOP 进程池；多 worker 部署必须按
  `Uvicorn worker 数 x TRICYCLE_MOLOP_BATCH_N_JOBS` 计算全机进程和内存上限。
MolOP 解析并发只由显式解析进程池的 worker 数控制，不再设置请求级 slot 闸门。
- gzip 等压缩输入在解压前后都检查大小；禁止仅按上传压缩包大小判断资源预算。
- 中期把批量接口从“全部读取为 `bytes` 后再写临时文件”改为受控 spool/path 流程；
  immediate fix 不能等待该重构完成。

**阻断验收：** 超出文件数、总字节、解压后字节或解析 slot 的请求不会导致未界定内存
增长；部分失败仍保留逐文件结果，pending reservation 和 RustFS 对象补偿测试通过。

### S4：收紧生产认证配置

**实现范围：**

- `environment=production` 时拒绝默认 `session_secret`，并要求
  `session_cookie_secure=true`。
- OIDC 模式启动时要求非空 client ID、client secret、redirect URI、issuer、audience 和
  JWKS URL；生产 issuer、redirect URI 和 JWKS URL 必须是 HTTPS。
- 明确记录 TLS 在 Cloudflare/反向代理终止时，浏览器看到的外部 URL 仍必须是 HTTPS。
- `compose.yaml` 中的 Keycloak `start-dev`、默认管理员和开发 realm 继续只绑定 loopback，
  并在文档中标为不可直接暴露的开发配置。

**阻断验收：** 缺少任一生产安全项时应用启动失败并返回明确配置错误；开发环境和现有
本地 Keycloak 流程保持可用。

## 6. 阶段 F2：查询和认证写放大

### Q1：数据库内完成项目授权

**实现范围：**

- 将组织 owner/admin 可访问项目与直接 ProjectMembership 可访问项目写成数据库查询，
  只返回当前用户有权访问的 Project/Organization 行。
- `require_project_permission()` 改为针对单一 `project_id` 的 `SELECT EXISTS`，不得调用
  `accessible_project_ids()` 后扫描集合。
- `project_accesses()` 合并为至多一个集合查询，不再先读 membership 再读取全部活动项目。
- 将 QueryVisibilityScope 从 Python UUID 集合逐步改为以 user ID 和 permission 构造的
  `EXISTS` 子查询，避免大量 `IN` bind 和计划缓存碎片。
- 保持 organization owner/admin、Manager、Contributor、Viewer 的现有权限矩阵不变。

**性能验收：**

- 单项目权限检查只发出 1 条 SQL；
- 项目访问列表只返回有权行，扫描行数不随其他组织项目数线性增长；
- 新增查询数断言和至少一个“大量无关项目”集成用例；
- `EXPLAIN` 使用 membership/organization 外键或复合索引，不出现意外全表读取。

### Q2：分离登录事件和普通请求认证

**实现范围：**

- Session principal 使用一次联表查询读取 AuthSession、UserAccount 和最近 ExternalIdentity。
- 仅当 `last_seen_at` 距当前时间超过 5 分钟时执行条件 UPDATE；普通请求不再固定 commit。
- 将“首次 OIDC provisioning / 登录资料同步”和“已有身份的 Bearer 请求解析”拆成不同
  service。已有 ExternalIdentity 的 Bearer 请求只读，不更新 claims、用户资料或
  `last_authenticated_at`。
- `auth.login` 只在 authorization-code callback 成功创建浏览器 Session，或首次建立外部
  身份时记录；不得按 API 请求记录。
- 为 Bearer-only 首次访问定义显式 provisioning：缺少映射时只执行一次幂等 upsert，冲突
  后重新读取，不把每次请求当作登录。
- OIDC discovery 使用按 issuer 缓存的短 TTL 元数据；网络失败不得无限缓存。
- Session 列表增加分页和过期过滤；增加过期/撤销 Session 清理任务及对应索引审查。

**性能验收：**

- 热 Session 请求为 1 条 SELECT、0 条写 SQL；只有节流窗口跨越时允许 1 条条件 UPDATE；
- 已 provisioning 的 Bearer 请求不写 UserAccount、ExternalIdentity 或 AuditEvent；
- 连续 100 次认证请求只产生预期的节流写入，不产生 100 条 `auth.login`；
- 撤销、暂停用户、身份缺失、Token 过期和并发首次 provisioning 测试通过。

### Q3：Artifact 搜索和分页

**实现范围：**

- 启用 PostgreSQL `pg_trgm`，为 `ArtifactFile.original_filename` 增加与实际 ILIKE 表达式
  匹配的 GIN trigram 索引。
- 对用户目录的 display name/email 包含搜索执行相同计划审查；只有真实查询需要时才增加
  索引，避免无依据的索引扩张。
- 保留现有分页 DTO 的兼容路径，同时增加无需精确 total 的 cursor/keyset 模式；前端普通
  翻页优先使用 keyset，只有明确需要总数的页面才执行 count。
- 对 `(created_at DESC, id)` 等稳定排序补齐与可见性、项目过滤匹配的复合索引，具体索引
  以 `EXPLAIN` 结果为准。

**性能验收：** 非空文件名包含搜索在代表性数据集上命中 trigram 索引；深页不再因 offset
线性丢弃大量行；列表 SQL 数量固定，不出现逐行加载。

## 7. 阶段 F3：依赖和前端供应链

### D0：MolOP 升级策略

当前 `molop 0.2.5.post1` 已是最新版本，本阶段不进行伪版本更新。后续新版本发布时：

1. 仅执行 `uv lock --upgrade-package molop`，先检查 MolOP/MolGR/RDKit 传递变化；
2. 运行 MolOP mapping、array、单文件/批量解析和真实 ORCA/Gaussian fixture；
3. 对比 parser provenance、frame 数、坐标、Topology、TS endpoint 和错误报告；
4. 只有行为回归通过后才接受锁文件变化。

### D1：修复 Python 公告

- 将 FastMCP 约束提升到包含修复的 `>=3.2,<4`，逐项核对 Middleware、Client、
  Streamable HTTP、ToolResult 和 NexusX 集成 API。
- 将 `cryptography` 解析到 `>=50`；优先通过上游 PyJWT 传递约束解决，不无故增加重复的
  顶层依赖。
- 重新运行 REST、GraphQL、MCP 授权矩阵，并确认 FastMCP 公告涉及的 Gemini CLI/OAuth
  proxy 代码是否在本项目可达。
- CI 中加入锁文件 Python 审计；公告豁免必须记录 ID、不可达证据、责任人和失效日期。

### D2：隔离 ChemDoodle 编辑器，其他 UI 收敛到 Vue

#### D2.1 已确认依赖面

当前只有 `ChemDoodleTopologyEditor.vue` 使用 `ChemDoodle.SketcherCanvas`，并在专用 sandbox
iframe 中加载 ChemDoodle Web Components `11.0.0` 的 core/UI 资产。浏览器验证确认该版本
编辑器不需要 jQuery、jQuery UI 或 touch-punch；现有 `ViewerCanvas`、`TransformCanvas3D`、
`MovieCanvas3D`、SDF/MOL 解析和 TS 动画也只使用 ChemDoodle core。ReactionView、GeometryView
和其他 Vue 业务组件没有直接调用 jQuery。

因此本任务不要求为了形式统一而替换 ChemDoodle，也不引入另一套 React/GWT 分子编辑器。
目标是把官方 ChemDoodle UI 限定在 typed、sandboxed editor 边界内，并从主 Vue 应用的
全局运行环境中移除旧的编辑器依赖链。

#### D2.2 Vue 编辑器契约

- 保留 `ChemDoodleTopologyEditor.vue` 作为唯一的 ChemDoodle 编辑器入口。Vue 负责 SMILES
  输入、外层命令、清空、错误状态、loading、键盘可达性、ResizeObserver 和卸载清理。
- 页面只通过 `v-model` 交换 `smiles` 和 `molfile`；ReactionView、GeometryView 不得直接访问
  第三方 editor、canvas、iframe 或全局变量。
- 建立 typed `ChemDoodleEditorBridge`，最小接口为 `loadMolfile()`、`getMolfile()`、
  `getSmiles()`、`clear()`、`resize()`、`onChange()` 和 `destroy()`；bridge command/event 带
  固定协议版本，拒绝未知消息。
- 结构解析、标准化和搜索有效性以后端 RDKit/MolAlchemy 结果为权威。浏览器编辑器输出只是
  用户输入，不能直接成为数据库 canonical identity。
- ChemDoodle 编辑器 core/UI 只在 editor sandbox 按使用页面加载；只读 core 由 typed adapter
  按需加载。Reaction/Geometry 以外的首屏不得下载任何 ChemDoodle runtime 或 legacy DOM 依赖。
- Vue 业务 UI 继续使用 Vue 3、Vue Router、TanStack Vue Query、Lucide 和原生 Web API；
  禁止为普通表格、弹窗、拖放、请求、动画或状态管理新增 jQuery 调用。

#### D2.3 隔离方式

把 SketcherCanvas 放入专用、懒加载的 sandboxed editor document：

```text
Vue ChemDoodleTopologyEditor
  -> sandboxed iframe + typed postMessage bridge
       -> ChemDoodle Web Components 11.0.0 core/UI
            -> 无 jQuery 运行时依赖
```

- 主 `frontend/index.html` 不加载 jQuery、jQuery UI、touch-punch 或
  `ChemDoodleWeb-uis.js`；只读渲染所需的 ChemDoodle core 与编辑器资源分离。
- 专用 editor document 只加载固定版本的 ChemDoodle 11.0.0 core/UI，不加载业务 API client、
  Session、Router、项目数据或 Token。
- iframe 默认只授予脚本运行所需的最小 sandbox capability。父组件按 `event.source`、协议
  版本和消息 schema 校验通信，不接受任意 command 或 HTML。
- editor document 设置独立 CSP，禁止远程脚本、任意导航、弹窗和非必要网络请求。
- 如果 ChemDoodle 在严格 sandbox 中确实不可运行，只能逐项放宽专用 iframe 的必要
  capability，并记录浏览器测试和风险；不得退回父 window 全局加载编辑器脚本。

ChemDoodle core 继续用于已有 2D/3D 只读展示，已从 `index.html` 的全局同步脚本收敛到
typed、懒加载的 `useChemRenderer()` adapter。此步骤不得改变现有 movie、TS 动画和非空
canvas 行为。

ChemDoodle 版本、来源、许可证和 SHA-256 固定在 vendor manifest 中；升级必须重新运行
版本标记、哈希和 OSV 审计，并通过官方 UI sandbox 的浏览器回归。

#### D2.4 删除和验证

- 删除主 `index.html` 中 jQuery、jQuery UI、touch-punch 和 ChemDoodle UI bundle 的
  script 与 stylesheet；官方 UI 资产只在专用 editor document 中加载。
- `frontend/src/` 和普通 Vue 页面不得出现 `$`、`jQuery`、`ChemDoodle.uis` 或
  `SketcherCanvas` 引用；编辑器只通过 typed bridge 通信。
- vendored 化学资产记录来源版本、许可证和 SHA-256；CI 对剩余 vendor 清单运行 OSV/版本
  检查。
- Playwright 覆盖 topology search、Reaction reactant/product 编辑、清空、错误恢复、多组分、
  桌面/移动端、3D movie、TS 动画、canvas 像素和浏览器 console error。
- Playwright 断言普通页面和主 window 没有 `$`/`jQuery`，官方 editor sandbox 也没有这些
  全局，且 editor resource timing 不包含 jQuery。

**阻断验收：** `pip-audit`、npm audit 和 vendor OSV 检查没有未解释的高危公告；主 Vue
应用及其业务组件不包含或暴露 jQuery/jQuery UI；官方 ChemDoodle 11.0.0 UI sandbox 在
无 jQuery 环境中完成结构搜索、Reaction 创建和只读展示回归。

## 8. 阶段 F4：轻量 OIDC 服务 PoC

### 8.1 决策边界

OIDC npm 包必须作为独立服务端进程运行，通过同源反向代理暴露；不得加入
`frontend/package.json` 后打入静态 bundle。PoC 不直接删除 Keycloak，也不修改现有用户
授权关系。已完成的候选验证、Keycloak 基线、恢复证据和最终选择见
[身份提供方决策记录](identity-provider-decision.md)。

| 方案 | 协议能力 | 账户与交互 | 运维边界 | 本计划定位 |
| --- | --- | --- | --- | --- |
| Keycloak | 完整 OIDC、管理、MFA、联合身份 | 内置 | JVM 服务，资源较高 | 当前实施和回滚基线 |
| Better Auth + `@better-auth/oauth-provider` | OAuth 2.1、OIDC discovery、JWT/JWKS、PKCE | Better Auth 提供账户/Session，仍需配置页面和邮件 | 服务端 Node + 数据库 schema | 已完成导入/能力 PoC，未替换 |
| `oidc-provider` | 成熟且已认证的 OIDC 协议核心 | 必须自建账户发现、登录/同意页面和持久化 adapter | 服务端 Node + 较多自维护安全代码 | 已完成协议 smoke，未替换 |

2026-08-16 的候选快照为 Better Auth 1.6.29、`@better-auth/oauth-provider` 1.6.29 和
`oidc-provider` 9.11.3。实施时必须重新检查版本、维护状态和安全公告。

### 8.2 PoC 目标架构

```text
Browser
  -> https://reaction.example.com/identity/*
       -> local Node identity service
            -> dedicated PostgreSQL schema
  -> https://reaction.example.com/api/*
       -> FastAPI
            -> validate issuer + audience + JWKS
            -> local opaque browser Session
```

- Node 身份服务只监听 loopback/private network，由同源代理转发 `/identity/*`。
- 身份表使用独立 schema 和独立数据库角色，不直接写业务 `user_account`、项目或科学事实表。
- FastAPI 继续拥有本地 opaque Session、项目权限和审计；身份服务只负责认证和 OIDC Token。
- OIDC issuer、discovery、JWKS、authorization、token、userinfo 和 logout 路径必须在代理后
  保持一致，禁止内部 host 泄漏到 metadata。
- 当前 Python authorization-code client 增加 PKCE。state、nonce、code verifier、return URL
  使用短期一次性 auth transaction 保存，callback 成功或失败后立即失效。

### 8.3 身份迁移

- 新 issuer 会产生新的 `issuer + subject`。切换前生成显式映射，把新 subject 绑定到已有
  UserAccount UUID；不得按邮箱静默合并生产用户。
- 对 bootstrap administrator、普通用户、暂停用户和 invitation 邮箱分别演练。
- 签名密钥支持轮换并保留验证窗口；备份包含身份 schema、密钥和必要配置，不把明文
  secret 写入 Git。
- 保留 Keycloak realm export 和 volume 备份；回滚时恢复旧 issuer 配置和旧 ExternalIdentity
  映射，不回滚业务数据库迁移。

### 8.4 PoC 验收与切换门

以下条件全部满足后才能提出替换 Keycloak：

- 注册策略、邮箱验证、密码或 passkey、MFA、找回、暂停用户和管理员操作边界已明确；
- authorization code + PKCE、nonce/state、refresh、logout、Session 撤销和 JWKS 轮换通过；
- 项目邀请在首次登录和已有用户两条路径都不会重复创建 UserAccount；
- Cloudflare/反向代理后的 issuer、Secure Cookie、回调 URL 和缓存策略通过浏览器测试；
- 完成身份数据库迁移、备份恢复、密钥轮换、升级和回滚演练；
- 记录冷启动、稳定内存、登录延迟和维护复杂度，与 Keycloak 基线比较；
- 对外部署需要的能力没有退化，且没有新增由前端持有的 Token 或 secret。

如果上述任一身份生命周期能力需要大量自研安全代码，终止替换并继续使用 Keycloak 或托管
OIDC。资源更小本身不是切换依据。

### 8.5 I1 已完成证据

- `oidc-provider` `9.11.3` 在 Node `22.23.2` disposable smoke 中返回 discovery、JWKS、
  logout 和 `S256` PKCE 元数据；Better Auth 候选的 OAuth provider exports 也已验证可导入。
  这些结果证明协议核心可运行，不冒充生产身份服务。
- 本轮没有创建新 issuer 或修改 `ExternalIdentity`；现有身份键仍是唯一的 `issuer + subject`。
  未来迁移必须使用逐用户显式映射到已有 `UserAccount.id`，禁止按邮箱静默合并。
- Keycloak 镜像已固定 digest；本地 realm export 包含 7 个 clients、1 个 user 和 3 个 realm
  roles，并在全新 volume 上恢复验证为 7 个 clients、1 个 user（`developer@localhost`）。
- 已记录停止新身份服务流量、恢复 Keycloak issuer/client/JWKS 和旧 realm/volume 的配置级
  回滚顺序；由于没有部署新 issuer，这不是生产切换或生产灾备演练。

## 9. 阶段 F5：发布、观测和回滚

### 9.1 发布顺序

1. 独立发布 `S1`、`S2` 及授权/缓存回归测试；
2. 发布 `S3`、`S4`，先以开发环境和单 worker 验证资源上限；
3. 发布 `Q1`、`Q2`，对比 SQL 次数、AuditEvent 增长率和请求延迟；
4. 发布 `Q3` 数据库索引和分页兼容路径；
5. 独立发布 `D1` 依赖变化和 `D2` Vue 编辑器迁移；
6. OIDC PoC 与上述修复并行但不进入生产主链，切换需要单独评审。

每个步骤使用独立提交和可独立回滚的 Alembic revision。禁止把授权修复、查询重写、依赖
大版本和身份切换合并为一次不可拆分发布。

### 9.2 观测指标

- 401/403/404 比例和 depiction 跨项目拒绝计数；
- shared-cache 命中头和 `/api/*` 边缘缓存旁路状态；
- 每请求 SQL 数、数据库读取行数、Session UPDATE 频率和 AuditEvent 增长率；
- 上传批次大小、解析 slot 等待时间、MolOP 子进程数、峰值 RSS 和补偿失败数；
- Artifact 文件名搜索的执行时间、buffer hit/read 和索引使用；
- OIDC discovery/token 错误率、登录延迟、Session 创建和身份映射冲突。

日志不得包含 Session Token、Authorization header、OIDC code、client secret、邀请 Token、
RustFS credential 或原始计算文件内容。

### 9.3 回滚规则

- 授权回滚不得恢复已确认的跨项目读取；出现兼容问题时宁可临时禁用表示接口。
- 缓存回滚保持 `private, no-store`，不得为了命中率恢复项目数据公共缓存。
- 索引 migration 必须提供 downgrade，但生产大表 downgrade 前先评估锁和磁盘成本。
- 上传新限制可通过配置向上调整，但不得设置为无限。
- OIDC 切换失败时恢复 Keycloak issuer 和代理路由；本地 UserAccount、项目角色和科学事实不
  随身份提供方回滚。

## 10. 验证命令

验证只读命令使用 `--frozen`，避免测试过程同步依赖或改写锁文件。

```bash
uv lock --check
uv run --frozen ruff check src tests migrations scripts
uv run --frozen ruff format --check src tests migrations scripts
uv run --frozen pyright src scripts
uv run --frozen mypy src scripts
uv run --frozen pytest -q
TRICYCLE_RUN_DATABASE_TESTS=1 uv run --frozen pytest -m integration
TRICYCLE_RUN_RUSTFS_TESTS=1 uv run --frozen pytest -m rustfs
TRICYCLE_RUN_DATABASE_TESTS=1 TRICYCLE_RUN_RUSTFS_TESTS=1 \
  uv run --frozen pytest -q -m integration
uv run --frozen alembic check
npm --prefix frontend run check
npm --prefix frontend run test:e2e
npm --prefix frontend audit --audit-level=high --registry=https://registry.npmjs.org
uv run --frozen python scripts/audit_vendored_assets.py
```

Python 锁文件审计需要排除当前 editable 项目，并禁止测试工具重新解析依赖：

```bash
uv export --frozen --no-dev --no-emit-project --no-hashes --format requirements-txt \
  | uvx --from pip-audit==2.10.1 pip-audit --disable-pip --no-deps -r /dev/stdin
```

新增数据库索引或查询重写还必须运行：

```bash
TRICYCLE_RUN_DATABASE_TESTS=1 \
  uv run --frozen pytest -q tests/integration/test_query_cost_database.py
```

## 11. 完成定义

只有以下条件全部满足，本计划才能标记为 `done`：

- Topology、Geometry、TS anchor 的列表、详情和表示接口使用一致的项目可见性；
- 项目私有结构不会进入浏览器外的共享缓存；
- 上传批次、累计字节、解压大小、解析并发和 MolOP worker 都有生产硬上限；
- 生产认证配置拒绝默认 secret、非 Secure Cookie 和非 HTTPS OIDC endpoint；
- 单项目授权不扫描全库，热 Session 和已有 Bearer 身份的普通请求不写数据库；
- Artifact 包含搜索有计划证据，深分页不再依赖无限增长的 offset；
- Python、npm 和 vendored 资产没有未解释的高危公告；
- 主 Vue 应用、业务组件和普通页面不包含或暴露 jQuery；官方 ChemDoodle 11.0.0 UI
  sandbox 按需加载并在无 jQuery 环境中通过浏览器验证；编辑器边界不暴露 legacy 全局；
- MolOP 行为、解析证据和现有科学数据契约没有回归；
- 全部 lint、类型、单元、数据库、RustFS、前端和浏览器门禁通过；
- OIDC PoC 已形成明确的“切换”或“保留 Keycloak”决策，并附协议 smoke、显式身份映射边界、
  本地备份/恢复和配置级回滚证据；不得把这些证据表述为生产迁移或生产灾备演练。

OIDC 是否切换不阻断 `S1` 至 `D2` 完成；不得以等待身份服务决策为理由延后 P0 安全修复。
