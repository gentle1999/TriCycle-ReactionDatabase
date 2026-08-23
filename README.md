# Example Chemistry Database

用于存储和查询环加成反应路径节点计算数据的专用数据库。

Geometry 是与量化软件和势能面无关的核坐标对象。Gaussian、ORCA 或其他 MolOP
可识别后端产生的 CalculationFrame 可以通过拓扑、原子顺序和坐标证据复用同一
Geometry；软件、方法、基组、电荷、多重度和电子态保留在 Protocol/Frame。
所有有效优化中间帧仍完整保存并连接逐帧重建的实际分子拓扑。

> 当前状态：M1-M3 已完成；已接入 PyPI 发布版 MolOP/MolGR、完成 Gaussian DA 原型录入，
> 并实现 M6 的只读查询原型。

## v1 目标范围

- 使用已有 molecular topology 创建或复用逻辑反应和 mapped reaction。
- 使用 MolOP probe 自动识别并解析原始量化计算文件。
- 使用 RustFS 保存不可变原始日志和输入 artifact。
- 使用 PostgreSQL、RDKit cartridge 和 MolAlchemy 存储与检索化学结构。
- 使用 SQLModel/Pydantic 建模，使用 FastAPI 和 NexusX 提供 API、GraphQL 与 MCP。
- 保存原始计算溯源、版本化质量检查和复合能/活化能等派生结果。

项目暂不覆盖实验条件、产率、ee/dr、文献数据、HPC 任务调度或通用机理发现。

## 核心架构

```text
化学主轴: MolecularFormula -> MolecularTopology -> Geometry
物理主轴: RustFS -> ArtifactFile -> ParseRevision -> Segment -> Frame -> Geometry
反应主轴:
  LogicalReaction -> LogicalReactionParticipant -> MolecularTopology
       -> MappedReaction(mapped rxn_smiles) -> MappedReactionNode
坐标绑定:
  MappedReactionNode -> NodeGeometry -> Geometry <- CalculationFrame
  Geometry -> request-visible GeometryEnergyView

MolOP ingestion -> normalization/QC -> SQLModel/MolAlchemy -> PostgreSQL/RDKit
PostgreSQL/RDKit -> application services -> FastAPI + NexusX
```

Topology 图优先使用 MolOP 导出的 MolGR 分子图，不从文件名推断。认证用户上传计算
文件后，系统统一用 MolOP 拆分全部 segment/frame，并保存每帧坐标、拓扑和计算结果；
其中只对 MolOP 判定为 `is_TS=True` 的帧调用 `possible_pre_post_ts()`，由 MolOP 沿
虚频模式对正负两侧独立采样振幅并保留每侧最稳定的拓扑来确定前后体；项目只测量被
选中端点的实际位移并创建或复用同一反应，创建方式只记录在 TS 推断溯源中，不形成
另一类反应。
上传支持一次选择多个原始文件。每个文件独立保存和解析，不依赖文件名、目录结构或
manifest；坐标相同的帧按版本化 RMSD/最大偏差策略复用 Geometry。TS 帧推断反应后，
会补齐已存在 Geometry 的节点关系，后续上传的匹配 Geometry 也会反向补链。反应详情
通过 Geometry 展开全部可见 CalculationFrame；电子能和热化学校正由 GeometryEnergyView
在查询时按计算层级和物理上下文选择并标记来源。

## 文档

- [技术方案与实施路线图](docs/technical-roadmap.md)
- [实施目标清单](docs/implementation-backlog.md)
- [项目重构与上线计划](docs/refactor-plan.md)
- [数据模型与存储边界](docs/data-model.md)
- [字段级业务模型](docs/business-model.md)
- [数据库实体关系图](docs/database-erd.md)
- [MolOP 计算结果导出需求](docs/molop-export-requirements.md)
- [开发环境](docs/development.md)
- [部署与配置指南](docs/deployment-configuration.md)
- [生产运维与恢复 Runbook](docs/operations-runbook.md)
- [RDKit Mol 对象数据库往返契约](docs/rdkit-mol-roundtrip.md)

