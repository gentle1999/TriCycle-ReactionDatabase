# 部署与配置指南

本文说明反应数据库服务的开发和生产部署配置。仓库中的 `Example Chemistry Database`、组织名和项目名
都是可替换的开发占位默认值，不是生产部署身份。环境变量示例见
[.env.example](../.env.example)，前端变量见
[frontend/.env.example](../frontend/.env.example)。

## 1. 部署边界

单机和多机生产拓扑都受支持。应用不要求 PostgreSQL、RustFS 或中间件与 API 同机，也不要求
这些后端各自只有一台机器。推荐的多机/多节点拓扑是：

~~~text
浏览器 -> EDGE-01/02 (TLS + frontend/dist + API upstream)
                       `-> API-01/02 ...
                            |-> db-rw.internal      -> PG-01/02/03 (PostgreSQL/RDKit HA)
                            |-> s3.internal         -> OBJ-01/02/03/04 (RustFS cluster)
                            |-> redis-rw.internal   -> CACHE-01/02/03 (Redis HA)
                            |-> identity.example.org -> IDP-01/02 (OIDC cluster)
                            `-> smtp.internal       -> MAIL-01/02 (SMTP relay)

SCHED-01 -----------------------> 同一个 DB、S3 和 Redis 逻辑端点
~~~

当前 `compose.yaml` 编排 PostgreSQL/RDKit、RustFS、开发用 Keycloak、数据库迁移、初始数据
bootstrap、API、前端静态服务和同源 HTTPS Caddy。API 与前端只在 Compose 网络内监听，公网
入口只有 Caddy 的 HTTP/HTTPS 端口。HTTP 只返回 308 跳转，HTTPS 代理 `/api`、`/health`、
`/docs`、GraphQL、MCP 和 `/nexusx/*`，其余路径交给前端 SPA。

### 两台服务器部署

“数据服务器运行 PostgreSQL/RustFS，算力服务器运行 API、前端、Caddy、Keycloak”是合理的
两层拓扑，尤其适合计算解析和 Web 流量集中在算力服务器的场景。但当前根目录
`compose.yaml` 是单机开发栈，会同时声明本地 PostgreSQL/RustFS；不要把它原样部署到算力
服务器再让两套数据库并存。生产应拆成两个 Compose project（或使用编排平台的两个 stack）：

~~~text
浏览器 -> 算力服务器 Caddy (443)
                    |-> frontend 静态服务
                    |-> API / 直接导入 CLI / Keycloak
                    |       |-> TLS -> 数据服务器 PostgreSQL:5432
                    |       `-> TLS -> 数据服务器 RustFS/S3:9000
                    `-> 仅内部管理端口，不直接暴露 API、Keycloak admin、RustFS Console
~~~

必须满足这些条件：

1. PostgreSQL 和 RustFS 使用数据服务器本地可靠磁盘，不把 PostgreSQL 数据目录放在普通
   NFS/SMB 共享盘；RustFS 也应使用经过验证的本地文件系统或其明确支持的存储后端。
2. 数据服务器防火墙只允许算力服务器（以及备份/监控主机）访问 PostgreSQL 和 RustFS；
   RustFS Console、PostgreSQL 管理端口和 API 内网端口不开放公网。
3. API 使用 `sslmode=verify-full` 连接 PostgreSQL，RustFS 使用 HTTPS、私有 bucket 和
   TLS 校验。内部网络也不要依赖明文 HTTP 作为生产默认。
4. 数据库备份和 RustFS 对象/版本备份必须写到独立故障域；数据服务器单机故障仍是当前
   拓扑的主要 SPOF。恢复演练要同时验证数据库清单与对象 bytes、metadata、version ID。
5. 直接存量导入脚本应在算力服务器运行，并让它读取本地暂存或只读挂载的源文件；它直接
   调用应用服务写 PostgreSQL/RustFS，不经过浏览器或 HTTP multipart。大批量导入时优先把
   源文件暂存到算力服务器本地 SSD，避免让 NFS 读流量和 RustFS 写流量互相争用。
6. Keycloak 当前 Compose 配置是开发用 `start-dev`，不能作为生产身份服务；生产 Keycloak
   应使用外部数据库、正式 TLS、独立备份和不暴露 admin endpoint 的反向代理规则。

算力服务器只有一台时，API、Caddy、前端和 Keycloak 共机是可接受的起步方案；要横向扩展
   API，需要共享 Redis 限流、共享数据库/RustFS endpoint，以及独立的 Keycloak/负载均衡策略。

Compose 的无参数默认值用于开发或单机验收。生产模式不会降低传输安全要求：仍须使用
`sslmode=verify-full` 的 PostgreSQL URL、HTTPS RustFS/S3、外部生产 OIDC、SMTP STARTTLS 和
Redis TLS。开发单 worker 可以使用进程内限流；生产环境必须使用 Redis 作为 API/MCP worker
的共享限流后端。v1 数据流程不依赖 Celery 或 Kafka。

开发用 Keycloak 使用 start-dev，只能绑定本机回环地址，不能直接作为公网身份服务。

## 2. 需要提前准备的信息

将下面的信息交给部署人员，秘密值通过密码管理器或部署平台 Secret 注入，不要提交 Git。

| 类别 | 必需信息 | 用途 |
| --- | --- | --- |
| 公网访问 | 前端域名，例如 https://app.example.com | 登录回跳、邀请链接和浏览器访问 |
| 反向代理 | TLS 证书、域名、API 上游地址 | 提供同源 HTTPS 入口 |
| PostgreSQL | 稳定读写 endpoint、端口、数据库、用户名、密码、SSL 参数 | 领域数据、用户和权限；后端每个候选节点必须有 RDKit extension |
| RustFS/S3 | 稳定 HTTPS endpoint、access key、secret key、bucket、region、TLS 校验方式 | 保存原始 Gaussian/ORCA 文件；endpoint 可位于多节点 RustFS 前的负载均衡器 |
| Redis | 稳定可写 TLS endpoint、认证、CA 和 key prefix | 所有 API/MCP 节点共享限流窗口；后端可由多节点 HA 组成 |
| OIDC | issuer、audience、JWKS URL、client ID、client secret、回调 URI | 登录、用户创建和邮箱身份 |
| 初始管理员 | 第一个 OIDC 用户的邮箱或 subject | 部署后加入 system organization 的 owner/admin |
| SMTP | host、端口、用户名、密码/app password、发件人地址 | 发送项目邀请 |
| 运维 | 备份位置、保留周期、监控、定时任务和资源预算 | 恢复、GC、会话清理和容量控制 |

