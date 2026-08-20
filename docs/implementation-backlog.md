# 实施目标清单

> 状态：执行中
>
> 建立日期：2026-08-12
>
> 维护规则：本文件记录未完成工作和验收状态；架构边界与里程碑定义仍以
> [技术方案与实施路线图](technical-roadmap.md) 为准。

## 1. 状态定义

| 状态 | 含义 |
| --- | --- |
| `done` | 交付物和阻断验收均已完成 |
| `partial` | 主链路可用，但仍有本清单列出的验收缺口 |
| `todo` | 尚未开始或只有底层模型，没有可用业务入口 |
| `blocked` | 存在已记录的外部依赖，当前无法继续 |

任务只有在代码、接口、测试和文档四项均满足验收条件后才能标记为 `done`。新增
查询默认通过同一 NexusX `UseCaseService` 自动注册到 REST、GraphQL 和 MCP；不得另建
直接访问 ORM 的平行接口。

## 2. 当前基线

以下能力已完成，不再列入待办：

- Formula 元素数量范围查询。
- Topology 的精确结构、SMARTS、子结构计数、Tanimoto/Dice、Top-K KNN、描述符和
  scaffold 查询。
- Geometry、CalculationProtocol、CalculationSegment、ParseRevision、
  ArtifactIngestion 和 TransitionStateInference 的列表及详情查询。
- CalculationFrame 多条件筛选，以及 GeometryEnergyView 的计算层级和物理上下文来源选择。
- LogicalReaction、MappedReaction、Topology/Geometry 反向关联和 MappedReaction 的
  reaction SMARTS、相似度及 KNN 查询。
- ScientificArray 元数据和受控 NPY 下载。
- 反应相对能、反应能和正反向势垒查询。

开发期数据库 revision 曾为 `20260812_0033`；主线 baseline 为 `0001_initial_schema`。上述查询已通过 PostgreSQL/RDKit 专项集成
测试以及 `ruff`、`mypy`、`pyright` 检查。

## 3. P0：补齐后端读取面

### Q1 高级计算结果查询

- **状态：** `done`
- **依赖：** 无；现有结果表和 ScientificArray owner 关系可直接使用。
- **目标：** 新增 `CalculationResultQueryService`，按 `frame_id` 返回完整的结构化计算
  结果，而不是只暴露数组 owner 元数据。
- **交付物：**
  - molecular orbital；
  - charge/spin population 与 atomic population series；
  - polarizability；
  - NMR 与 shielding tensor；
  - bond order、total spin 和 single-point properties；
  - electronic state set、state 和 configuration；
  - multireference 与 implicit solvation；
  - 对应稳定 DTO、列表/详情查询和 Frame 反向查询。
- **阻断验收：**
  - 标量和子记录完整返回，ScientificArray 载荷仍只通过受控下载接口读取；
  - REST、GraphQL、MCP schema 均包含新服务；
  - 使用当前数据库中已有 orbital、population、polarizability 和 electronic-state 数据
    完成集成测试；
  - 不暴露 ORM、RDKit 对象、内部 JSONB 或 RustFS 凭据。

### Q2 WorkflowManifest 查询

- **状态：** `done`
- **依赖：** 无；`WorkflowManifest` 和 `ManifestArtifactBinding` 已持久化。
- **目标：** 可审计可选 Manifest 的 revision 链及其 artifact 声明和解析结果。
- **交付物：**
  - Manifest 列表、详情和 revision/supersedes 链；
  - 按 status、schema version、QC policy、artifact 查询；
  - ArtifactBinding 列表和详情；
  - 按 artifact、reaction、path、node、role、resolution status 查询。
- **阻断验收：**
  - 能从 Manifest 完整追踪所有 binding，也能从 Artifact 反查 Manifest；
  - declared、resolved 和失败状态不会混淆；
  - Manifest 保持可选，不成为普通日志上传的前置条件；
  - 自动注册和真实数据库集成测试通过。

### Q3 Storage GC 审计查询

