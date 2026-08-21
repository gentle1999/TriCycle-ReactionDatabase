# 开发环境

## 前置条件

- `uv 0.9` 或更高版本
- Docker 与 Docker Compose
- Linux/amd64 开发环境
- 对内部 MolOP/MolGR Git 服务的访问权限

项目首个支持的 Python 版本是 3.12，具体解释器和全部 Python 依赖由
`.python-version` 与 `uv.lock` 固定。

`pyproject.toml` 的 `[tool.uv] cache-dir` 把 uv 的包缓存放在仓库内的
`.uv-cache/`（已被 `.gitignore` 排除）。这样在无法写入用户缓存目录
（如 `~/.cache/uv`）的受限 shell 里 `uv run` 依然可用；不需要时可以删除
`.uv-cache/`，下次 `uv run` 会重新下载依赖。

## 初始化

```bash
uv sync --python 3.12
```

需要覆盖开发默认值时，基于 `.env.example` 创建 `.env`。默认配置只监听
`127.0.0.1`，数据库账号和密码仅用于本地开发。

## 数据库

启动明确版本 tag 的 PostgreSQL/RDKit 容器：

```bash
docker compose up -d --wait postgres
uv run alembic upgrade head
make bootstrap-development
```

检查容器和 migration：

```bash
docker compose ps
uv run alembic current
uv run alembic check
```

停止数据库但保留开发数据卷：

```bash
make db-down
```

不要在应用启动代码中调用 `SQLModel.metadata.create_all()`。所有 schema 变更都应
通过 Alembic revision 完成；生产 downgrade 不自动删除 RDKit extension。

## Artifact 存储

原始 Gaussian/ORCA 文件使用 RustFS 保存，具体边界见
[数据模型与存储边界](data-model.md)。启动明确版本 tag 的 RustFS：

```bash
make storage-up
```

默认 S3 API 为 `http://127.0.0.1:19000`，Console 为
<http://127.0.0.1:19001>。开发凭据和 bucket 见 `.env.example`，仅用于本地环境。

运行真实对象往返测试：

```bash
make test-storage
```

测试覆盖 bucket 创建、put/head/get、SHA-256 校验、delete 和删除后 404。当前固定的
RustFS `1.0.0-beta.8` 尚非 stable release；升级 tag 或 digest 必须重新执行该测试。
生产和开发 Compose 默认设置 `RUSTFS_COMPRESSION_ENABLED=true`，启用 RustFS 磁盘层
压缩。该压缩不改变 S3 GET/HEAD 返回的逻辑原始字节，也不改变 PostgreSQL 中记录的
Artifact SHA-256 和大小；`.gz`、图片、音视频、PDF 等内建排除类型不会重复压缩。

同时启动 PostgreSQL/RDKit、RustFS 与本地 Keycloak，并验证数据基础设施：

```bash
make infra-up
uv run alembic upgrade head
make test-infra
```

只停止 RustFS 使用 `make storage-down`，只管理 Keycloak 使用 `make auth-up` / `make
auth-down`；停止全部容器并保留 named volumes 使用 `make infra-down`。

## API

### 认证与授权

开发环境默认使用 `TRICYCLE_AUTH_MODE=development`，每个受保护请求映射到
`make bootstrap-development` 显式创建的固定开发用户。Alembic migration 只创建 schema，
不会创建任何用户、组织、项目或权限。用户、外部身份、组织、项目和成员关系均保存在
PostgreSQL。

生产环境必须设置 `TRICYCLE_ENVIRONMENT=production` 和
`TRICYCLE_AUTH_MODE=oidc`，并配置 `TRICYCLE_OIDC_ISSUER`、
`TRICYCLE_OIDC_AUDIENCE`、`TRICYCLE_OIDC_JWKS_URL`。服务只验证外部 JWT 并保存
`issuer + subject` 映射，不保存本地密码。

浏览器使用 OIDC authorization-code flow：`/api/auth/login` 负责 state/nonce，回调后只把
随机 session token 的 SHA-256 摘要写入 `auth_session`，原始 token 通过 HttpOnly、SameSite
Cookie 返回浏览器。前端请求必须携带 Cookie；退出、单会话撤销和“撤销其他会话”都会立即
使数据库会话失效。不存在本地密码注册页，用户注册由 OIDC 身份提供方负责，首次通过 OIDC
登录时自动创建本地账户。用户可以创建组织并自动成为 owner，再创建第一个项目；也可以通过
邀请加入已有项目。OIDC 邮箱是身份提供方的权威字段，账户页只允许修改显示名称，
避免通过修改本地邮箱冒领项目邀请。