三级化学存储、RustFS 文件主轴、逐帧实际 topology 和矩阵 ORM 映射已经确定。
`20260713_0002` 已实现 Formula、Topology、Geometry、Artifact 和 CalculationProtocol；
`20260713_0003` 已实现 ParseRevision、Segment、Frame、ScientificArray 和
ThermochemistryResult；`20260713_0004` 已实现 WorkflowManifest、ArtifactBinding、
Reaction/Participant、Path/Node/Edge 原型，并使用真实 DA
`ene/diene/ts/prod` 子集完成数据库往返。`20260714_0005` 已接入 MolOP，
将 Energies、GeometryOptimizationStatus、Vibrations、ThermalInformations 和 Status
映射为 Frame 子结果表，并删除数据库侧重复的 Gaussian source scanner。
`20260715_0006` 至 `0008` 实现 Node 多坐标和显式 atom-map 转换；`20260715_0009`
将反应轴规范为 `LogicalReaction -> MappedReaction -> Node -> Geometry`，并为 Geometry
增加规范 atom order 的单 conformer RDKit Mol。复合性质在查询层按版本化政策
临时计算。`20260716_0010` 对齐 MolOP 的通用 population、轨道、极化率、NMR、键级、
电子态和多参考结果模型，并用显式 array assignment 保存矩阵语义。`20260809_0011`
将解析溯源切换为 PyPI package version + 公共 parser provenance，Git commit 仅作为历史
开发构建的可选证据，并补齐 `0010` 新增 ScientificArray kind 所需的列宽。
`20260809_0012` 将 topology identity 与 reconstruction derivation 分离，
逐 frame 绑定实际 derivation，并保存源坐标的可选打印精度。`20260809_0013` 增加
用户、OIDC 外部身份、组织、项目和成员关系；Artifact 显式记录所属项目、创建者与
`public/project` 可见性。
`20260809_0014` 增加 ArtifactIngestion 和 TransitionStateInference；
`20260810_0015` 至 `0017` 增加按 bucket/prefix 持久化的 RustFS 增量 GC 水位、运行审计
和 pending 补偿索引；`20260810_0018` 至 `20260811_0020` 增加压缩源身份、软件无关的
Geometry 匹配证据、多文件上传，以及通用 manifest 几何来源。
`20260811_0021` 至 `0029` 增加自动反应命名、Formula 元素向量、反应帧资格约束、
E(3)-不变 Geometry/帧原始坐标、六位 Hartree 精度及 Topology/Reaction RDKit 索引；
`20260812_0030` 至 `0032` 将 TS inference 归属 ParseRevision，增加显式 reparse revision
序号与前驱链，并保存 source atom order 到 Geometry order 的 permutation；
`20260812_0033` 为 Formula 的常见精确元素计数增加 generated token 和 GIN 索引，同时
保留 118 维元素数量向量作为权威范围查询数据。
统一上传先在 PostgreSQL 建立 pending 关系，再按 UTC 小时分区写入 RustFS 并校验为
available；失败时生命周期 Hook 立即定点清理本次对象，定期增量 GC 作为可选崩溃恢复
安全网。计算输出再完整录入全部 MolOP 帧；检出 TS 时按 topology + atom mapping
幂等复用反应，并把 TS CalculationFrame 作为推断来源关联到该反应。

## 快速启动

完整容器部署只要求 Docker 和 Docker Compose；宿主机直接开发另外要求 `uv`、Node.js 20+
和 npm。

当前 Compose 使用明确版本 tag 的 PostgreSQL/RDKit、RustFS 和本地开发 Keycloak；tag 更新后应在
部署环境重新运行对象往返测试。生产环境如需可复现供应链，可通过部署平台或镜像签名策略额外锁定
digest，不要求把 digest 写进开发 Compose。
默认开启 RustFS 磁盘层对象压缩
（`RUSTFS_COMPRESSION_ENABLED=true`）；S3 GET/HEAD 和 Artifact SHA-256 仍对应原始逻辑字节。

完整容器栈还包含 API、前端静态服务和同源 HTTPS Caddy。首次本地启动可直接运行：

```bash
cp .env.example .env
make stack-up
curl --insecure https://localhost/health/ready
```