## 3. 前置软件

- Linux/amd64 是当前部署基线。
- 完整容器部署只要求 Docker Engine 及支持 `--wait`、`!reset` 和 `!override` 的 Docker
  Compose（建议 Compose 2.24+）。
- 宿主机直接开发时要求 Python 3.12、uv 0.9 或更高版本、Node.js 20+ 和 npm。
- 生产 PostgreSQL 必须安装 RDKit extension；开发 Compose 使用明确版本 tag 的
  `antonsiomchen/cheminfo-db` 镜像作为兼容性基线。生产部署可以由镜像仓库或 CI 在发布时
  解析并记录 digest，但不要求把 digest 写进 Compose 文件。

## 4. 环境文件

~~~bash
cp .env.example .env
chmod 600 .env
~~~

.env 已被 Git 忽略。开发环境可以保留示例中的本机账号；生产环境必须替换所有
密码、密钥、域名和 endpoint，不要使用示例开发密码或默认
Session secret。

### 4.1 完整 Compose 与 HTTPS

本地首次启动：

~~~bash
cp .env.example .env
docker compose up -d --build --wait
curl --insecure https://localhost/health/ready
~~~

也可使用 `make stack-up`、`make stack-logs` 和 `make stack-down`。启动顺序由健康检查保证：
PostgreSQL -> Alembic migration -> bootstrap -> API；RustFS 与前端 healthy 后才启动 Caddy。
`docker compose down` 保留 named volumes；只有显式 `docker compose down --volumes` 才删除
数据库、对象存储、Keycloak 和自签名证书数据。

开发环境的 `CADDY_SERVER_NAME=localhost` 使用 Caddy 内置 CA；Caddy 状态保存在
`caddy-data` 和 `caddy-config` named volume 中。生产环境必须将域名解析到算力服务器，并让
TCP 80/443 可被 ACME issuer 访问，Caddy 会自动申请和续期证书。生产 `.env` 至少设置：

~~~dotenv
CADDY_SERVER_NAME=app.example.com
CADDY_BIND_ADDRESS=0.0.0.0
CADDY_HTTP_PORT=80
CADDY_HTTPS_PORT=443

# 容器内 API 使用这两个值；普通 TRICYCLE_* 变量仍由 API 读取。
TRICYCLE_COMPOSE_DATABASE_URL=postgresql+psycopg://user:password@db-rw.internal.example/reactions?sslmode=verify-full
TRICYCLE_COMPOSE_RUSTFS_ENDPOINT_URL=https://s3.internal.example
TRICYCLE_BOOTSTRAP_MODE=production
~~~

不要删除或替换 `caddy-data`；其中包含 ACME 账户、证书和续期状态。若 80/443 已由外部负载均衡器
占用，可以覆盖宿主机端口，但 ACME HTTP-01 仍需要由外部入口转发到 Caddy 的 HTTP 端口；正式
环境不应关闭 TLS 校验或把 Caddy storage volume 放在临时目录。

### 4.1.1 两台机器：数据服务器 + 算力服务器

两台机器在同一路由器下时，不要把 Docker Compose 的默认网络当作跨主机网络。数据服务器和
算力服务器各运行自己的服务，应用只通过数据服务器的私网 DNS/IP 和端口连接。仓库提供
[`compose.data.yaml`](../compose.data.yaml) 作为数据服务器独立栈，以及
[`compose.compute.yaml`](../compose.compute.yaml) 作为算力服务器 overlay：它把本地
PostgreSQL/RustFS 放入 `local-data` profile，并移除迁移/API 对本地数据服务的依赖。

数据服务器（只运行 PostgreSQL/RustFS）配置 `.env`：

~~~dotenv
POSTGRES_DB=reactions
POSTGRES_USER=reaction_app
POSTGRES_PASSWORD=<strong-password>
POSTGRES_BIND_ADDRESS=192.168.50.29
POSTGRES_PORT=5433

RUSTFS_BIND_ADDRESS=192.168.50.29
RUSTFS_API_PORT=9001
# Console 不应对算力服务器或公网开放；回环绑定时通过 SSH 隧道管理。
RUSTFS_CONSOLE_BIND_ADDRESS=127.0.0.1
RUSTFS_CONSOLE_PORT=9002
TRICYCLE_RUSTFS_ACCESS_KEY=<access-key>
TRICYCLE_RUSTFS_SECRET_KEY=<secret-key>
~~~

数据服务器只需启动两个数据服务（不要在这里启动 API/前端）：

~~~bash
docker compose -f compose.data.yaml up -d --wait
~~~

`POSTGRES_BIND_ADDRESS`、`RUSTFS_BIND_ADDRESS` 和 `RUSTFS_CONSOLE_BIND_ADDRESS` 只控制宿主机端口绑定，默认仍为
`127.0.0.1`。使用私网固定 DHCP 租约或内部 DNS；若绑定 `0.0.0.0`，必须由数据服务器
防火墙限制来源。PostgreSQL 的 `listen_addresses`、`pg_hba.conf`/`hostssl` 规则也必须只
允许算力服务器私网地址使用 TLS + SCRAM。RustFS S3 API 只允许算力服务器访问，Console
只绑定回环或管理网访问。

算力服务器（运行 API、迁移、bootstrap、前端、Caddy 和开发 Keycloak）配置 `.env`：

~~~dotenv
COMPOSE_PROJECT_NAME=reaction-database-compute
TRICYCLE_ENVIRONMENT=production
TRICYCLE_AUTH_MODE=oidc
TRICYCLE_COMPOSE_DATABASE_URL=postgresql+psycopg://reaction_app:<url-encoded-password>@db.lan.example:5433/reactions?sslmode=verify-full&sslrootcert=/etc/reaction-database/ca/internal-ca.pem
TRICYCLE_COMPOSE_RUSTFS_ENDPOINT_URL=https://s3.lan.example:9001
# Same endpoints for a host-run `tricycle-import-artifacts` process.
TRICYCLE_DATABASE_URL=postgresql+psycopg://reaction_app:<url-encoded-password>@db.lan.example:5433/reactions?sslmode=verify-full&sslrootcert=/etc/reaction-database/ca/internal-ca.pem
TRICYCLE_RUSTFS_ENDPOINT_URL=https://s3.lan.example:9001
# Host directory containing internal-ca.pem; the compute overlay mounts it
# read-only at /etc/reaction-database/ca in api/migrate/bootstrap.
TRICYCLE_COMPOSE_CA_DIRECTORY=/etc/reaction-database/ca
TRICYCLE_RUSTFS_ACCESS_KEY=<access-key>
TRICYCLE_RUSTFS_SECRET_KEY=<secret-key>
TRICYCLE_RUSTFS_BUCKET=reactions-raw-files
TRICYCLE_RUSTFS_REGION=us-east-1
TRICYCLE_RUSTFS_VERIFY_TLS=true
TRICYCLE_RUSTFS_CA_BUNDLE=/etc/reaction-database/ca/internal-ca.pem
TRICYCLE_BOOTSTRAP_MODE=production
# The bundled keycloak service is start-dev for local acceptance only. In
# production run Keycloak separately with its own supported database/TLS.
~~~