- **状态：** `done`
- **依赖：** 无；GC state 和 run 已持久化。
- **目标：** 为运维提供只读水位和运行审计，不开放修改能力。
- **交付物：**
  - GC state 列表和详情；
  - GC run 列表和详情；
  - bucket、prefix、status 和时间范围筛选；
  - 最近成功运行、当前运行和失败信息投影。
- **阻断验收：**
  - 返回 scan window 及 seen/deleted/retained/failed 计数；
  - 能判断每个 bucket/prefix 的最新水位和最近失败；
  - 查询接口无法启动、重试或修改 GC；
  - 当前数据库已有 GC 记录的集成测试通过。

### Q4 Topology derivation provenance 查询

- **状态：** `done`
- **依赖：** 无。
- **现状：** Frame detail 已内嵌实际 derivation；Topology detail 只返回数量。
- **交付物：**
  - 按 topology、reconstruction method/version、provenance schema/hash 查询；
  - derivation 详情及其 Geometry/Frame 反向引用计数。
- **阻断验收：**
  - 可审计每个 Topology 的 MolOP/MolGR 重建来源；
  - 可用 provenance hash 检测重复和不一致推导；
  - Frame detail 与独立 derivation 查询返回一致。

## 4. P1：完成上传入库和关联闭环

### I1 无整理批量上传 MVP

- **状态：** `done`
- **依赖：** MolOP 公共 probe 和 dump 契约保持稳定。
- **目标：** 用户直接上传任意受支持的 Gaussian、ORCA 或其他 QM 日志，系统自动完成
  格式识别、解析、归一化、去重、关联和入库；Manifest 只作为可选策展入口。
- **交付物：**
  - 文件级独立事务和批量导入报告；
  - probe 自动识别、segment/frame 完整持久化；
  - Formula、Topology、Geometry、Protocol 和 Frame 自动创建或复用；
  - TS 检测、前后体推断、LogicalReaction/MappedReaction 创建或复用；
  - 单文件失败隔离、结构化错误和安全重解析；
  - artifact/parser/config identity 下的幂等导入。
- **阻断验收：**
  - 输入只需要原始日志，不依赖文件名、目录结构或 Manifest；
  - 同一输入重复导入零新增，重解析创建明确的新 revision；
  - 一个文件失败不回滚同批其他成功文件；
  - 任一 Frame、Geometry 和 Reaction 关联可追溯到 Artifact、source span 和 parser
    provenance；
  - Gaussian、ORCA 和混合批次黄金样本端到端通过。
- **验收记录（2026-08-12）：** 单文件/批量上传、只读 validate、MolOP 内容 probe、
  文件级独立事务、全帧持久化、TS 推断和普通重复上传幂等均已实现。显式 reparse 从
  RustFS 读取并校验原始 bytes，创建递增 `revision_number/reparse_of_id`；
  TransitionStateInference 按 ParseRevision + frame 唯一。Gaussian 29 帧、ORCA 1 帧和
  无效文件的真实 PostgreSQL/RustFS 混合批次测试通过；失败文件未回滚其他文件，解析与
  持久化两类失败 reparse 均不覆盖既有成功状态。普通重复上传不创建新 revision，显式
  reparse 不用随机 nonce 伪造科学 identity hash。

### I2 Reaction-Geometry-Frame 关联 QC

- **状态：** `done`
- **依赖：** I1。
- **目标：** 稳定维护 `MappedReaction -> Node -> NodeGeometry -> Geometry <- Frame`，
  同时保留 Frame 原始坐标方向。
- **交付物：**
  - E(3) 等变 Geometry 去重和不唯一候选拒绝；
  - source atom order 到 Geometry atom order 的可验证映射；
  - 同一节点多个构象、同一 Geometry 多个 Protocol/Frame；
  - 创建 Reaction 时补链现有 Geometry，创建 Geometry 时反向补链现有 Reaction；
  - GeometryEnergyView 的版本化来源选择和完整 Frame 溯源。