本地 `localhost` 使用 Caddy 内置 CA；Caddy 的 ACME 账户、证书和续期状态保存在持久化
`caddy-data`/`caddy-config` volume。正式部署应把 `CADDY_SERVER_NAME` 设置为实际 DNS 名称，
并确保 80/443 可达以便 Caddy 自动申请和续期证书。HTTP 端口只做 308 HTTPS 跳转，API 和前端
容器不直接发布宿主机端口。生产模式仍必须按
[部署与配置指南](docs/deployment-configuration.md)提供外部 OIDC、SMTP、Redis TLS 以及带 TLS
校验的 PostgreSQL/RustFS endpoint。

```bash
uv sync --python 3.12
npm --prefix frontend ci
make infra-up
uv run alembic upgrade head
make bootstrap-development
make seed-da-bench
make test-infra
make frontend-build
uv run tricycle-api
```

仓库中的 `Example Chemistry Database`、组织名和项目名都是开发占位默认值。后端显示名通过
`TRICYCLE_APP_NAME`、`TRICYCLE_BRAND_NAME`、`TRICYCLE_MCP_SERVER_NAME` 和
`TRICYCLE_NEXUSX_*` 覆盖；前端在构建时通过 `VITE_APP_NAME`、`VITE_BRAND_NAME`、
`VITE_APP_TAGLINE` 和 `VITE_MCP_SERVER_NAME` 覆盖。若自定义 CSRF Cookie/header 名，后端的
`TRICYCLE_CSRF_*` 与构建时的 `VITE_CSRF_*` 必须成对设置。`tricycle_reaction_db` 包名、
`tricycle-*` CLI 名和 `TRICYCLE_*` 环境变量前缀是兼容性标识，不是部署品牌。迁移只建 schema，新库必须显式执行
development 或 production bootstrap，详见[部署与配置指南](docs/deployment-configuration.md)。

默认本地端点：PostgreSQL `127.0.0.1:5432`、RustFS S3 API
`http://127.0.0.1:19000`、RustFS Console <http://127.0.0.1:19001>、Keycloak
<http://127.0.0.1:8080>。

生产不要求这些组件同机部署，也不要求 PostgreSQL、RustFS、Redis、OIDC 或 SMTP 各自只有
一个节点。[部署与配置指南](docs/deployment-configuration.md)给出了 EDGE/API 与多节点后端分布
在不同主机时的逻辑 endpoint、防火墙、TLS、upstream 和上线检查；可解析的 API 节点模板见
[`infra/deployment/multi-host-api.env.example`](infra/deployment/multi-host-api.env.example)。
Voyager 使用 NexusX 6.1.2 的 member cluster/color 展示数据库归属；标签和颜色通过
`TRICYCLE_NEXUSX_DATABASE_CLUSTER_*` 覆盖，多台 PostgreSQL HA 节点仍属于同一个逻辑 member。

API 启动后，浏览器从前端同源入口访问增强能力：

- NexusX 入口页：<http://127.0.0.1:5173/nexusx>
- 项目 API 文档：<http://127.0.0.1:5173/docs>
- Direct-list GraphQL（只读直接列表，适合探索字段）：<http://127.0.0.1:5173/nexusx/graphql>
- Paginated GraphQL（`items + page`，适合筛选、导出和翻页）：<http://127.0.0.1:5173/nexusx/paginated-graphql>
- MCP Streamable HTTP：`http://127.0.0.1:5173/nexusx/mcp/`
- Voyager：<http://127.0.0.1:5173/nexusx/voyager/>

MCP 入口页内置 Claude Desktop、Cursor、Cline、Windsurf、VS Code/Copilot 和 Claude Code
的配置片段；选择客户端后可一键复制对应 JSON 或命令。开发环境直接使用代理 URL，生产环境
请在登录后的 NexusX 页面点击“生成 Token”，再复制包含
`Authorization: Bearer <token>` 的客户端配置。Token 原文只在生成时返回一次；账户页可查看
有效 Token 的名称和到期时间并撤销。也可以直接调用 `POST /api/auth/mcp-tokens` 创建、
`GET /api/auth/mcp-tokens` 查看元数据、`DELETE /api/auth/mcp-tokens/{id}` 撤销。该 token
只用于 `/mcp/`，不会扩大 REST/GraphQL 的访问范围。