把私有 CA 放在 `TRICYCLE_COMPOSE_CA_DIRECTORY` 指定的算力服务器目录中；overlay 会将该
目录只读挂载到 API/migration/bootstrap 容器的 `/etc/reaction-database/ca`。如果 CA 已在
基础镜像信任库中，可省略 `sslrootcert`/`TRICYCLE_RUSTFS_CA_BUNDLE`，但仍保持证书校验。
密码中的 `@`、`:`、`/`、`#` 等字符必须 URL 编码。TLS 证书 SAN 应包含应用实际使用的
DNS 名称；不要用裸 IP 规避 DNS，除非证书明确包含该 IP SAN。

在算力服务器执行：

~~~bash
cp .env.example .env
chmod 600 .env
# 修改 .env 后，将 CA 目录以只读卷挂载到 api/migrate/bootstrap，或放入镜像信任库。
docker compose -f compose.yaml -f compose.compute.yaml config --quiet
docker compose -f compose.yaml -f compose.compute.yaml up -d --build --wait
curl --fail https://app.lan.example/health/ready
~~~

不要在算力服务器运行 `docker compose up`（不带 overlay），否则会启动本地 PostgreSQL 和
RustFS。直接存量导入脚本若在宿主机运行，除了 Compose 变量外还要设置同一 endpoint 的
`TRICYCLE_DATABASE_URL` 和 `TRICYCLE_RUSTFS_ENDPOINT_URL`，并确保宿主机能读取 CA 文件。
数据服务器故障仍是该两机拓扑的单点故障；数据库和 RustFS 对象必须分别备份并演练恢复。

前端的 `VITE_*` 是镜像构建参数，修改后必须重新执行 `docker compose build frontend`。
`VITE_API_BASE_URL` 应保持为空，使浏览器通过同一个 HTTPS origin 访问 API。API 镜像也包含
Alembic 和 migrations，可单独执行 `docker compose run --rm migrate`。

### 4.2 部署名称

名称分成“部署显示名”和“稳定协议标识”。Python distribution 名、HTTP 路由、GraphQL
`SystemService` 类型名及数据库表名是兼容性标识，不随部署改名；以下显示名和 NexusX app
键可以覆盖：

| 环境变量 | 作用 | 默认值性质 |
| --- | --- | --- |
| `TRICYCLE_APP_NAME` | OpenAPI、health metadata、`SystemService` | 开发占位 |
| `TRICYCLE_BRAND_NAME` | GraphiQL 标题、邀请邮件主题 | 开发占位 |
| `TRICYCLE_MCP_SERVER_NAME` | MCP server 显示名 | 开发占位 |
| `TRICYCLE_NEXUSX_APP_NAME` | NexusX/MCP 的 app key | 可配置协议键 |
| `TRICYCLE_NEXUSX_PLAYGROUND_NAME` | 只读 playground app key | 可配置协议键 |
| `TRICYCLE_NEXUSX_DATABASE_CLUSTER_NAME` | Voyager 数据库 member 标签 | 开发占位 |
| `TRICYCLE_NEXUSX_DATABASE_CLUSTER_COLOR` | Voyager 数据库 member 的六位十六进制颜色 | 展示配置 |
| `VITE_APP_NAME` | 浏览器标题、导航和登录页 | 前端构建时注入 |
| `VITE_BRAND_NAME` | 前端品牌副标题 | 前端构建时注入 |
| `VITE_APP_TAGLINE` | 前端用途副标题 | 前端构建时注入 |
| `VITE_MCP_SERVER_NAME` | 生成的客户端配置键 | 前端构建时注入 |
| `TRICYCLE_SESSION_COOKIE_NAME` | 后端 Session Cookie 名 | 运行时部署命名空间 |
| `TRICYCLE_CSRF_COOKIE_NAME` / `TRICYCLE_CSRF_HEADER_NAME` | 后端 CSRF Cookie/header 名 | 运行时部署命名空间 |
| `VITE_CSRF_COOKIE_NAME` / `VITE_CSRF_HEADER_NAME` | 前端读取/发送的 CSRF 名称，必须与后端相同 | 前端构建时注入 |
| `COMPOSE_PROJECT_NAME` | 本地 Compose 容器、网络和卷前缀 | 开发基础设施占位 |
| `POSTGRES_DB` / `POSTGRES_USER` | 本地 Compose 数据库和角色名 | 开发基础设施占位 |
| `POSTGRES_BIND_ADDRESS` | 本地 PostgreSQL 宿主机监听地址 | 默认 `127.0.0.1`；两机数据服务器使用私网地址并配合防火墙 |
| `RUSTFS_BIND_ADDRESS` | 本地 RustFS 宿主机监听地址 | 默认 `127.0.0.1`；两机数据服务器使用私网地址并配合防火墙 |
| `RUSTFS_CONSOLE_BIND_ADDRESS` | RustFS Console 宿主机监听地址 | 默认 `127.0.0.1`；不要与 S3 API 一起对算力机或公网开放 |
| `TRICYCLE_DATABASE_URL` | 宿主机/systemd API 使用的 PostgreSQL URL | 运行时连接配置 |
| `TRICYCLE_COMPOSE_DATABASE_URL` | Compose API 容器使用的 PostgreSQL URL | 容器运行时连接配置 |
| `TRICYCLE_COMPOSE_RUSTFS_ENDPOINT_URL` | Compose API 容器使用的 S3 endpoint | 容器运行时连接配置 |
| `TRICYCLE_RUSTFS_BUCKET` | 实际对象存储 bucket | 运行时存储配置 |
| `CADDY_SERVER_NAME` / `CADDY_*_PORT` | HTTPS 域名、HTTP/HTTPS 监听端口和 ACME 证书状态 | Compose 边缘入口配置 |
| `TRICYCLE_OIDC_CA_BUNDLE` / `TRICYCLE_RUSTFS_CA_BUNDLE` / `TRICYCLE_SMTP_CA_BUNDLE` | 私有 CA 的绝对 PEM 路径 | 可选 TLS 信任链 |
| `TRICYCLE_BOOTSTRAP_ORGANIZATION_*` | 初始组织 slug 和显示名 | 首次 bootstrap 注入 |
| `TRICYCLE_BOOTSTRAP_PROJECT_*` | 初始项目 slug 和显示名 | 首次 bootstrap 注入 |