浏览器退出会先撤销本地 session Cookie；OIDC 回调另外把 ID Token 保存在独立的 HttpOnly
Cookie 中，仅用于向 provider 的 `end_session_endpoint` 提供 `id_token_hint`，不会写入数据库。
退出完成后两个 Cookie 都会清除，再回到应用。若 provider 没有提供该端点，系统仍会完成
本地退出。生产 HTTPS 部署应设置 `TRICYCLE_SESSION_COOKIE_SECURE=true`。

#### MCP 客户端令牌

浏览器登录使用 HttpOnly session Cookie，Cookie 不会暴露给 Claude、Cursor 等外部客户端。
登录后打开 `/nexusx`，在 UseCase MCP 卡片中生成独立的 MCP access token。生成响应中的
`access_token` 原文只返回一次，页面会把它填入各客户端配置；客户端实际发送：

```http
Authorization: Bearer mcp_<generated-value>
```

服务端只保存 SHA-256 摘要。账户页可以查看 token 名称、到期时间和最近使用时间，并撤销
token；撤销后原值立即失效。对应 API 为：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `POST` | `/api/auth/mcp-tokens` | 创建 token（请求 `{ "name": "Cursor" }`，原文只在响应中出现一次） |
| `GET` | `/api/auth/mcp-tokens` | 查看当前账户的 token 元数据，不返回原文 |
| `DELETE` | `/api/auth/mcp-tokens/{id}` | 撤销当前账户的 token |

开发模式仍允许不带 token 的本地请求；如果使用 MCP token，服务端仍会校验其有效期和撤销
状态，并只允许该 token 访问 `/mcp/`，不会把它当作通用 REST/GraphQL 凭据。生产模式的 MCP
请求必须携带 MCP token 或由受信任的 OIDC access token 认证。

普通认证请求只读会话；`last_seen_at` 最多每 5 分钟条件更新一次。过期会话和撤销超过 30 天
的会话由调度器定期执行 `make auth-session-cleanup`（或
`uv run tricycle-auth-session-cleanup --revoked-retention-days 30`）清理。该命令输出 JSON
删除计数，适合 cron、systemd timer 或 Kubernetes CronJob；不要放进 API 请求路径。

项目创建页通过 `GET /api/organizations` 获取组织角色，因此即使组织还没有项目，owner/admin
也可以创建第一个项目。邮箱邀请在开发环境默认为 `link_only`，接口返回可复制的接受链接；
生产环境建议设置 `TRICYCLE_EMAIL_DELIVERY_MODE=smtp` 以及 `TRICYCLE_SMTP_*` 参数。邀请记录
会保存 `pending`、`link_only`、`sent` 或 `failed` 投递状态，发送失败可调用重发接口，不会
丢失邀请记录。

本地 Keycloak 可由 `docker compose up -d keycloak` 启动，realm 允许开发环境自助注册，并
预置账号 `development / development-password`。启用本地 OIDC 时，按 `.env.example` 配置
issuer、client secret、回调 URI 和前端地址，并执行 `uv run alembic upgrade head`。realm
JSON 只会在空 Keycloak volume 首次导入；已有开发 volume 需要通过 Keycloak 管理界面同步
realm 配置，或明确重建开发身份数据。

生产部署应由一个 HTTPS origin 提供 `frontend/dist` 和 `/api/*`。可从
`infra/nginx/tricycle.conf` 开始配置 SPA fallback 与 FastAPI 反向代理；该示例对全部
`/api/*` 关闭 Nginx shared cache，并强制 `Cache-Control: private, no-store`。若外层还有
Cloudflare，必须另建 Cache Rule，使 URI path 以 `/api/` 开头的请求 bypass cache；应用响应头
不能纠正已配置的强制边缘缓存规则。仓库 Nginx 配置不再添加请求体大小或请求速率限制，并将
长请求读写超时设为一小时；上传大小、文件数量、并发和查询预算统一由应用配置校验。若外层
代理另设更小限制，仍以外层限制为准。