`make serve-frontend` 会将这些路径代理到组合 API（默认 `127.0.0.1:8000`）。入口页和 GraphiQL
编辑器均预填可运行的查询：直接列表使用 `GraphQLCatalogService`，分页查询使用
`ArtifactQueryService`。点击 `Execute Query` 执行，右上角 `Docs` 可选择其他字段；Compose
GraphQL 不支持 variables，参数需要直接写在查询文本中。
`make serve-nexusx` 仅启动 GraphQL、分页 GraphQL、MCP 和 Voyager 的独立演示进程；这些
端口应保持在内网或 loopback。Core API 与 UseCase REST 已包含在组合 API 的 `/docs`，不再
由该命令重复启动。需要拆分上游时，在 Vite/Caddy 中配置对应的
`NEXUSX_*_PROXY_TARGET`，不要把多个后端端口直接暴露给浏览器。

FastAPI REST、GraphQL 和 MCP 共用白名单 `UseCaseService`，当前开放 artifact、
molecular formula、molecular topology、logical reaction、mapped reaction 和
calculation frame 查询，以及高级计算结果、Workflow Manifest、Storage GC 审计和
topology derivation provenance 查询。显式写入面包括 `create_reaction` mutation、统一
Artifact 上传/重解析、项目与现有用户成员管理，以及 Artifact 退役删除；不暴露通用
ORM CRUD。
这些查询由 NexusX 自动注册到 GraphQL、UseCase REST 与 MCP。接口不暴露
RDKit `Mol`、矩阵载荷、RustFS 凭据或通用
ORM CRUD。公开 Artifact 支持匿名列表、预览和下载；其他接口要求认证，生产环境只接受
外部 OIDC/JWT。统一单文件、批量、validate 和 reparse 上传生命周期要求项目
`artifact:upload` 权限；普通重复上传复用科学 identity，显式 reparse 创建可审计的下一
ParseRevision，失败 reparse 不覆盖既有成功汇总。`DELETE /api/artifacts/{artifact_id}`
要求 `artifact:delete`，保留 PostgreSQL tombstone 和科学溯源并移除 RustFS 对象；退役内容
不会继续出现在目录、详情、下载或派生事实查询中。同一项目以相同 Artifact 类型重新上传
完全相同的 bytes 时恢复原 tombstone、RustFS 对象和既有科学历史，不创建重复记录。
项目 manager 可通过 `PATCH /api/artifacts/{artifact_id}` 修改显示文件名和
`public/project` 可见性，但不能替换文件 bytes。`/api/users` 提供受控用户目录：项目
manager 只能为目标项目搜索已完成 OIDC 首次登录的活跃用户，system organization
owner/admin 才能全局查询和启停账号。
查询入口已启用 statement timeout、结构输入和候选集预算、GraphQL 深度/复杂度限制、
REST/MCP 限流及稳定超限错误；生产启用前仍需完成跨传输查询授权闭环和完整测试基线。

数据浏览器使用 Vue 3 + Vite 构建，ChemDoodle Web Components 11.0.0 负责本地
分子画布。反应式、映射路径节点、RDKit 分子结构、逐帧能量/优化/频率信息和原始
文件索引均来自 Core REST；带 Geometry 的分子由后端从 PostgreSQL RDKit `mol` 生成 SDF，
前端使用 ChemDoodle TransformCanvas3D 的增强 Wireframe 展示三维构象和键级，不在浏览器
重新从 SMILES 构造坐标。矩阵仅显示类型、shape、dtype、大小和哈希元数据。

开发时可单独启动 Vite：

```bash
make serve-frontend
```

浏览器访问 <http://127.0.0.1:5173/>；Vite 会将 `/api`、`/health`、`/docs` 和
`/nexusx/*` 代理到组合 FastAPI。`VITE_API_PROXY_TARGET` 可覆盖开发代理地址。发布构建使用
`make frontend-build`，输出到独立的 `frontend/dist`，由静态服务器或 CDN 部署；
生产反向代理需将上述后端路径转发到 FastAPI。

## 许可证

本项目代码采用 [MIT License](LICENSE)。ChemDoodle Web Components 文件采用上游
GPLv3 或相应商业许可，详见
[`frontend/public/vendor/chemdoodle/COPYING.txt`](frontend/public/vendor/chemdoodle/COPYING.txt)。