修改 `VITE_*` 后必须重新构建 `frontend/dist`。`VITE_MCP_SERVER_NAME` 是客户端本地配置键，
建议与 `TRICYCLE_NEXUSX_APP_NAME` 相同。`TRICYCLE_*` 环境变量前缀、Python distribution
名、数据库表名和路由属于稳定兼容性标识；它们不会作为部署品牌显示，也不随安装改名。
Compose 中的 `example_reaction_db` 数据库、开发 Keycloak realm 和开发 bucket 只用于本地 fixture，
生产分别由数据库 URL、OIDC issuer/client 和 RustFS/S3 配置替换。

### 4.3 开发环境最小配置

开发环境默认使用固定开发用户，不需要外部 OIDC 或真实邮件：

~~~dotenv
TRICYCLE_ENVIRONMENT=development
TRICYCLE_AUTH_MODE=development
TRICYCLE_DATABASE_URL=postgresql+psycopg://example_user:example-local-password@127.0.0.1:5432/example_reaction_db
TRICYCLE_RUSTFS_ENDPOINT_URL=http://127.0.0.1:19000
TRICYCLE_RUSTFS_ACCESS_KEY=example-local-access
TRICYCLE_RUSTFS_SECRET_KEY=example-local-secret
TRICYCLE_RUSTFS_BUCKET=example-reaction-raw-files
TRICYCLE_EMAIL_DELIVERY_MODE=link
~~~

迁移后显式创建开发占位数据：

~~~bash
uv run alembic upgrade head
uv run tricycle-bootstrap --mode development
~~~

可以通过 `TRICYCLE_BOOTSTRAP_ORGANIZATION_*` 和 `TRICYCLE_BOOTSTRAP_PROJECT_*` 改掉
开发组织/项目占位名称。

### 4.4 生产环境配置模板

以下是必须替换占位符的模板。未列出的限流、查询预算和上传限制使用
.env.example 中的受控默认值。

~~~dotenv
TRICYCLE_ENVIRONMENT=production
TRICYCLE_DEBUG=false
TRICYCLE_API_HOST=127.0.0.1
TRICYCLE_API_PORT=8000
TRICYCLE_APP_NAME=<deployment-service-name>
TRICYCLE_BRAND_NAME=<deployment-brand-name>
TRICYCLE_MCP_SERVER_NAME=<deployment-mcp-name>
TRICYCLE_NEXUSX_APP_NAME=<lowercase-app-key>
TRICYCLE_NEXUSX_PLAYGROUND_NAME=<lowercase-playground-key>
TRICYCLE_NEXUSX_DATABASE_CLUSTER_NAME=<lowercase-database-cluster-key>
TRICYCLE_NEXUSX_DATABASE_CLUSTER_COLOR="#E3F2FD"

TRICYCLE_DATABASE_URL=postgresql+psycopg://<db_user>:<db_password>@db-rw.internal.example:5432/<db_name>?sslmode=verify-full&sslrootcert=/etc/reaction-database/ca/internal-ca.pem

TRICYCLE_AUTH_MODE=oidc
TRICYCLE_SESSION_SECRET=<随机生成的至少32字符密钥>
TRICYCLE_SESSION_COOKIE_SECURE=true
TRICYCLE_OIDC_ISSUER=https://<identity-host>/realms/<realm>
TRICYCLE_OIDC_AUDIENCE=<access-token-audience>
TRICYCLE_OIDC_JWKS_URL=https://<identity-host>/realms/<realm>/protocol/openid-connect/certs
TRICYCLE_OIDC_CA_BUNDLE=/etc/reaction-database/ca/internal-ca.pem
TRICYCLE_OIDC_CLIENT_ID=<client-id>
TRICYCLE_OIDC_CLIENT_SECRET=<client-secret>
TRICYCLE_OIDC_REDIRECT_URI=https://<app-host>/api/auth/callback
TRICYCLE_OIDC_FRONTEND_URL=https://<app-host>

TRICYCLE_RUSTFS_ENDPOINT_URL=https://<s3-host>
TRICYCLE_RUSTFS_ACCESS_KEY=<access-key>
TRICYCLE_RUSTFS_SECRET_KEY=<secret-key>
TRICYCLE_RUSTFS_BUCKET=<private-bucket>
TRICYCLE_RUSTFS_REGION=us-east-1
TRICYCLE_RUSTFS_VERIFY_TLS=true
TRICYCLE_RUSTFS_CA_BUNDLE=/etc/reaction-database/ca/internal-ca.pem

TRICYCLE_EMAIL_DELIVERY_MODE=smtp
TRICYCLE_SMTP_HOST=<smtp-host>
TRICYCLE_SMTP_PORT=587
TRICYCLE_SMTP_USERNAME=<smtp-user>
TRICYCLE_SMTP_PASSWORD=<smtp-password-or-app-password>
TRICYCLE_SMTP_FROM_EMAIL=<verified-sender@example.com>
TRICYCLE_SMTP_STARTTLS=true
TRICYCLE_SMTP_CA_BUNDLE=/etc/reaction-database/ca/internal-ca.pem
TRICYCLE_SMTP_TIMEOUT_SECONDS=15

TRICYCLE_CORS_ORIGINS=["https://<app-host>"]
TRICYCLE_RATE_LIMIT_BACKEND=redis
TRICYCLE_RATE_LIMIT_REDIS_URL=rediss://:<redis-password>@redis-rw.internal.example:6380/0?ssl_ca_certs=/etc/reaction-database/ca/internal-ca.pem
TRICYCLE_RATE_LIMIT_KEY_PREFIX=reaction-database