- **阻断验收：**
  - 优化中间帧保留在 Geometry 的事实链，并在反应详情中可见；
  - 同一坐标不会重复展示，不同构象不会错误合并；
  - 振动和其他方向相关数组仍以 Frame 原始坐标为基准；
  - 一个 MappedReaction 可绑定多个 TS Geometry；
  - Gaussian/ORCA 跨软件同几何复用测试通过。

- **验收记录（2026-08-12）：** `20260812_0032` 保存 source atom order 到 Geometry atom
  order 的完整 permutation；Geometry 仍以 E(3)-不变内坐标去重，候选不唯一时拒绝并保存
  QC evidence。真实 TS 批次验证所有 Frame 全部入库；TS terminal Frame 豁免优化收敛阈值，
  但其 Geometry 只有具备至少一个真实热力学属性时才能绑定反应；非平凡 TS permutation、
  多个 TS Geometry、原始坐标方向和重复绑定均通过。Gaussian/ORCA water 单点跨软件复用
  一个 Geometry，同时保留两个 Protocol/Frame；
  GeometryEnergyView 按理论层级选出电子能，并以兼容的频率 Frame 提供热化学校正，来源
  Frame/Protocol ID 随视图返回而不持久化到反应表。
  后端 Ruff/Pyright/Mypy、前端 typecheck/build、Alembic check
  和 ERD 生成均通过。

  联合反应专项为 `24 passed`。随后已用 `.tmp` 的完整 DA-bench 快照替换无法恢复的旧
  fixture，并冻结 `000000000000 + 000000403256` 的 `conf_01/product_00` 路径；完整
  DA 数据库与 RustFS 验收归入 V1。

### I3 扩充领域过滤条件

- **状态：** `done`
- **依赖：** Q1-Q4 的查询 DTO。
- **交付物：**
  - TS inference：logical reaction、frame、虚频范围；
  - Frame：segment/frame index、frequency range、charge、multiplicity；
  - Protocol：software version、method family、solvation model；
  - ScientificArray：dtype、shape、payload hash；
  - Geometry：atom count、derivation、reaction node role；
  - Reaction：label、created-at、node role、TS/Geometry 数量。
- **阻断验收：**
  - 所有范围参数验证上下界；
  - 过滤条件可以组合且分页 total 正确；
  - 高频过滤条件有索引或有基准证明顺序扫描可接受。

- **验收记录（2026-08-12）：** TS inference、Frame、Protocol、ScientificArray、Geometry、
  LogicalReaction 和 MappedReaction 的清单字段均已通过 NexusX 自动注册到 REST/OpenAPI、
  GraphQL 和 MCP 共用 service；关联过滤使用 `EXISTS` 或相关标量子查询，不因一对多 join
  重复分页 total。真实 PostgreSQL 动态选取完整 TS 链的组合过滤、分页、protocol solvation
  样本和所有倒置范围/非法 shape 专项为 `15 passed`，NexusX schema/传输专项为
  `21 passed`。当前 815 个 Frame、2981 个 ScientificArray、695 个 Geometry 和 9 个
  Protocol 下，低选择性 metadata 查询 `EXPLAIN ANALYZE` 实测为 0.027–1.061 ms；回归测试
  对顺序扫描同时设置 250 ms 与关系规模上限，超限时要求新增选择性索引或更新基准。
  Ruff、Mypy、Pyright 和 Alembic check 均通过；纯查询/DTO 变更未创建迁移。

## 5. P1：API、安全和查询成本

### A1 统一 NexusX 传输面

- **状态：** `done`
- **依赖：** Q1-Q4。
- **目标：** 主 REST、分页 GraphQL、Direct-list GraphQL 和 MCP 暴露相同的
  白名单只读能力。
- **阻断验收：**
  - direct playground 补齐 Artifact 和 ArtifactIngestion 服务；
  - 四种入口使用同一 service 和 DTO，不复制查询逻辑；
  - schema snapshot、OpenAPI 路由和 MCP disclosure 测试通过。

### A2 查询成本和限流

- **状态：** `done`
- **依赖：** I3。
- **交付物：**
  - 数据库 statement timeout；
  - 分页、SMARTS/SMILES/reaction 输入和结构候选集上限；
  - GraphQL 深度/复杂度限制；
  - REST/MCP 限流和统一超限错误；
  - 慢查询日志及代表性 `EXPLAIN` 回归测试。
