# 环境变量参考

按功能查找项目直接读取的环境变量。功能页列出代码默认值、校验边界及非 Python 变量的示例值。

应用配置按运行查询、MCP 导出、导入任务、身份邮件和存储连接拆为独立 Pydantic 模型，由 `ApplicationSettings` 组合。RustFS、存储清理、前端构建和 Compose 也有独立模型，统一定义在 [core/config.py](../src/tricycle_reaction_db/core/config.py)。`load_configuration()` 加载后端设置，`load_project_configuration()` 组合全部模型并校验跨域关系。

Python 设置读取进程环境和当前工作目录的 `.env`；Compose 使用 `.env` 插值并传给服务。后端修改后重启相关服务；`VITE_*` 修改后重新构建前端。

运行 `make validate-config` 预检根目录 `.env`；`make frontend-build` 会预检 `frontend/.env`。

生产组合校验见 [部署配置](deployment-configuration.md)。配置声明变更后运行 `make env-docs` 同步中英文页面。

| 功能 | 内容 | 变量数 |
| --- | --- | ---: |
| [运行与查询](environment/runtime-query.md) | 服务名称、数据库、GraphQL、查询预算与统计。 | 37 |
| [MCP 与数据导出](environment/mcp-export.md) | MCP 响应上限、TS 几何与 UniTS 导出。 | 19 |
| [导入与后台任务](environment/ingestion-workers.md) | 上传、解析、持久化、维护命令与 worker。 | 56 |
| [身份认证与邮件](environment/identity-email.md) | OIDC、会话、Bootstrap、邮件与跨域。 | 45 |
| [对象存储与清理](environment/storage.md) | RustFS/S3 连接与垃圾回收。 | 22 |
| [前端构建与分页](environment/frontend.md) | Vite 构建变量、分页与预取。 | 19 |
| [Compose 与基础设施](environment/deployment.md) | 容器镜像、PostgreSQL、Caddy、Keycloak 与构建代理。 | 41 |