TRICYCLE_MOLOP_BATCH_N_JOBS=2
~~~

Voyager 使用 NexusX 6.1.2 的 `ComposedErManager` member cluster/color。当前所有数据库实体
属于同一个 PostgreSQL 逻辑 engine，因此配置中只有一个数据库 cluster；即使
`db-rw.internal.example` 后面有多台主备节点，也不能为每台机器创建一个 member。只有新增
拥有互斥实体集合和独立 session factory 的数据库 engine 时，才应在代码中增加 member 和
对应的跨 engine relationship。

NexusX 6.1.2 的 Compose executor 会在执行前严格校验字段 selection；未知字段、缺少嵌套
selection 或对标量附加 selection 都会返回结构化错误，不会静默丢字段。该版本还支持
federation `page_by_*_in` 根的默认排序；本项目没有启用跨数据库 federation，新增 engine
时必须显式声明实体的 `__federation_keys__` 和 `__pagination_orders__`，并在上线验收中
检查默认排序与跨 member 结果一致性。

应用启动时会拒绝以下生产配置：非 OIDC 认证、默认 Session secret、非 Secure Cookie、
非 HTTPS 的 issuer/redirect/JWKS URL、SMTP 465/无 STARTTLS/非法发件域名，以及
TRICYCLE_MOLOP_BATCH_N_JOBS=-1。

数据库 URL 中的特殊字符必须进行 URL 编码。`verify-full` 校验连接主机名，因此 URL 应使用
证书 SAN 中的 DNS 名，不能临时改成裸 IP。不要把数据库、RustFS S3 API 或 Console 暴露公网。

### 4.4 生产 bootstrap

Alembic 只创建 schema。准备好第一个管理员在 OIDC 中不可变的 `iss + sub` 后，设置以下
变量并运行一次幂等 bootstrap：

~~~dotenv
TRICYCLE_BOOTSTRAP_OIDC_ISSUER=https://id.internal.example/realms/chemistry
TRICYCLE_BOOTSTRAP_OIDC_SUBJECT=<administrator-sub-claim>
TRICYCLE_BOOTSTRAP_ADMIN_DISPLAY_NAME=<administrator-display-name>
TRICYCLE_BOOTSTRAP_ADMIN_EMAIL=<administrator-email>
TRICYCLE_BOOTSTRAP_ORGANIZATION_SLUG=<organization-slug>
TRICYCLE_BOOTSTRAP_ORGANIZATION_NAME=<organization-display-name>
TRICYCLE_BOOTSTRAP_PROJECT_SLUG=<project-slug>
TRICYCLE_BOOTSTRAP_PROJECT_NAME=<project-display-name>
~~~

~~~bash
uv run alembic upgrade head
uv run tricycle-bootstrap --mode production
~~~

命令要求 `TRICYCLE_ENVIRONMENT=production`，且 bootstrap issuer 必须与应用 OIDC issuer
完全一致。它只把指定 OIDC 身份设为组织 owner 和初始项目 manager，并写入
`deployment.bootstrap` 审计；不会创建开发用户，也不会给 system service account 授权。

## 5. 多主机部署

下面是一种可直接映射到虚拟机、物理机或不同云服务的部署方式。组件不要求位于同一台机器，
也不依赖 Docker 跨主机网络。`compose.yaml` 是单机开发编排，不应拿 Docker Compose 的
默认网络跨主机拼接生产集群。

| 节点 | 示例 DNS | 运行内容 | 入站来源 |
| --- | --- | --- | --- |
| EDGE-01/02 | `app.example.com` | TLS、静态前端、负载均衡 | 公网 443 |
| API-01/02 | `api01.internal.example` | FastAPI 进程 | 仅 EDGE 到 8000 |
| CACHE-01..N | `redis-rw.internal.example` | Redis 节点及其代理/VIP；共享限流状态 | 仅 API 到 6380/TLS |
| PG-01..N | `db-rw.internal.example` | PostgreSQL/RDKit HA，主备切换由稳定读写端点收敛 | 仅 API/迁移/备份节点到 5432 |
| OBJ-01..N | `s3.internal.example` | 多节点 RustFS；S3 与 Console 使用不同入口 | 仅 API/GC 到 S3 端口，Console 仅管理网 |
| IDP-01..N | `identity.example.org` | Keycloak/OIDC 集群及其稳定 issuer | 浏览器/API 到 443 |
| MAIL-01..N | `smtp.internal.example` | SMTP relay 池 | 仅 API 到 587 |
| SCHED-01 | `scheduler.internal.example` | GC、Session cleanup、迁移/恢复验证 | 到 DB/S3，运维网 SSH |

部署步骤：

1. 为所有逻辑 endpoint 签发证书，把私有 CA PEM 只读挂载到每台 API 节点。数据库证书 SAN
   包含 `db-rw.internal.example`，对象存储证书 SAN 包含 `s3.internal.example`。证书校验对象是
   应用连接的逻辑 DNS 名，而不是被负载均衡隐藏的节点名。
2. PostgreSQL 节点仅监听私网地址，启用 TLS、SCRAM 和 `hostssl` 白名单，每个可能提升为主节点
   的实例都安装相同版本的 RDKit extension。使用 Patroni/云数据库 writer endpoint、VIP 或
   可感知主备角色的数据库代理提供 `db-rw.internal.example`；不能把普通 DNS round-robin
   直接指向主库和只读备库。从唯一迁移节点使用同一个 `TRICYCLE_DATABASE_URL` 执行 Alembic
   和 bootstrap，API 不配置独立只读副本。
3. RustFS 节点启用 HTTPS；bucket 保持 private，S3 API 只对 API/GC 节点开放，Console
   只对管理网开放。多节点 RustFS 的卷、纠删码和负载均衡按 RustFS 版本文档配置。
4. 每台 API 节点安装相同 build、`.env` 和 CA bundle，各运行一个独立 API 进程；所有节点指向同一
   PostgreSQL、bucket、OIDC 和 SMTP。不要在 API 进程中启动 GC 或 session cleanup。
5. 所有 API 和 MCP worker 使用相同的 `TRICYCLE_RATE_LIMIT_REDIS_URL` 和 key prefix；Redis
   使用 TLS、认证和仅允许 API 节点访问的防火墙规则。生产应用在 Redis 不可用时返回 503，
   不回退到进程内额度。当前客户端从一个 `rediss://` URL 建立普通 Redis 连接，不直接解析
   Sentinel 节点列表，也不创建 Redis Cluster client；多节点 Redis 必须通过托管可写 endpoint、
   Sentinel-aware proxy 或 VIP 暴露稳定的 `redis-rw.internal.example`。限流窗口可重建，但部署方
   仍必须监控可用性和容量并演练 endpoint 切换。