- **阻断验收：**
  - 禁止无条件大范围结构扫描；
  - Formula、RDKit GiST、Geometry 和 fingerprint 高频查询命中预期索引；
  - 超时或超限不会长期占用数据库连接；
  - 查询成本门在 CI 中可重复运行。

- **验收记录（2026-08-12）：** PostgreSQL engine 统一设置 15 秒 statement timeout，
  SQLSTATE `57014` 映射为稳定 `query_timeout`；50 ms 取消专项证明 rollback 后同一 session
  可继续复用连接。REST、GraphQL 和 MCP 共用结构输入、GraphQL AST 和固定窗口限流预算，
  分别使用 `query_budget_exceeded`、`query_rate_limit_exceeded` 和 `query_timeout` 稳定错误码；
  慢查询日志只记录 SQL 模板与耗时，不记录绑定值。未索引 descriptor/Murcko scaffold
  扫描按廉价预筛关系执行候选上限；SMARTS 和相似度阈值按 RDKit GiST 谓词后的实际候选
  计数，纯 Top-K 由 fingerprint GiST KNN 与 `limit <= 200` 限界。Formula 精确元素计数
  使用 `20260812_0033` generated token + GIN，数值范围语义仍以 118 维向量为准。
  传输/schema 专项 `25 passed`，Formula、Topology、Reaction、I3 过滤和查询成本数据库联合
  专项 `30 passed`；代表性计划命中 Formula GIN、Topology/Reaction mol/fingerprint GiST、
  Geometry topology 和 Frame derivation B-tree。Ruff、Mypy、Pyright、Alembic current/head/check
  均通过，查询成本测试已进入 `make test-db` 的 integration 测试路径。

### A3 查询授权闭环

- **状态：** `done`
- **依赖：** Q1-Q3。
- **交付物：**
  - 明确 Manifest、Frame、Geometry、Reaction 和 ScientificArray 的可见性继承；
  - ScientificArray 下载继承其 Frame/Artifact 项目权限；
  - MCP 复用与 HTTP 相同的 principal 和授权策略；
  - 对详情 ID 枚举的拒绝测试。
- **阻断验收：**
  - 私有项目对象不能通过列表、详情、反向查询或下载旁路访问；
  - 匿名/public 和 project 可见性的结果在所有传输面一致；
  - 权限失败使用稳定错误语义且不泄漏对象是否存在。

- **验收记录（2026-08-12）：** 所有 Artifact 派生读取统一使用请求级 visibility scope；
  Frame、Geometry、Reaction、ScientificArray、Protocol、revision/segment/inference、
  advanced result、Manifest/binding 和 topology derivation 均从 Artifact public/project
  权限继承可见性。混合来源 Reaction/Geometry 只投影当前 principal 可见的 Frame，完全
  私有对象在列表、详情和反向查询中均不可见；ScientificArray NPY、Artifact 内容和
  Geometry SDF 的私有 ID 与不存在 ID 使用相同 404 语义。认证用户仍可读取没有任何
  Artifact 来源的人工 Reaction；匿名 public Artifact 的列表和详情结果一致。REST、
  GraphQL、mounted/dedicated MCP 共用同一 principal、service 和 DTO，MCP 不重复认证且
  限流按 user ID 计数。误加到全局 Storage GC 查询的 topology derivation 谓词已移除。
  授权专项 `7 passed`，数据库查询联合回归 `33 passed`，相关单元/传输专项
  `28 passed`；Ruff、Mypy、Pyright 和 Alembic check 均通过，无 schema 变更。

## 6. P2：测试、里程碑与生产准备

### F：前端逻辑重构