| 接口 | 匿名 | 已认证用户 |
| --- | --- | --- |
| `GET /api/artifacts` | 仅列出 `public` | 公开文件和有权项目内文件 |
| `GET /api/artifacts/{id}/preview` | 公开文件 | 公开文件和有权项目内文件 |
| `GET /api/artifacts/{id}/download` | 公开文件 | 公开文件和有权项目内文件 |
| `GET /api/auth/me` | `401` | 当前用户、组织/项目角色和权限 |
| `GET /api/organizations` | `401` | 当前用户可访问的组织和创建项目权限 |
| `POST /api/organizations` | `401` | 创建组织，当前用户自动成为 owner |
| `POST /api/artifacts` | `401` | 需要目标项目 `artifact:upload` 权限 |
| `POST /api/artifacts/batch` | `401` | 同一项目内独立处理多个文件 |
| `POST /api/artifacts/validate` | `401` | 只 probe/解析，不写存储或数据库 |
| `POST /api/artifacts/{id}/reparse` | `401` | 校验已存 bytes 后创建下一 parse revision |
| `PATCH /api/artifacts/{id}` | `401` | 项目 manager 修改显示文件名或可见性 |
| `DELETE /api/artifacts/{id}` | `401` | 需要项目 `artifact:delete`，退役记录并清理对象 |
| `GET/POST /api/projects` | `401` | 可含归档项目；创建要求组织 owner/admin |
| `GET/PATCH /api/projects/{id}` | `401` | 查看项目；修改要求 project manager 或组织管理员 |
| `/api/projects/{id}/members` | `401` | 项目成员列表、添加、改角色和移除 |
| `GET /api/users?project_id=...` | `401` | 项目 manager 搜索可添加的活跃用户 |
| `GET/PATCH /api/users/...` | `401` | system organization 管理员查询和启停用户 |
| `/api/auth/sessions` | `401` | 当前账户会话列表和撤销 |
| `/api/projects/{id}/invitations` | `401` | project manager 创建、列出和撤销邮箱邀请 |
| `POST /api/projects/{id}/invitations/{invitation_id}/resend` | `401` | 重发未接受的邮箱邀请 |
| `POST /api/auth/invitations/{token}/accept` | `401` | 登录邮箱匹配后接受一次性项目邀请 |
| `/api/auth/audit`、`/api/projects/{id}/audit` | `401` | 账户或项目管理审计记录 |
| 其他 REST、GraphQL、MCP、depiction 接口 | `401` | 需要有效身份 |

公开文件请求携带无效 `Authorization` header 时仍返回 `401`，不会降级为匿名。
统一文件上传已开放，新建 Artifact 固定为 `project`；可见性修改尚未开放。上传接口先
校验项目权限，在 PostgreSQL 建立 `pending` Artifact 关系，再保存 RustFS object，校验
通过后更新为 `available`。写入、校验或状态提交失败时，生命周期补偿 Hook 立即定点删除
未变成 `available` 的本次对象和 pending 预约行，不保留 `missing` 垃圾记录。Artifact
DELETE 保留 `retired` tombstone，RustFS 临时故障时可重复请求继续清理。
退役来源不会继续参与详情、下载和派生事实可见性；同一项目以相同 Artifact 类型重新上传
相同 bytes 会恢复原 tombstone 和既有解析历史。计算输出
统一拆分并录入所有 MolOP 帧；检测到
TS 帧时额外创建或复用同一反应，并保存 TS CalculationFrame 到反应的推断溯源。
格式由 MolOP probe 从内容识别；文件名、扩展名、目录结构、manifest 和上传顺序都不参与
化学身份。批量请求中每个 Artifact 使用独立事务，一个文件失败不会回滚其他文件。
生产 OIDC 用户首次登录后才进入本地用户目录；首次 system administrator 需要部署侧将该
用户加入 system organization 并授予 owner/admin，API 不允许普通项目 manager 提升全局
账号权限。

开发环境可使用 migration 创建的默认项目测试 multipart 上传：

```bash
curl -sS http://127.0.0.1:8000/api/artifacts \
  -F project_id=00000000-0000-7000-8000-000000000201 \
  -F artifact_kind=calculation_output \
  -F file=@path/to/transition-state.log

curl -sS http://127.0.0.1:8000/api/artifacts/batch \
  -F project_id=00000000-0000-7000-8000-000000000201 \
  -F artifact_kind=calculation_output \
  -F files=@path/to/reactant.log \
  -F files=@path/to/transition-state.out \
  -F files=@path/to/single-point.data

curl -sS http://127.0.0.1:8000/api/artifacts/validate \
  -F project_id=00000000-0000-7000-8000-000000000201 \
  -F file=@path/to/unstructured-upload.bin

curl -sS -X POST \
  http://127.0.0.1:8000/api/artifacts/00000000-0000-0000-0000-000000000000/reparse
```