6. EDGE 将 `/api`、`/health`、`/graphql`、`/mcp` 和 `/nexusx` 负载均衡到 API 节点；
   MCP 关闭 buffering 并保持 3600 秒 timeout。Caddy 不在边缘重复实现全局限流。
7. 在独立 scheduler 节点以单实例运行 `tricycle-auth-session-cleanup` 和
   `tricycle-rustfs-gc`。这些 CLI 也读取 `TRICYCLE_ENVIRONMENT`，生产环境下独立 RustFS
   配置同样拒绝 HTTP endpoint 或关闭 TLS 校验；数据库备份与对象存储备份独立执行，但使用
   同一恢复点清单关联。
8. OIDC 多节点必须共用 realm/tenant、用户库和签名密钥，并经同一个 issuer URL 发布 discovery
   与 JWKS；切换节点时 issuer 字符串不能变化。SMTP 节点通过 relay DNS/VIP 接入并共用发件人
   策略，API 不感知具体邮件节点。

应用只配置逻辑 endpoint，不配置后端节点清单：

| 应用变量 | 逻辑 endpoint | 多节点切换的责任方 |
| --- | --- | --- |
| `TRICYCLE_DATABASE_URL` | `db-rw.internal.example:5432` | PostgreSQL HA 管理器、托管 writer endpoint 或数据库代理 |
| `TRICYCLE_RUSTFS_ENDPOINT_URL` | `https://s3.internal.example:9000` | RustFS/四层或七层负载均衡器 |
| `TRICYCLE_RATE_LIMIT_REDIS_URL` | `rediss://redis-rw.internal.example:6380/0` | 托管 Redis endpoint、Sentinel-aware proxy 或 VIP |
| `TRICYCLE_OIDC_ISSUER` | `https://identity.example.org/...` | OIDC 入口负载均衡器 |
| `TRICYCLE_SMTP_HOST` | `smtp.internal.example:587` | SMTP relay DNS/VIP |

这一区分很重要：环境变量表达服务发现契约，复制/一致性、选主、仲裁和节点修复由对应基础设施
负责。应用连接池会在逻辑 endpoint 切换后通过 `pool_pre_ping` 淘汰失效 PostgreSQL 连接；这不
替代数据库层对脑裂、事务复制和只读节点误路由的防护。

EDGE 节点可直接使用 `infra/caddy/Caddyfile`。单机时保留 `CADDY_API_UPSTREAM` 中的
`127.0.0.1:8000`；多机时删除该成员并启用实际 API 节点，例如：

~~~dotenv
CADDY_API_UPSTREAM="api01.internal.example:8000 api02.internal.example:8000"
CADDY_FRONTEND_UPSTREAM=frontend.internal.example:8080
~~~

每台 EDGE 必须使用相同 upstream 成员和代理规则。若有多台 EDGE，健康检查和故障摘除应
由它们前面的云负载均衡协调；全局请求预算由 API/MCP worker 共用的 Redis 保证，Caddy 不增加
第二套请求限流。API 节点本身无状态，但会话、权限和科学数据都依赖同一 PostgreSQL，原始
文件都依赖同一逻辑 S3 bucket，因此不能为每个 API 节点配置彼此独立的数据后端。

API 节点的关键连接配置示例：

~~~dotenv
TRICYCLE_API_HOST=0.0.0.0
TRICYCLE_DATABASE_URL=postgresql+psycopg://app:<url-encoded-password>@db-rw.internal.example:5432/reactions?sslmode=verify-full&sslrootcert=/etc/reaction-database/ca/internal-ca.pem
TRICYCLE_RUSTFS_ENDPOINT_URL=https://s3.internal.example:9000
TRICYCLE_RUSTFS_CA_BUNDLE=/etc/reaction-database/ca/internal-ca.pem
TRICYCLE_RATE_LIMIT_BACKEND=redis
TRICYCLE_RATE_LIMIT_REDIS_URL=rediss://:<password>@redis-rw.internal.example:6380/0?ssl_ca_certs=/etc/reaction-database/ca/internal-ca.pem
TRICYCLE_OIDC_ISSUER=https://id.example.com/realms/chemistry
TRICYCLE_OIDC_JWKS_URL=https://id.example.com/realms/chemistry/protocol/openid-connect/certs
TRICYCLE_SMTP_HOST=smtp.internal.example
TRICYCLE_SMTP_PORT=587
~~~

完整、可由 `infra/systemd/reaction-database-api.service` 读取的示例位于
[`infra/deployment/multi-host-api.env.example`](../infra/deployment/multi-host-api.env.example)。
部署时由配置管理将同一份非 secret 配置和每节点可读取的 Secret 渲染到
`/etc/reaction-database/application.env`，权限设为 root/reactiondb `0640`。模板中的
`replace-*` 只是明显的占位符，不能作为生产 secret。

从每台 API 节点上线前执行只读连通性检查：

~~~bash
getent hosts db-rw.internal.example s3.internal.example redis-rw.internal.example identity.example.org smtp.internal.example
openssl s_client -connect db-rw.internal.example:5432 -starttls postgres -verify_return_error </dev/null
curl --fail --cacert /etc/reaction-database/ca/internal-ca.pem https://s3.internal.example:9000/health
openssl s_client -connect redis-rw.internal.example:6380 -verify_return_error </dev/null
curl --fail https://identity.example.org/realms/chemistry/.well-known/openid-configuration
openssl s_client -connect smtp.internal.example:587 -starttls smtp -verify_return_error </dev/null
~~~

这些检查只证明 DNS/TLS/端口可达。随后还要运行 `/health/ready`、真实 S3 HEAD/PUT/GET、
OIDC 登录、SMTP 邀请，以及上传/下载 smoke。防火墙不要允许浏览器直接访问 5432、6380、
9000、9001 或 API 内网端口。Prometheus 只能从监控网访问各 API 节点的
`/internal/metrics`；公网 EDGE 对 `/internal/` 固定返回 404。

## 6. PostgreSQL/RDKit

### 自行运行

~~~bash
docker compose up -d --wait postgres
uv run alembic upgrade head
uv run alembic current
uv run alembic check
~~~