- **状态：** `partial`
- **依赖：** A3、V1；项目/成员管理页面另依赖后端管理 API。
- **权威计划：** [前端重构计划](frontend-refactor-plan.md)
- **目标：** 将当前 `App.vue` 全局状态改为 Vue Router、服务端 Query cache、统一用户/项目上下文和独立上传队列。
- **第一阶段交付物：** 会话 composable、项目切换器、路由守卫、Geometry 垂直切片、服务端分页和权限矩阵测试。
- **完成条件：** F0-F8 的代码、后端契约、前端构建、Playwright、权限、项目隔离和上传部分失败门禁全部通过；未提供后端成员 CRUD 前不得标记项目管理 UI 完成。

**执行记录（2026-08-14）：** 已落地 F1 的第一批基础设施：Vue Router 深链接和旧 `/` 入口
兼容重定向、TanStack Vue Query 客户端、`useSession`（`/api/auth/me` 唯一会话来源）、
项目选择器/`useProjectContext`、受保护路由守卫以及匿名 Artifact 降级页。Reaction、
CalculationFrame 和 Artifact 列表请求已移除前端固定 `limit=200`，默认使用服务端分页窗口；
Reaction/Frame 列表 API 接受项目上下文并复用可见性范围。Geometry 垂直切片随后完成；独立
上传队列、账户/项目正式页面和权限/Playwright 矩阵仍未完成，故保持 `partial`。
本地 PostgreSQL/RustFS 与 API 启动后，Playwright 现有 4 项通过 3 项（反应桌面、移动端
无溢出、Geometry 深链接、认证上传）；匿名 Artifact 用例因当前数据库没有 `visibility=public` 样本而阻断，
不是前端请求或路由错误。

**补充执行记录（2026-08-14）：** F2 Geometry 垂直切片已接入独立列表/详情 Query、项目
上下文过滤、服务端分页、SDF 三维画布、GeometryEnergyView 能量来源和 CalculationFrame /
Protocol 来源表。真实 API 验证了列表、详情（含 2 个来源 Frame）和 SDF；Playwright 深链接
回归覆盖桌面端详情、非空 WebGL 画布和页面宽度。另修复了应用初始路由尚未 ready 时项目
上下文 query watcher 把 `/geometries` 误导航到 `/reactions` 的竞态。F3-F8 及公开 Artifact
匿名样本门禁仍未完成，F 总状态继续保持 `partial`。

**补充执行记录（2026-08-14，第二批）：** Reaction、MappedReaction、CalculationFrame、
Artifact 和结构搜索均已改为路由驱动的按需 Query；目录查询的 `project_id`、`limit` 和
`offset` 已进入 Query key，Reaction/Frame/Artifact 均提供服务端分页控件。项目切换会清除
前一个项目范围的缓存、详情和抽屉状态；应用刷新与上传完成只失效当前项目的目录、Geometry、
拓扑搜索和概览 Query，不再执行跨项目 `catalog` 全量刷新。上传队列以最多三个并发请求处理
文件级成功、失败、重试和取消，且项目切换会中止未完成任务。`/account`、`/projects` 和项目
详情只使用 `/api/auth/me`，无权项目 URL 被守卫重定向；成员页继续保持只读占位，等待基础版
项目/成员 API。生产注册/登录继续由外部 OIDC 负责，本地只建立授权用户和外部身份映射；不做
本地密码或复杂组织运营后台。

**本轮验收（2026-08-14）：** `npm run build` 通过；Chromium Playwright `8 passed`，覆盖
桌面/移动布局、Reaction/Frame/Geometry 深链接、服务器分页、账户与无权项目路由、匿名公开
Artifact 预览/下载链接、上传部分失败重试和取消；后端认证、授权、GraphQL/OpenAPI 契约、能量
profile 和前端边界专项 `28 passed`。GraphQL 为详情/列表/Topology 搜索保留可选 `project_id`
参数，旧调用可省略它。数据库/RustFS 的匿名 Artifact 授权集成测试仍依赖显式集成环境标志，
本轮在默认环境中为预期 skip；F7 后端管理 CRUD 及 F8 全量发布门禁尚未完成，因此 F 继续为
`partial`。