响应给出 artifact/ingestion ID、源帧数、TS 帧数，以及每个 TS 帧复用的
logical/mapped reaction ID，以及本次 `parse_revision_id/parse_revision_created`。同一文件
普通重复上传返回相同 revision；显式 reparse 创建 artifact 内递增 revision 并连接前驱。
已有成功 revision 时解析或持久化失败不会覆盖当前成功 ingestion 汇总。非计算 artifact
只返回存储结果，不创建 ParseRevision 或 CalculationFrame。

### 上传补偿与可选 RustFS 垃圾回收

先完成迁移，再运行一次 GC：

```bash
uv run alembic upgrade head
make storage-gc
```

正常上传失败由默认启用的生命周期 Hook 定点补偿，不需要列举 bucket。定期 GC 是处理进程
强制终止、机器故障、外部写入和 Hook 失败的可选安全网；需要最终收敛保证的生产环境可通过
cron、systemd timer 或 Kubernetes CronJob 低频调用 `uv run tricycle-rustfs-gc`，不要在
FastAPI 多 worker 内启动后台循环。默认每次保留一小时
宽限期，并只列举上次成功水位之后的 `uploads/YYYY/MM/DD/HH/` 分区。可用环境变量调整：
`TRICYCLE_STORAGE_GC_GRACE_PERIOD_SECONDS`、`TRICYCLE_STORAGE_GC_INITIAL_LOOKBACK_SECONDS`
和 `TRICYCLE_STORAGE_GC_PARTITION_CLOCK_SKEW_SECONDS`。运行结果以 JSON 输出，并同时写入
PostgreSQL 审计表；失败退出码非零且不推进水位。

GC 保留 `available` Artifact；对超过宽限期、仍未发布的 `pending`，在内容
identity lock
内删除 RustFS 对象（若存在）和数据库预约行。不要把失败预约转成长期 `missing` 记录，也
不要用该规则删除已有 ParseRevision、ingestion、manifest 或 binding 的历史 Artifact。

原有单进程组合应用仍可启动：

```bash
uv run tricycle-api
```

按照 NexusX demo 将四个非 REST 传输拆分到独立进程：

```bash
make serve-nexusx
```

| 前端代理路径 | 模式 | 默认上游 |
| --- | --- | --- |
| `/docs` | 项目组合 API，包含 Core 和 UseCase REST | 组合 API `8000/docs` |
| `/nexusx/graphql` | Direct-list GraphQL，只读直接列表 | 组合 API `8000/graphql-playground` |
| `/nexusx/paginated-graphql` | Paginated GraphQL，`items + page` | 组合 API `8000/graphql` |
| `/nexusx/mcp/` | UseCase MCP，四层渐进披露 | 组合 API `8000/mcp/` |
| `/nexusx/voyager/` | Voyager 可视化 | 组合 API `8000/voyager/` |

Core API 和 UseCase FastAPI 的独立应用仍保留给兼容性测试和拆分部署；它们不再由
`make serve-nexusx` 默认启动，前端也不重复展示文档入口，日常使用统一打开项目组合 API
的 `/docs`。

浏览器只需要访问前端端口 `5173`。`make serve-nexusx` 仍可启动各传输的独立演示进程，
但它们是代理的内部上游，不应直接暴露；如需拆分上游，可通过 `NEXUSX_*_PROXY_TARGET`
覆盖 Vite 代理，并在生产 Nginx 中同步调整对应 location。独立演示进程的 GraphQL
playground 占用 `8000`，因此不能与默认也占用 `8000` 的 `tricycle-api` 同时启动。

NexusX `ErManager` 当前不接受复合 relationship join。Voyager ER 子图因此暂时省略
`CalculationSegment`、`ManifestArtifactBinding`、`MappedReactionEdge` 和
`WorkflowManifest` 四个模型；数据库表、外键和
其他 API 不受影响。不能为了 Voyager 展示而移除这些复合一致性约束。

组合应用默认地址：