生产环境如果使用 Compose，必须覆盖数据库密码、限制端口访问并配置持久卷备份。不要在应用
启动时执行 SQLModel.metadata.create_all()；所有 schema 变化必须使用 Alembic。

### 使用托管 PostgreSQL

需要确认服务商允许安装 RDKit extension，并实际执行：

~~~sql
SELECT extversion FROM pg_extension WHERE extname = 'rdkit';
~~~

没有 RDKit extension 时，/health/ready 会失败，结构查询和迁移不能视为可用。

## 7. RustFS/S3 对象存储

RustFS 是原始计算文件的存储后端；PostgreSQL 保存 artifact 索引、哈希、解析状态和科学
事实。可以使用 RustFS，也可以使用兼容 S3 Signature V4 的对象存储。

必须准备：

1. 私有 bucket 和只允许 API 使用的 access key/secret key。
2. HTTPS endpoint；生产环境保持 TRICYCLE_RUSTFS_VERIFY_TLS=true。
3. 对象存储的版本、删除保护、生命周期和备份策略。
4. API 主机到 endpoint 的网络连通性。

RustFS 可以在多台存储主机上组成集群，但 API 仍只配置一个 HTTPS S3 endpoint。负载均衡器
必须支持完整对象上传、Range GET、HEAD、S3 错误响应和足够长的超时；Console 使用独立管理网
入口，不能与公网 S3 endpoint 共用暴露策略。节点数、纠删码布局和磁盘故障域必须按所部署的
RustFS 固定版本验证，本仓库不替代 RustFS 集群编排。

上传流程先在 PostgreSQL 写入 pending，再写对象并校验 SHA-256，成功后变为 available。
启用 bucket versioning 时还会保存 S3 `VersionId`，后续预览、下载、退役和恢复验收都指向该
精确版本，不读取同 key 的偶然最新版本。PostgreSQL 与对象存储不是一个事务，因此必须分别
备份和演练恢复。

建议通过 cron、systemd timer 或 Kubernetes CronJob 定期运行：

~~~bash
uv run tricycle-rustfs-gc
~~~

不要在 FastAPI 多 worker 内启动 GC 常驻循环。

## 8. OIDC 身份服务

生产配置必须使用 authorization-code + PKCE。可以使用生产 Keycloak 或其他兼容 OIDC 的
身份提供方；当前 Compose 中的 Keycloak 只适合开发。

在身份服务中创建一个 confidential client，并登记：

~~~text
回调 URI：    https://<app-host>/api/auth/callback
前端地址：    https://<app-host>
允许 scope：  openid profile email
~~~

必须能从 issuer 的 discovery 文档获取 authorization endpoint 和 token endpoint，且
TRICYCLE_OIDC_JWKS_URL 可被 API 主机访问。访问令牌的 aud 必须包含配置的
TRICYCLE_OIDC_AUDIENCE。OIDC token 至少需要 iss、sub、exp、iat、aud；建议提供
email、name 或 preferred_username。项目邀请依赖 email claim，且接受邀请时邮箱必须匹配。
使用私有 CA 时设置绝对 PEM 路径 `TRICYCLE_OIDC_CA_BUNDLE`；该证书链会同时用于 discovery、
authorization-code token exchange 和 JWKS 下载，并验证身份服务主机名。
生产运行时还会拒绝 issuer 不精确匹配、authorization/token/logout endpoint 非 HTTPS，或
discovery 未声明 `code_challenge_methods_supported: ["S256", ...]` 的身份服务。

首次生产用户登录后，部署管理员需要在 PostgreSQL/受控管理流程中把该用户加入
system organization 并授予 owner/admin。普通项目 manager 不能提升全局管理员权限。

## 9. SMTP 邮件服务

SMTP 只发送项目邀请，不发送登录验证码、密码重置或账户注册邮件；这些属于 OIDC 提供方
的职责。邀请邮件正文是纯文本，邀请状态会保存为 sent 或 failed，失败后可以调用重发接口。

### 邮箱服务商需要提供的配置

~~~dotenv
TRICYCLE_EMAIL_DELIVERY_MODE=smtp
TRICYCLE_SMTP_HOST=smtp.example.com
TRICYCLE_SMTP_PORT=587
TRICYCLE_SMTP_USERNAME=noreply@example.com
TRICYCLE_SMTP_PASSWORD=<密码或应用专用密码>
TRICYCLE_SMTP_FROM_EMAIL=noreply@example.com
TRICYCLE_SMTP_STARTTLS=true
TRICYCLE_SMTP_TIMEOUT_SECONDS=15
~~~

当前实现使用 smtplib.SMTP + STARTTLS，推荐端口 587。TLS 使用系统 CA 或绝对路径
`TRICYCLE_SMTP_CA_BUNDLE`，并强制验证证书链和 `TRICYCLE_SMTP_HOST` 主机名。它没有使用 SMTP_SSL，因此只提供
465 隐式 SSL 的服务商不能仅靠环境变量接入；应使用支持 587 STARTTLS 的 SMTP relay，或
扩展邮件适配器。

还需要在发件人域名配置 SPF、DKIM 和 DMARC，并确认服务器允许出站 TCP 587。From 地址
应是服务商验证过的域名/邮箱。配置校验会检查 host、纯 mailbox 格式和 DNS 域名，但无法从
应用配置证明服务商已验证该域名；用户名、密码、SPF/DKIM/DMARC 和送达情况必须通过实际发信验证。

### 邮件验证

1. 使用已登录且有 project manage 权限的账户创建项目邀请。
2. 检查接口返回的 delivery_status 是否为 sent。
3. 检查收件箱中的链接是否指向 TRICYCLE_OIDC_FRONTEND_URL。
4. 使用与邀请邮箱相同的 OIDC 账户登录并接受邀请。
5. 故意配置错误 SMTP，确认邀请记录为 failed，再调用 resend 接口恢复。

## 10. 前端与反向代理

### 构建前端

同源生产部署将 VITE_API_BASE_URL 留空，并在自定义 CSRF 名称时同时设置前后端值：

~~~bash
npm --prefix frontend ci
VITE_APP_NAME="Example Chemistry Database" \
VITE_BRAND_NAME="Example Research Platform" \
VITE_CSRF_COOKIE_NAME=example_chemistry_csrf \
VITE_CSRF_HEADER_NAME=x-example-chemistry-csrf \
npm --prefix frontend run build
~~~

