# TriCycle Reaction Database

[English](README.md) | [简体中文](README.zh-CN.md) | [Documentation / 文档](docs/README.md)

TriCycle Reaction Database 是面向量子化学反应路径计算的拓扑优先数据库与 Web 应用。它导入
计算 artifact，保留原始溯源，重建分子图，存储可复用几何构象，并通过 Web UI、REST、
GraphQL 和 MCP 提供反应、结构及计算数据。

项目仍处于 1.0 前的活跃开发阶段。数据库迁移、API 行为和导入策略都有版本与测试；下游集成
应固定发布版本，不应假定未版本化的 `main` 分支稳定。

## 主要能力

- 导入 MolOP 支持的 Gaussian、ORCA 等计算输出，并将原始字节保存到兼容 S3 的 RustFS。
- 持久化每个可恢复 segment 和计算 frame 的 parser、软件、方法、坐标、频率、能量和数组溯源。
- 经 MolGR 按 frame 重建分子图；拓扑来自重建结果，不从文件名或目录推断。
- 去重复用 formula、topology 与 geometry。Geometry 身份包含 topology、坐标、总电荷和
  自旋多重度；一个计算 frame 内的源原子对应关系保持不变。
- 从虚频推断合格的过渡态端点，保留推断证据，并连接到 logical/mapped reaction 路径；不会
  自动指定任何反应类别。
- 查询与浏览反应、mapped reaction、topology、geometry、calculation frame 和 artifact；
  结构查询受输入、候选数、超时与限流预算保护。
- 在浏览器检查三维 Geometry，并下载带电荷/多重度元数据的 Geometry 专属 XYZ 与 SDF。
- 支持项目范围授权、开发身份、生产 OIDC 登录、邀请加入成员和独立 MCP access token。

## 范围

TriCycle 保存和提供计算化学记录。它不是 HPC 调度器、通用反应发现引擎、电子实验记录本或
文献/产率数据库，也不会为了匹配预设反应标签而改变计算产生的化学事实。

## 架构

```text
计算文件
  -> ArtifactFile (PostgreSQL) + 按内容寻址的原始对象 (RustFS)
  -> ParseRevision -> CalculationSegment -> CalculationFrame
  -> MolecularFormula -> MolecularTopology -> Geometry
  -> TS 端点推断 -> LogicalReaction -> MappedReaction -> 路径节点

Vue 3 应用 <-> FastAPI / NexusX <-> PostgreSQL + RDKit cartridge
                                      \-> RustFS（原始 artifact）
```

化学身份轴为 `MolecularFormula -> MolecularTopology -> Geometry`，溯源轴为
`ArtifactFile -> ParseRevision -> CalculationSegment -> CalculationFrame`。它们在 frame
对 Geometry 的绑定处相交，从而同时保留可复用化学事实和观察到这些事实的计算 frame。

## 技术栈

| 层 | 组件 |
| --- | --- |
| 后端 | Python 3.12、FastAPI、SQLModel、SQLAlchemy、Pydantic、Alembic |
| 化学 | MolOP、MolGR、RDKit、RDKit PostgreSQL cartridge、MolAlchemy |
| 存储 | PostgreSQL 18 + RDKit、RustFS S3-compatible object storage |
| 前端 | Vue 3、Vite、Vue Router、TanStack Vue Query、ChemDoodle |
| 查询传输 | REST/OpenAPI、NexusX GraphQL、MCP Streamable HTTP、Voyager |
| 运维 | Docker Compose、Caddy、Prometheus metrics、GitHub Actions |

## 快速开始

### 前置条件