- OpenAPI：<http://127.0.0.1:8000/docs>
- `GET /health/live`：进程存活检查，不访问数据库
- `GET /health/ready`：检查 PostgreSQL 和 RDKit extension
- `POST /api/{service}/{method}`：NexusX 从白名单 use case 生成的 REST
- `POST /graphql`：NexusX Compose GraphQL HTTP endpoint
- `GET /graphql`：开发环境 GraphiQL；生产环境返回 404
- `GET /graphql/schema`：Compose schema SDL
- `POST /graphql-playground`：直接列表、只读 Compose GraphQL endpoint
- `GET /graphql-playground`：仅供前端 `/nexusx/graphql` 代理的开发环境 GraphiQL
- `GET /graphql-playground/schema`：直接列表 schema SDL
- `/mcp/`：无状态 Streamable HTTP MCP endpoint

浏览器前端源码位于 `frontend/`，使用 Vue 3 + Vite 构建，ChemDoodle Web Components
11.0.0 官方 JS/CSS/license 位于 `frontend/public/vendor/chemdoodle/`。FastAPI
不提供首页或前端静态文件。开发时运行 `make serve-frontend`，访问
<http://127.0.0.1:5173/>；Vite 将 `/api`、`/health`、`/docs` 和 `/nexusx/*` 代理到组合
FastAPI，目标可通过 `VITE_API_PROXY_TARGET` 调整。拓扑图片由
`GET /api/depictions/topology/{topology_id}.svg`
生成；ChemDoodle 画布使用 `GET /api/depictions/topology/{topology_id}.mol` 返回的
2D molfile，两个接口都从数据库 RDKit `mol` 副本派生且不修改持久化对象。

`make frontend-build` 将生产文件写入 `frontend/dist`。该目录不进入 Python wheel，
应交给静态服务器或 CDN；反向代理需要将 `/api`、`/health`、`/docs`、`/openapi.json` 和
`/nexusx/*` 转发到 FastAPI。跨域独立部署时可在构建阶段设置
`VITE_API_BASE_URL`，并在 API 网关显式配置允许的前端 origin。

首次运行浏览器测试需要安装 Chromium，且组合 API、PostgreSQL 和 fixture 数据必须
可用：

```bash
npm --prefix frontend exec playwright install chromium
make frontend-test-e2e
```

REST、GraphQL 和 MCP 共用 `SystemService`、`ArtifactQueryService`、
`ArtifactIngestionQueryService`、`LogicalReactionQueryService`、
`MappedReactionQueryService`、`CalculationQueryService`、
`CalculationResultQueryService`、`WorkflowManifestQueryService`、
`StorageGarbageCollectionQueryService`、
`MolecularTopologyDerivationQueryService` 和
`ReactionCommandService`。Direct-list GraphQL 额外提供只读 catalog service，
但包含主配置的全部业务 service。
`UseCaseService` 是 application 层查询边界；FastAPI 路由只处理 HTTP transport，
不得直接查询 ORM。NexusX 只启用显式 `create_reaction` mutation，不提供通用
entity CRUD。

生成的 REST 路由全部使用 `POST`，参数放在 JSON body。例如：

```bash
base_url=http://127.0.0.1:8000
logical_reactions="$base_url/api/logical_reaction_query_service/list_logical_reactions"
create_reaction="$base_url/api/reaction_command_service/create_reaction"

curl -s "$logical_reactions" \
  -H 'content-type: application/json' \
  -d '{"limit": 20, "offset": 0}'

curl -s "$create_reaction" \
  -H 'content-type: application/json' \
  -d '{"reaction":"C1CC1>>C=CC"}'
```

创建反应不接收数据库 ID 或计算文件。后端从 reaction components 自动解析、复用或创建
Formula/Topology；计算文件通过独立导入流程补充 Geometry 和 Frame。

分页 GraphQL 根字段是 service 类名，方法名保持 snake_case：

```graphql
{
  LogicalReactionQueryService {
    list_logical_reactions(limit: 20, offset: 0) {
      items { id reaction_key }
      page { total limit offset }
    }
  }
}
```

NexusX 6.1.2 Compose executor 暂不支持 variables；参数必须 inline，带非空
`variables` 的请求返回 HTTP 400。MCP 按开发指南提供四层渐进披露工具：
`list_apps`、`describe_compose_schema`、`describe_compose_method` 和
`compose_query`。