**补充执行记录（2026-08-14，后端管理闭环）：** 新增受控用户目录与账号启停、项目创建、
详情、重命名/归档恢复，以及已有本地用户的成员列表、添加、角色修改和移除 API；项目行锁
串行化成员写入，最后一名
Manager 不能被降级或移除。新增 `artifact:delete` 和 Artifact 退役 DELETE：数据库保留
`retired` tombstone，RustFS 对象经 hash/大小校验后删除，所有读取和派生事实可见性排除退役
来源；同项目、同类型、同内容的重复上传恢复原 tombstone 和解析历史。项目 manager 还可修改显示文件名和 `public/project` 可见性，
而不改变内容 identity。真实 PostgreSQL/RustFS 管理、删除、访问和 GC 专项 `6 passed`；
邀请状态机、组织访问列表和管理操作审计已实现；成员分页仍是后续优化项。

### V1 恢复完整测试基线

- **状态：** `done`
- **当前检查点：** 缺失的旧 DA-bench fixture 已由可复现的完整 v3 子集替换；默认测试、
  PostgreSQL/RustFS、seed 幂等和静态门禁已完成。固定 digest 的 PostgreSQL/RustFS service、
  fixture validator、DA seed 和前端 E2E 已进入独立 CI job；首次远端 workflow 结果仍应随
  pull request 保存，不以本地检查代替。
- **交付物：**
  - 补齐并校验 fixture SHA-256，或将其拆成有明确获取方式的可选测试包；
  - 更新 fixture manifest 与当前 MolOP 导出统计；
  - PostgreSQL、RustFS、MolOP 和前端端到端测试进入 CI。
- **阻断验收：**
  - 默认测试没有由缺失本地文件导致的失败；
  - 启用数据库和 RustFS 标志后的完整集成测试通过；
  - skip 必须对应明确、可复现的外部条件。

#### V1 持久化执行队列

本队列是 V1 的唯一恢复点。任务状态、命令结果和新增阻塞项必须在完成当次工作前
回写本节；不得以聊天记录、终端滚动输出或未提交的临时笔记作为唯一状态来源。每项
只有实现、针对性测试、相关完整门禁和本节验收记录同时满足后才可标记为 `done`。

| ID | 状态 | 工作项 | 依赖 | 完成条件 |
| --- | --- | --- | --- | --- |
| V1-01 | `done` | 修正 `MappedReactionNodeGeometry` 的复用身份：优先按 `(node, geometry, mapped participant)` 查找既有绑定，不能因调用方给出的 `coordinate_index` 与既有构象槽不同而报冲突。 | I2 | 同一节点、同一 Geometry、同一 participant 在不同 coordinate slot 请求时只复用一条 binding；不同 Geometry 或 participant 仍被拒绝；被策展的 terminal Geometry 可按明确规则提升为 primary，且不复制构象。 |
| V1-02 | `done` | Geometry 层 atom-map mapping 只保存 canonical Geometry atom order，和 Frame source order 分离。 | V1-01 | `geometry_atom_map_numbers` 和 `mapped_smiles` 相同的 Geometry mapping 可跨 source permutation 复用；Frame 的 `observed_to_geometry_atom_indices`、原始 Cartesian 坐标和方向相关数组保持不变。 |
| V1-03 | `done` | 为 V1-01/V1-02 增加真实 PostgreSQL 集成回归：多构象、primary 提升、source permutation 和重复 seed。 | V1-01, V1-02 | `tests/integration/test_reaction_command.py` 覆盖同一 participant 的两个 Geometry、coordinate slot 冲突、primary promotion 和 mapping 复用；`tests/integration/test_artifact_upload.py` 覆盖非 identity source permutation；`tests/integration/test_da_bench_seed.py` 覆盖 PostgreSQL/RustFS 双跑和稳定 object key。 |
| V1-04 | `done` | 在隔离开发数据库完成 DA-bench seed 双跑，并把可复现结果记录到本节。 | V1-03 | 连续两次 `uv run tricycle-seed-da-bench` 均成功；第二次返回的所有新增计数为零。验证使用命名隔离数据库和 bucket，没有清理用户工作库。 |
| V1-05 | `done` | 重新运行 V1 后端完整门禁，按通过/失败/跳过/环境阻塞记录实际结果。 | V1-04 | 默认测试、数据库/RustFS integration、静态检查、Redis 真实 backend、供应链审计和 `alembic check` 的结果已在本节记录；E2E fixture 缺失时单独标记环境阻塞。 |
| V1-06 | `done` | 建立 CI，并纳入前端 build/e2e 与可启动的 PostgreSQL/RustFS 集成门禁。 | V1-05 | CI 不依赖本机 `.tmp` 之外的未声明文件；fixture 完整性、后端默认测试、前端 build/e2e 及可复现的数据库/RustFS 集成测试均有独立 job。 |