- Python 3.12 和 [uv](https://docs.astral.sh/uv/)
- Node.js 20 或更高版本以及 npm
- Docker Engine 与 Docker Compose，用于本地 PostgreSQL/RDKit、RustFS 和开发 Keycloak

克隆仓库后安装锁定依赖：

```bash
uv sync --python 3.12
npm --prefix frontend ci
```

启动本地基础设施、执行迁移、创建开发组织/项目/用户，并在宿主机上运行支持热更新的 API 和
Vite 前端：

```bash
make dev-stack
```

打开 <http://127.0.0.1:5173/>。Vite 开发服务会将 `/api`、`/health`、`/docs`、`/graphql`
和 `/nexusx/*` 代理到宿主机 `8000` 端口的 API。`Ctrl-C` 只停止 API 和前端；基础设施容器和
数据卷保持运行。

若基础设施已经运行，使用 `make dev`。常用生命周期命令为：

```bash
make infra-up                 # PostgreSQL/RDKit、RustFS、开发 Keycloak
make migrate                  # Alembic upgrade head
make bootstrap-development    # 开发组织、项目和用户
make dev                      # 宿主机 API + Vite 热更新
make infra-down               # 停止本地基础设施
```

### 完整 Compose 栈

如需运行打包的 API、前端和 Caddy，而不是宿主机开发服务：

```bash
cp .env.example .env
make stack-up
curl --insecure https://localhost/health/ready
```

`localhost` 使用 Caddy 的内部开发 CA。生产环境必须使用外部 OIDC、经过 TLS 验证的
PostgreSQL/RustFS endpoint、secret 管理与部署专属品牌配置；不要将开发 `.env` 值直接提升到
生产，请遵循[部署指南](docs/deployment-configuration.md)。

## 使用应用

浏览器 UI 提供项目切换、artifact 上传和重解析状态、反应与 Geometry 目录、结构化筛选、排序、
缓存分页、计算 frame 详情、三维 Geometry 检查和源文件预览。

本地开发中，登录后可通过同一 origin 使用：

| 接口 | 本地路径 | 适用场景 |
| --- | --- | --- |
| Web 应用 | `/` | 交互式目录、上传与检查 |
| OpenAPI | `/docs` | REST API 发现与请求测试 |
| Direct-list GraphQL | `/nexusx/graphql` | 小型只读探索查询 |
| Paginated GraphQL | `/nexusx/paginated-graphql` | 筛选与分页数据访问 |
| MCP | `/nexusx/mcp/` | MCP 客户端访问 |
| Voyager | `/nexusx/voyager/` | 数据模型浏览 |

Artifact 可以被明确设置为 `public`。项目数据和写操作需要对应项目权限。生产环境使用 OIDC；
development identity 仅用于可复现的本地开发和自动化测试。

## 导入已有文件

浏览器上传队列和本地导入器共用同一 application upload service。CLI 的区别仅在来源：它直接
读取已有路径，而不是接收浏览器临时文件。

```bash
IMPORT_MODE=development \
IMPORT_PROJECT_ID=<project-uuid> \
IMPORT_ROOTS='/data/calculations /data/supplemental' \
IMPORT_STATE_FILE=.tmp/artifact-import.jsonl \
IMPORT_PIPELINE_WINDOW_FILES=128 \
IMPORT_COMMIT_BATCH_FILES=16 \
IMPORT_STREAM_QUEUE_SIZE=128 \
make import-artifacts
```

检查点是 append-only 的，使用源路径、大小、mtime 和 SHA-256 支持断点续跑。没有可恢复
calculation frame 的文件会以 `filtered` artifact 保留；一个文件或 frame 失败不会丢弃已解析的
frame 或不相关文件。

导入器刻意分离三种控制：

| 控制 | 默认值 | 作用 |
| --- | ---: | --- |
| `TRICYCLE_MOLOP_BATCH_N_JOBS` | `2` | 同时运行的文件级 MolOP worker 数 |
| `IMPORT_PIPELINE_WINDOW_FILES` | `128` | 解析队列可用的候选文件数 |
| `IMPORT_COMMIT_BATCH_FILES` | `16` | 每个持久化/检查点微批中的完成文件数 |

候选窗口应明显大于 worker 数，使结束或超时的 worker 立即获得下一个候选。不要用持久化微批
大小限制解析并发。应限制 `OMP_NUM_THREADS`、`OPENBLAS_NUM_THREADS` 和 `MKL_NUM_THREADS`
（提供的开发配置均为 `1`），避免嵌套 native thread 过度订阅。

单文件 timeout 以 `TRICYCLE_MOLOP_FILE_PARSE_TIMEOUT_SECONDS` 作为 10 MiB 源文件基准；
更大文件按体积获得比例更高的预算。超时文件独立失败并释放 worker 槽位。导入参数、重解析行为
与生产配置见[开发环境：直接批量导入存量文件](docs/development.md#直接批量导入存量文件)。

## 配置

只有在覆盖开发默认值或运行 Compose 栈时才需要从 `.env.example` 创建 `.env`。前端构建期设置
见 `frontend/.env.example`。重要配置分组包括：

| 分组 | 示例 |
| --- | --- |
| 应用品牌与环境 | `TRICYCLE_ENVIRONMENT`、`TRICYCLE_APP_NAME`、`VITE_APP_NAME` |
| 数据库 | `TRICYCLE_DATABASE_URL`、`TRICYCLE_QUERY_STATEMENT_TIMEOUT_MS` |
| 原始文件存储 | `TRICYCLE_RUSTFS_*` |
| 身份与会话 | `TRICYCLE_AUTH_MODE`、`TRICYCLE_OIDC_*`、`TRICYCLE_SESSION_*` |
| 导入资源 | `TRICYCLE_MOLOP_BATCH_N_JOBS`、`TRICYCLE_MOLOP_FILE_PARSE_TIMEOUT_SECONDS`、`OMP_NUM_THREADS` |
| 查询保护 | `TRICYCLE_*_RATE_LIMIT_*`、`TRICYCLE_STRUCTURE_*` |

生产启动会拒绝 development auth、默认 session secret、不安全 cookie、明文数据服务 endpoint
或未设置的必需 OIDC 配置。完整说明和多主机配置见[部署与配置指南](docs/deployment-configuration.md)。

## 开发与测试

```bash
make lint
make type
make test
make frontend-check
make frontend-build
```

数据库与对象存储集成测试还需要运行基础设施：

```bash
make infra-up
make migrate
make test-db
make test-storage
make test-infra
```

迁移只通过 Alembic 执行；不要在应用启动时调用 `SQLModel.metadata.create_all()`。更新 MolOP、
MolGR、OpenBabel 或 RDKit 后，重新锁定依赖，并运行 `make check` 与 `make test-db`。

## 仓库布局

```text
src/tricycle_reaction_db/  FastAPI adapter、application service、domain、DB model、ingestion
frontend/                  Vue 3 应用和 Playwright 测试
migrations/                Alembic schema migration
tests/                     Unit、integration、storage 与 contract 测试
docs/                      架构、开发、部署与运维指南
infra/                     Caddy、systemd、监控、Keycloak 与部署资源
scripts/                   验证、benchmark、审计与运维辅助工具
```

## 文档

文档索引提供中文与英文配对版本，并将当前运行契约与带日期的计划/验收记录分开。

- [文档索引 / Documentation index](docs/README.md)
- [开发环境与本地导入](docs/development.md) / [Development environment](docs/en/development.md)
- [部署与配置指南](docs/deployment-configuration.md) / [Deployment and configuration](docs/en/deployment-configuration.md)
- [数据模型与存储边界](docs/data-model.md) / [Data model and storage boundaries](docs/en/data-model.md)
- [业务模型](docs/business-model.md) / [Business model](docs/en/business-model.md)
- [生产运维与恢复 Runbook](docs/operations-runbook.md) / [Operations and recovery runbook](docs/en/operations-runbook.md)

## 安全

Artifact 文件可能包含受限的计算细节。生产公开前请保持 RustFS bucket 私有，在可信 edge
终止 TLS，并配置 OIDC 与 Redis-backed rate limit。请私下向维护者报告漏洞；不要在公开 issue
中提交凭据、access token 或原始受限计算文件。

## 贡献

保持改动范围清晰，并添加与风险相称的测试。schema 改动需要 Alembic migration；后端改动应
保持 application service 边界；前端改动应通过 Vue build 与相关 browser test。提交前查看
`git status`，不要纳入 `.env`、对象存储数据、测试输出或本地数据库卷。

## 许可证

除非仓库根目录的 `LICENSE` 另有说明，代码与文档的使用、再分发及贡献均受该许可证约束。