当前运行时使用 NexusX 6.1.2 的 DTO-first Compose executor、严格 selection 校验、
`UseCaseAppConfig`、新版 `create_use_case_voyager` 和 Streamable HTTP MCP server。
NexusX 6.1.2 同时为 federation 的 `page_by_*_in` 根提供声明式默认排序；本项目当前
使用单数据库 member，未启用跨数据库 federation，因此该能力由依赖保留，待新增独立
engine 时通过实体 `__federation_keys__` 与 `__pagination_orders__` 显式开启。为启用
6.1.2 的 Voyager member cluster/color，数据库实体和应用 DTO 登记在一个带
`service_name`、`color` 的 `ErManager`
中，再作为单个 member 交给 `ComposedErManager`。ER 图和 UseCase 图的数据库归属标签及颜色
分别由 `TRICYCLE_NEXUSX_DATABASE_CLUSTER_NAME` 和
`TRICYCLE_NEXUSX_DATABASE_CLUSTER_COLOR` 覆盖。

这个单 member 包装不改变查询、关系或权限边界。本项目的 PostgreSQL 高可用节点通过一个
writer endpoint 对应用呈现为同一个逻辑 engine，节点数量不会变成 NexusX member 数量。
若未来接入真正独立的数据库 engine，应为每个 member 建立互斥实体集合，并在
`ComposedErManager.cross_relationships` 显式声明跨边界关系；不能仅把 PostgreSQL HA
节点列表当作多个 NexusX engine。RustFS、Redis、OIDC 和 SMTP 也不是 ER member。

查询 DTO 只返回稳定业务字段。`ScientificArray` 仅暴露 kind、unit、dtype、shape、
字节数和 SHA-256，不返回矩阵 `data`；RDKit `Mol`、内部 JSONB 和 RustFS 凭据同样
不进入 API schema。

### 查询预算与慢查询

组合 FastAPI、独立 UseCase REST、GraphQL 和 MCP 共用查询预算。默认值定义在
`.env.example`：

| 配置 | 默认值 | 作用 |
| --- | ---: | --- |
| `TRICYCLE_QUERY_STATEMENT_TIMEOUT_MS` | `15000` | 每个 PostgreSQL 连接的 statement timeout |
| `TRICYCLE_SLOW_QUERY_THRESHOLD_MS` | `500` | 记录 SQL 模板和耗时，不记录绑定参数 |
| `TRICYCLE_GRAPHQL_MAX_QUERY_CHARACTERS` | `20000` | GraphQL 文档字符数上限 |
| `TRICYCLE_GRAPHQL_MAX_TOKENS` | `2000` | GraphQL parser token 上限 |
| `TRICYCLE_GRAPHQL_MAX_DEPTH` | `12` | GraphQL AST 最大深度 |
| `TRICYCLE_GRAPHQL_MAX_COMPLEXITY` | `250` | 字段、分页和 fragment 展开的复杂度上限 |
| `TRICYCLE_QUERY_RATE_LIMIT_REQUESTS` | `120` | 管理写操作和未分类请求的固定窗口请求数 |
| `TRICYCLE_READ_RATE_LIMIT_REQUESTS` | `10000` | 登录态、目录、详情、GraphQL 等只读请求数 |
| `TRICYCLE_UPLOAD_RATE_LIMIT_REQUESTS` | `1000` | Artifact 上传、批量上传、验证和重解析请求数 |
| `TRICYCLE_UPLOAD_MAX_CONCURRENCY` | `8` | 单个 API 进程内同时处理的上传请求数 |
| `TRICYCLE_MOLECULE_QUERY_RATE_LIMIT_REQUESTS` | `10000` | 分子式、拓扑和几何只读查询的独立固定窗口请求数 |
| `TRICYCLE_DEPICTION_RATE_LIMIT_REQUESTS` | `10000` | 分子 SVG/MOL/SDF 资源的独立固定窗口请求数 |
| `TRICYCLE_QUERY_RATE_LIMIT_WINDOW_SECONDS` | `60` | 限流窗口秒数 |
| `TRICYCLE_STRUCTURE_QUERY_MAX_CHARACTERS` | `16384` | SMILES/SMARTS/reaction 输入长度上限 |
| `TRICYCLE_STRUCTURE_CANDIDATE_LIMIT` | `50000` | 需要逐候选后处理的最大关系行数 |
| `TRICYCLE_MOLOP_BATCH_N_JOBS` | `2` | 批量上传时保留 source evidence 的文件级并行进程数；`-1` 在开发环境使用全部可用 CPU，生产环境必须显式限界 |