**当前恢复检查点（2026-08-12）：** fixture 已冻结为 9 segments、45 frames、227 arrays，
其再生目录字节比较一致；默认测试曾通过 `161 passed, 73 skipped`，完整 PostgreSQL
集成曾通过 `70 passed, 5 skipped`，RustFS 专项曾通过 `5 passed`。当前 `alembic current`
为 `0001_initial_schema (head)`，`alembic check` 无 upgrade operation。历史检查点中的 V1-01 尚未完成，
`uv run tricycle-seed-da-bench` 在
`application/services/reactions.py:persist_mapped_reaction_node_geometry` 以
`ValueError: node coordinate identity resolved to different source objects` 停止：自动反应
补链已占用某个 coordinate slot，但待策展 Geometry 已在同一 participant 的另一 slot 绑定。

**恢复顺序（历史记录）：** 先完成 V1-01 的服务层修正和单元/集成用例，再处理 V1-02，随后执行
V1-03 的 seed 幂等断言。该顺序已于下方的 2026-08-19/20 验收记录完成；不得把一次 seed 成功
等同于幂等验收。

**V1 验收记录（2026-08-19 至 2026-08-20）：** 在隔离数据库
`tricycle_refactor_v1_20260819` 和 bucket `tricycle-refactor-v1-20260819` 上，先执行
`alembic upgrade head`，再执行 `tricycle-bootstrap --mode development` 后完成 seed；首次
bootstrap 前直接 seed 的外键失败被记录为初始化顺序错误，未计入 seed 回归失败。连续两次
`tricycle-seed-da-bench` 返回的 ID、行数和 frame/array counts 完全一致：4 artifacts、45
frames、35 geometries、227 scientific arrays、4 node-geometry bindings、4 mapping bindings、
1 logical reaction、1 mapped reaction 和 1 edge。RustFS `raw` 前缀始终为 4 个 object key，
第二次没有新增 key 或版本。`tests/integration/test_da_bench_seed.py` 与
`tests/integration/test_reaction_command.py` 的真实 PostgreSQL/RustFS 专项为 `11 passed`；
其中反应测试验证两个不同 Geometry 构象、coordinate slot 仅作展示槽、primary 提升不复制
binding，artifact-upload 回归验证非 identity source permutation 的 canonical mapping。

同一批门禁结果：`make check` 的默认 pytest 为 `291 passed, 112 skipped`；`make test-infra`
为 `111 passed, 2 skipped, 290 deselected`；临时 Redis 6.0.16 实例上的 `make test-redis`
为 `2 passed`；`make audit`、`make frontend-check`、`make frontend-build`、`uv run --frozen
alembic check` 和 `uv lock --check` 均通过。默认 `make frontend-test-e2e` 在当前 API 指向的
空/非 DA fixture 数据库上为 `27 passed, 18 failed`，失败均为缺少 mapped reaction、多帧
artifact 等固定数据；之前使用隔离 API + 完整 fixture 的 `45 passed` 证据仍有效，但不能把
本次默认库结果当作 E2E 通过。V1-06 已在 `.github/workflows/ci.yml` 建立独立 lint、type、
默认 pytest、Alembic fresh database、PostgreSQL/RDKit、RustFS、frontend build、Playwright、
Redis、operations config 和 audit job。Playwright job 在 DA seed 前执行只读 fixture validator；
manifest v3 记录 4 个日志的压缩/解压后 SHA-256 与 source size，以及 3 个 metadata JSON 的
SHA-256 与 size。2026-08-20 本地 validator 输出 `logs=4, metadata_files=3`，validator/CI
契约专项 `12 passed`，全树 `make lint type test` 为 `299 passed, 112 skipped`，`uv lock
--check` 通过。远端 GitHub Actions 的首次执行结果仍需在 pull request 中留存。