构建结果在 frontend/dist，由 Caddy/静态服务器提供（前端镜像内部仍可使用 Nginx）。开发代理目标由
VITE_API_PROXY_TARGET 控制，生产不应把开发端口暴露给浏览器。

### 代理路径

反向代理必须转发：

~~~text
/api/          -> FastAPI 8000
/health/       -> FastAPI 8000
/docs          -> FastAPI 8000
/docs/oauth2-redirect -> FastAPI 8000
/redoc         -> FastAPI 8000
/openapi.json  -> FastAPI 8000
/graphql*      -> FastAPI 8000
/mcp/          -> FastAPI 8000（或拆分的 NexusX MCP 上游）
/nexusx/*      -> FastAPI 8000，或内网中的拆分 NexusX 进程
~~~

SPA 必须启用 history fallback 到 index.html。上传请求的代理 body 限制不能小于
TRICYCLE_MAX_BATCH_BYTES，当前默认批次总大小为 256 MiB；单文件默认 64 MiB。

可以从 infra/caddy/Caddyfile 开始配置。当前示例已经显式代理 `/health/live`、
`/health/ready`、`/docs`、`/docs/oauth2-redirect`、`/redoc`、`/openapi.json`、GraphQL
和 `/nexusx/*`，并包含 `/api/` 的 `private, no-store`、应用侧批次大小校验和
MCP 的无缓冲/3600 秒超时。正式配置必须保留这些代理路由和缓存边界；否则健康检查可能被
SPA fallback 接管，或 MCP 长连接被短超时/缓冲截断。

如果外层使用 Cloudflare，必须为 /api/* 建立 bypass cache 规则。不能只依赖应用返回的
Cache-Control 头来纠正边缘强制缓存。

同源部署通常不需要额外 CORS。若前端和 API 分域，构建时设置 VITE_API_BASE_URL，并将
TRICYCLE_CORS_ORIGINS 设置为精确的 HTTPS 前端 origin；同时确认浏览器请求携带 Cookie，
以及下载、预览和 depiction 请求都经过同一认证策略。

## 11. 启动顺序

~~~bash
uv sync --frozen --python 3.12
npm --prefix frontend ci
uv run alembic upgrade head
uv run tricycle-bootstrap --mode production
npm --prefix frontend run build
uv run tricycle-api
~~~

API 默认只监听 127.0.0.1:8000，由反向代理对外提供 HTTPS。`infra/systemd/` 提供可直接安装
的单进程 API service；生产通过多个 API 主机和 Caddy upstream 横向扩容。当前 Prometheus
指标是进程内状态，不应在同一个监听端口启动 Uvicorn 多 worker，否则抓取请求只会随机命中
其中一个 worker。若未来引入 Prometheus multiprocess 聚合，同机多 worker 仍必须按
“Uvicorn worker 数 × TRICYCLE_MOLOP_BATCH_N_JOBS”评估 MolOP 解析进程的 CPU 和内存；
MolOP 解析并发由解析进程池 worker 数控制；请求不会再经过额外的 slot 闸门。
生产不得设置 `TRICYCLE_MOLOP_BATCH_N_JOBS=-1`。

## 12. 定时任务、备份与验收

建议配置：

~~~bash
uv run tricycle-auth-session-cleanup
uv run tricycle-rustfs-gc
~~~

可直接安装的 systemd unit、Prometheus 告警规则、备份清单和隔离恢复步骤见
[生产运维与恢复 Runbook](operations-runbook.md)。

备份必须覆盖：

- PostgreSQL 数据库和 Alembic 版本记录。
- RustFS/S3 bucket 对象及其版本/生命周期策略。
- OIDC provider 的 realm/client 配置、签名密钥和身份数据库。
- .env 中的生产 Secret（通过密码管理器备份，不要写入 Git）。

上线前先从每个 API 节点执行只产生一个临时 Redis key 的依赖 smoke：

~~~bash
install -d -m 0700 deployment-evidence
uv run --frozen tricycle-deployment-smoke \
  | tee "deployment-evidence/dependency-smoke-$(hostname)-$(date -u +%Y%m%dT%H%M%SZ).json"
test "${PIPESTATUS[0]}" -eq 0
~~~

该命令验证公网 live/ready、OIDC discovery/JWKS/PKCE S256、PostgreSQL TLS/writer/RDKit、
RustFS HTTPS bucket versioning、Redis TLS/Lua 写入与 SMTP STARTTLS 证书/主机名。Redis probe
使用部署 key prefix 下的随机 key，设置 60 秒 TTL 并在结束时删除。命令不发送邮件、不创建
领域记录，也不替代 OIDC 登录、Artifact 精确版本下载或故障切换演练。输出不得包含 secret，
应与[部署验收记录模板](../infra/deployment/acceptance-record.example.md)一同归档。

然后执行：

~~~bash
curl -fsS https://<app-host>/health/live
curl -fsS https://<app-host>/health/ready
uv run alembic current
uv run alembic check
caddy validate --config infra/caddy/Caddyfile --adapter caddyfile
~~~

并完成一次真实 OIDC 登录、退出、项目邀请邮件、邀请接受、artifact 上传/下载和备份恢复
演练。/health/ready 必须确认 PostgreSQL 和 RDKit extension 均正常；仅 /health/live 成功
不能证明应用可用。

## 13. 常见问题

### 应用启动时报 production requires

检查 TRICYCLE_ENVIRONMENT=production 时是否同时设置了 OIDC 三件套、client secret、
HTTPS redirect URI、至少 32 字符的 session secret、TRICYCLE_SESSION_COOKIE_SECURE=true
以及正整数的 MolOP 并行数。

### 邀请接口返回 failed

优先检查 SMTP host/端口、防火墙出站规则、STARTTLS、用户名和 app password、发件人域名验证，
以及 API 主机的 DNS/TLS。错误会保存在邀请的 delivery error 字段中，修复后使用 resend。

### 登录后回调失败

逐字比较身份服务登记的回调 URI 和 TRICYCLE_OIDC_REDIRECT_URI，确认 issuer/JWKS 可从 API
主机访问，并确认反向代理传递 Host、X-Forwarded-Host 和 X-Forwarded-Proto。

### 前端打开但 API 请求失败

检查浏览器请求是否指向公网同源 /api，不要把 127.0.0.1 写入生产的 VITE_API_BASE_URL。
分域部署时同时检查 HTTPS CORS allowlist、Cookie 和 depiction 请求。