描述符、Murcko scaffold、手性和匹配次数等逐候选计算必须先通过 Formula、
Topology 或
其他廉价关系条件缩小候选集。SMARTS 和带阈值的相似度以 RDKit GiST 谓词筛选后的实际
候选集计数；纯 Top-K 相似度由 fingerprint GiST KNN 和 API 的 `limit <= 200` 限界，
不会仅因整表规模超过候选上限被拒绝。

只读、上传、管理写操作、分子查询和 `GET /api/depictions/*` 分别计数，避免批量上传或卡片图片挤占登录态与目录读取额度。稳定错误语义如下：REST 对预算超限返回 HTTP 413 `query_budget_exceeded`，限流返回
HTTP 429 `query_rate_limit_exceeded` 和 `Retry-After`，数据库超时返回 HTTP 503
`query_timeout`。GraphQL 与 MCP 在 error envelope 的 `extensions.code` 使用相同
code。
PostgreSQL 取消语句后 session 会 rollback 并可继续复用连接；慢查询日志仅保存 SQL
模板和毫秒耗时，不能输出绑定值、结构输入或凭据。

## 质量检查

```bash
make lint
make type
make test
make test-db
make test-storage
make test-infra
```

`make test` 默认跳过真实数据库测试；数据库启动并完成 migration 后，使用
`make test-db` 验证 RDKit extension、MolAlchemy `Chem.Mol` 化学图往返、构象精度
边界、自定义 property 丢失、子结构查询、GiST 索引和就绪接口。往返契约及升级
检查要求见 [RDKit Mol 对象数据库往返契约](rdkit-mol-roundtrip.md)。

`make test-storage` 验证 RustFS。`make test-infra` 同时启用全部 PostgreSQL/RDKit
与 RustFS 集成测试。普通 `make test` 不访问外部基础设施。

查询成本数据库门可独立运行：

```bash
TRICYCLE_RUN_DATABASE_TESTS=1 uv run pytest -q \
  tests/integration/test_query_cost_database.py \
  tests/integration/test_topology_search.py \
  tests/integration/test_reaction_search.py --no-cov
```

该门验证 statement timeout 后连接恢复、慢查询绑定值脱敏、Formula GIN、Topology/
Reaction RDKit GiST 与 fingerprint KNN、Geometry/Frame B-tree 计划，并把候选上限压低后
验证未索引扫描被拒绝而索引 SMARTS、阈值相似度和 Top-K 不受全表行数误伤。

### 真实 DA fixture

`tests/fixtures/da_bench_minimal` 保存 `C=C + c1scc2c1OCCO2` 环加成的固定子集，
包括两个 reactant、一个 TS 和一个 product 的 Gaussian 日志及源 JSON。日志使用
deterministic gzip，fixture manifest 固定压缩与解压后双 SHA-256；选择的 TS
`conf_01` 具有一个虚频，第 22 帧为 terminal/converged 并可提供 TS Geometry；同一
Geometry 下的其他 Frame 仍作为计算事实保留并可在详情中查看。

普通 `make test` 会解压并用 MolOP 验证帧数、Formula 和逐帧 Topology；`make test-db`
进一步验证 UUIDv7、SQLModel Relationship、PostgreSQL RDKit `mol`、deferred NPY、
Formula -> Topology -> Geometry 与 Revision -> Segment -> Frame 两条主轴的幂等持久化。
测试不依赖 `/mnt/g` 挂载。

基础设施启动并完成 migration 后，可将该 fixture 作为开发数据录入：

```bash
make seed-da-bench
```

### 直接批量导入存量文件

大量存量文件不需要经过浏览器或 HTTP API。`tricycle-import-artifacts` 在服务端进程内递归读取文件，直接调用 Artifact 入库服务，写入 PostgreSQL/RustFS，并按现有 MolOP 解析流程处理计算输出。它按服务端 `max_batch_files` 和 `max_batch_bytes` 自动分批，内容 SHA-256 由 Artifact 唯一约束负责幂等去重。

先启动 PostgreSQL、RustFS、完成 migration 和 development bootstrap，然后执行：