### V2 完成 M4/M5 验收

- **状态：** `partial`
- **依赖：** I1、I2、I3、A2、V1。
- **阻断验收：**
  - 黄金语料可以直接批量上传并完整入库；
  - 重复运行零新增，失败文件不污染成功文件；
  - 路径、节点角色、构象和协议层级与人工标注一致；
  - 复合能和势垒可解释、可独立复算并记录来源；
  - 代表性查询没有意外全表扫描。

### V3 M7 试运行和 v1 发布

- **状态：** `todo`
- **依赖：** V2、A3。
- **交付物：**
  - 真实环加成数据试导入和抽样科学复核；
  - 生产部署、监控、告警和容量基准；
  - PostgreSQL/RustFS 协调备份恢复演练；
  - Alembic 升级、不可逆迁移和 MolOP 重解析手册；
  - SBOM、镜像签名和构建来源审计。
- **阻断验收：**
  - 全新环境可按文档重建；
  - 完成一次可核验的备份恢复演练；
  - 科学数据、反应路径、能量和 provenance 抽样复核通过；
  - 生产安全、性能和运维门禁全部通过。

## 7. 执行顺序

```text
Q1 -> Q2/Q3/Q4 -> I1 -> I2 -> I3 -> A1 -> A2/A3 -> V1 -> V2 -> V3
```

Q2、Q3 和 Q4 可以并行；V1 的 fixture 补齐也可以与查询开发并行。任何阶段发现新的
schema 缺口时，应先更新本清单，再决定是否需要 Alembic revision；开发阶段不为纯查询
或 DTO 变更创建迁移。

## 8. 通用验收命令

每个目标至少运行与改动范围匹配的测试；阶段完成前运行完整门禁：

```bash
uv run ruff check src tests migrations scripts
uv run ruff format --check src tests migrations scripts
uv run mypy src scripts
uv run pyright src scripts
uv run alembic check
uv run pytest -q
TRICYCLE_RUN_DATABASE_TESTS=1 uv run pytest -q tests/integration
```

涉及 RustFS 时额外设置 `TRICYCLE_RUN_RUSTFS_TESTS=1`，涉及前端时运行：

```bash
make frontend-build
make frontend-test-e2e
```

测试报告必须区分通过、失败、跳过、环境阻塞和未运行，不得把默认跳过的集成测试计为
已经验证。

## 9. 验证记录

### 2026-08-12：Q1-Q4、A1 与 I2

- `ruff check`、`ruff format --check` 覆盖 `src tests migrations scripts`，通过；
  `mypy src scripts` 与 `pyright src scripts` 通过。
- `alembic check` 通过，无新增 upgrade operation；RDKit `reaction` 类型仍产生已知的
  autogenerate 类型识别警告。
- Q1-Q4、Formula、Topology 和 Reaction 真实 PostgreSQL/RDKit 专项测试共
  `13 passed`，无跳过。当前库实际覆盖 134 个 orbital Frame、140 个 population Frame、
  140 个 polarizability Frame 和 6 个 electronic-state Frame；Q1 验收指定的四类现有
  数据均被读取。bond order、NMR、total spin、single-point properties、multireference
  和 implicit solvation 当前库无记录，其 DTO/查询分支已实现，但仍需随 I1 黄金样本补充
  真实数据回归。
- REST/OpenAPI、分页 GraphQL、direct GraphQL 和 MCP 自动注册/披露专项测试
  `21 passed`；direct playground 包含主配置全部服务，并已补齐 Artifact 与
  ArtifactIngestion。
- 数据库专项测试产生 SQLModel `AsyncSession.execute()` deprecation warning；不影响当前
  查询结果，后续查询层维护改用 `exec()` 时清理。