```bash
uv run tricycle-import-artifacts \
  --project-id 00000000-0000-7000-8000-000000000201 \
  --state-file .tmp/artifact-import.jsonl \
  /data/archive/reactions /data/archive/supplemental
```

参数说明：

- 可以传入多个文件或目录；目录会递归扫描，符号链接不会展开。
- 默认导入 `calculation_output`，可用 `--artifact-kind input|workflow_manifest|auxiliary` 覆盖。
- `--state-file` 是追加写入的 JSONL 检查点。重复执行会按路径、大小、mtime 和 SHA-256 跳过已成功文件；文件发生变化后会重新导入。
- 使用 `--dry-run` 只扫描并输出统计，不写数据库或对象存储。
- 生产环境必须显式提供 `--user-id`，该用户需要目标项目的 `artifact:upload` 权限。

也可以使用 Makefile：

```bash
IMPORT_PROJECT_ID=00000000-0000-7000-8000-000000000201 \
IMPORT_ROOTS='/data/archive/reactions /data/archive/supplemental' \
IMPORT_STATE_FILE=.tmp/artifact-import.jsonl \
make import-artifacts
```

命令最后输出 JSON 统计，包括扫描、跳过、尝试、成功、失败数量和成功字节数。失败批次会写入检查点并以非零状态退出；修复原因后重新执行同一个命令即可继续。

该命令会将 manifest 和四个解压后的 Gaussian 日志真实上传到 RustFS，并将反应物、
过渡态、产物及其反应路径写入 PostgreSQL。四个日志的全部 9 个 Link1 segment 和
45 个物理 frame 都会录入；坐标相同的重复终态仍保留为独立 Frame，但共享同一
Geometry。命令可重复执行；相同 fixture 会复用既有记录，并在结束时输出关键 UUID、
逐文件帧数和各业务表行数。

按原文件顺序查看全部优化帧及其能量：

```sql
SELECT
    a.original_filename,
    s.segment_index,
    f.frame_index,
    f.file_frame_index,
    f.frame_role,
    f.optimization_status,
    f.scf_status,
    f.reference_total_energy_hartree,
    t.canonical_isomeric_smiles
FROM calculation_frame AS f
JOIN calculation_segment AS s ON s.id = f.segment_id
JOIN parse_revision AS r ON r.id = f.parse_revision_id
JOIN artifact_file AS a ON a.id = r.artifact_file_id
JOIN geometry AS g ON g.id = f.geometry_id
JOIN molecular_topology AS t ON t.id = g.topology_id
ORDER BY a.original_filename, f.file_frame_index;
```

当前 seed 保存逐帧 Geometry、原文 span/hash、reference total energy、SCF/优化状态、
频率摘要，以及 MolOP 实际解析到的全部受支持数组。MolOP 子模型分别写入 45 个
FrameEnergyResult、49 个 EnergyObservation、40 个 GeometryOptimizationResult、
40 个 CalculationStatusResult、4 个 VibrationResult 和 4 个 ThermochemistryResult。
当前 fixture 共写入 227 条
`ScientificArray`：40 组 forces、45 组 rotational constants、14 组 orbital energies、
18 组 population values、74 组 polarizability/multipole 数组，以及各 4 组 Hessian、
frequencies、reduced masses、vibrational force constants、IR intensities、
normal modes、moments of inertia、rotational temperatures 和 vibrational
temperatures。5 个终态几何
重打印帧在 MolOP 中的 forces 为 `None`，因此保持缺失，不补零或复制相邻帧数据。

`ScientificArray.data` 默认使用 `raiseload` 延迟加载。ORM 查询矩阵载荷时必须显式使用
`undefer(ScientificArray.data)`；列表查询仍只返回 kind、unit、dtype、shape、nbytes、
payload hash 和 metadata。

## 依赖约束

MolOP `>=0.2.4` 与 MolGR `>=0.1.3` 直接从官方 PyPI 安装；`pyproject.toml` 声明最低
兼容版本，`uv.lock` 记录当前解析版本。项目不再使用内网 Git source 或
`override-dependencies`。

更新 MolOP、MolGR、OpenBabel 或 RDKit 时，
必须重新运行：

```bash
uv lock --python 3.12
uv sync --python 3.12 --frozen
uv pip check
uv run python -c "import molop, molgr, openbabel, rdkit"
make check
make test-db
```
