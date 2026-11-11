"""Split the generated environment-variable table into function-specific pages."""

# ruff: noqa: E501

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = {
    "runtime-query": (
        "运行与查询",
        "Runtime and query",
        "服务名称、数据库、GraphQL、查询预算与统计。",
        "Service identity, database, GraphQL, query budgets, and statistics.",
    ),
    "mcp-export": (
        "MCP 与数据导出",
        "MCP and data export",
        "MCP 响应上限、TS 几何与 UniTS 导出。",
        "MCP response budgets, TS geometry, and UniTS exports.",
    ),
    "ingestion-workers": (
        "导入与后台任务",
        "Ingestion and workers",
        "上传、解析、持久化、维护命令与 worker。",
        "Uploads, parsing, persistence, maintenance commands, and workers.",
    ),
    "identity-email": (
        "身份认证与邮件",
        "Identity and email",
        "OIDC、会话、Bootstrap、邮件与跨域。",
        "OIDC, sessions, bootstrap, email, and CORS.",
    ),
    "storage": (
        "对象存储与清理",
        "Object storage and cleanup",
        "RustFS/S3 连接与垃圾回收。",
        "RustFS/S3 connections and garbage collection.",
    ),
    "frontend": (
        "前端构建与分页",
        "Frontend build and pagination",
        "Vite 构建变量、分页与预取。",
        "Vite build variables, pagination, and prefetch.",
    ),
    "deployment": (
        "Compose 与基础设施",
        "Compose and infrastructure",
        "容器镜像、PostgreSQL、Caddy、Keycloak 与构建代理。",
        "Container images, PostgreSQL, Caddy, Keycloak, and build proxies.",
    ),
}
NOTES = {
    "runtime-query": (
        "后端设置在进程启动时读取；修改后重启相关服务。",
        "Backend settings are read at process startup; restart affected services after changes.",
    ),
    "mcp-export": (
        "TS 几何导出使用独立的 MCP 字节与记录上限，其他 JSONL/CSV 导出使用通用上限。UniTS 反应中心容量会改变导出数组形状，需与消费端协调。",
        "TS geometry has separate MCP byte and record budgets; other JSONL/CSV exports use the general budgets. UniTS reaction-center capacities change array shapes and must be coordinated with consumers.",
    ),
    "ingestion-workers": (
        "`TRICYCLE_IMPORT_*` 和 `TRICYCLE_RECONCILE_*` 是本地命令默认值，命令行选项优先。Compose 的 worker statement timeout 会映射到各进程的 `TRICYCLE_QUERY_STATEMENT_TIMEOUT_MS`。",
        "`TRICYCLE_IMPORT_*` and `TRICYCLE_RECONCILE_*` set local-command defaults; CLI options take precedence. Compose maps worker statement timeouts to each process's `TRICYCLE_QUERY_STATEMENT_TIMEOUT_MS`.",
    ),
    "identity-email": (
        "生产环境的 OIDC、SMTP、Cookie 与 TLS 组合校验见 [部署配置](../deployment-configuration.md)。",
        "For production OIDC, SMTP, cookie, and TLS checks, see [deployment configuration](../deployment-configuration.md).",
    ),
    "storage": (
        "独立的 RustFS 与存储垃圾回收设置类读取各自前缀的变量；`TRICYCLE_ENVIRONMENT` 也决定 RustFS 的生产 TLS 校验。",
        "Separate RustFS and storage-GC settings classes read their own prefixes; `TRICYCLE_ENVIRONMENT` also controls production RustFS TLS checks.",
    ),
    "frontend": (
        "`VITE_*` 在构建时注入，修改后重新构建前端。`make frontend-build` 会用 `FrontendBuildSettings` 校验分页值及默认值与上限的关系；`VITE_PAGE_PREFETCH_RADIUS=0` 关闭邻页预取。",
        "`VITE_*` values are injected at build time; rebuild the frontend after changes. `make frontend-build` validates pagination values and their limits with `FrontendBuildSettings`; `VITE_PAGE_PREFETCH_RADIUS=0` disables adjacent-page prefetch.",
    ),
    "deployment": (
        "`TRICYCLE_COMPOSE_DATABASE_URL` 和 `TRICYCLE_COMPOSE_RUSTFS_ENDPOINT_URL` 是容器内覆盖地址；宿主机进程分别使用应用设置中的地址。`make validate-config` 用独立的 `ComposeBuildSettings` 校验端口、worker 超时和连接地址。`.env.example` 中的密钥仅为开发示例。",
        "The Compose database and RustFS URLs override container endpoints; host processes use the application settings. `make validate-config` checks ports, worker timeouts, and connection URLs through a separate `ComposeBuildSettings` model. Keys in `.env.example` are development examples only.",
    ),
}


def application_group(name: str) -> str:
    field = name.removeprefix("TRICYCLE_").lower()
    if field in {
        "app_name",
        "brand_name",
        "mcp_server_name",
        "environment",
        "debug",
        "api_host",
        "api_port",
    } or field.startswith("nexusx_"):
        return "runtime-query"
    if field in {"mcp_token_ttl_seconds", "mcp_token_last_used_interval_seconds"}:
        return "identity-email"
    if field.startswith(("mcp_", "mapped_geometry_export_", "units_ts_", "units_dataset_worker_")):
        return "mcp-export"
    if field.startswith("rustfs_"):
        return "storage"
    if field.startswith(
        (
            "auth_",
            "development_user_",
            "oidc_",
            "bootstrap_",
            "session_",
            "csrf_",
            "email_",
            "smtp_",
            "cors_",
        )
    ):
        return "identity-email"
    if field.startswith(
        (
            "import_",
            "upload_",
            "max_upload_",
            "max_batch_",
            "molop_",
            "frame_conversion_",
            "inference_persist_",
            "transaction_retry_",
            "reconcile_",
            "profile_refresh_",
            "fast_copy_",
            "ts_endpoint_",
        )
    ):
        return "ingestion-workers"
    if field.startswith(
        (
            "database_",
            "graphql_",
            "query_",
            "read_rate_",
            "rate_limit_",
            "structure_",
            "molecule_query_",
            "depiction_",
            "molecular_graph_",
            "analytics_",
            "geometry_match_",
            "slow_query_",
        )
    ):
        return "runtime-query"
    raise ValueError(f"unclassified application setting: {name}")


def external_group(name: str) -> str:
    if name.startswith("VITE_"):
        return "frontend"
    if name.startswith("RUSTFS_"):
        return "storage"
    if name in {
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "TRICYCLE_FAST_COPY",
    } or name.startswith(
        (
            "TRICYCLE_BENCHMARK_",
            "TRICYCLE_UPLOAD_WORKER_STATEMENT_",
            "TRICYCLE_PROFILE_REFRESH_STATEMENT_",
        )
    ):
        return "ingestion-workers"
    if name == "TRICYCLE_UNITS_DATASET_WORKER_STATEMENT_TIMEOUT_MS":
        return "mcp-export"
    if name == "TRICYCLE_BOOTSTRAP_MODE":
        return "identity-email"
    return "deployment"


def split(language: str) -> None:
    root = ROOT / "docs" if language == "zh" else ROOT / "docs/en"
    source = root / "environment-variables.md"
    grouped: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    seen: set[str] = set()
    section = ""
    for line in source.read_text().splitlines():
        if line.startswith("## "):
            section = line
        match = re.match(r"\| `([A-Za-z_][A-Za-z0-9_]*)` \|", line)
        if match is None:
            continue
        name = match.group(1)
        if name in seen:
            continue  # TRICYCLE_ENVIRONMENT is shared by application and RustFS settings.
        seen.add(name)
        if "RustFS" in section or "Storage garbage" in section or "存储垃圾" in section:
            group, kind = "storage", "settings"
        elif "Application settings" in section or "应用配置" in section:
            group, kind = application_group(name), "settings"
        else:
            group, kind = external_group(name), "external"
        grouped[group][kind].append(line)

    directory = root / "environment"
    directory.mkdir(exist_ok=True)
    for slug, info in PAGES.items():
        title = info[0 if language == "zh" else 1]
        description = info[2 if language == "zh" else 3]
        note = NOTES[slug][0 if language == "zh" else 1]
        lines = [
            f"# {title}环境变量" if language == "zh" else f"# {title} environment variables",
            "",
            description,
            "",
            "[返回环境变量索引](../environment-variables.md)"
            if language == "zh"
            else "[Back to the environment-variable index](../environment-variables.md)",
            "",
        ]
        for kind in ("settings", "external"):
            rows = grouped[slug][kind]
            if not rows:
                continue
            if kind == "settings":
                lines += [
                    "## 后端设置" if language == "zh" else "## Backend settings",
                    "",
                    "| 变量 | 代码默认值 | 数值或格式约束 |"
                    if language == "zh"
                    else "| Variable | Code default | Value constraints |",
                    "| --- | --- | --- |",
                    *rows,
                    "",
                ]
            else:
                lines += [
                    "## Compose、前端与脚本变量"
                    if language == "zh"
                    else "## Compose, frontend, and script variables",
                    "",
                    "| 变量 | `.env.example` 示例值 | 来源 |"
                    if language == "zh"
                    else "| Variable | `.env.example` example | Source |",
                    "| --- | --- | --- |",
                    *rows,
                    "",
                ]
        lines += ["## 配置关系" if language == "zh" else "## Configuration notes", "", note, ""]
        (directory / f"{slug}.md").write_text("\n".join(lines))

    if language == "zh":
        index = [
            "# 环境变量参考",
            "",
            "按功能查找项目直接读取的环境变量。功能页列出代码默认值、校验边界及非 Python 变量的示例值。",
            "",
            "应用配置按运行查询、MCP 导出、导入任务、身份邮件和存储连接拆为独立 Pydantic 模型，由 `ApplicationSettings` 组合。RustFS、存储清理、前端构建和 Compose 也有独立模型，统一定义在 [core/config.py](../src/tricycle_reaction_db/core/config.py)。`load_configuration()` 加载后端设置，`load_project_configuration()` 组合全部模型并校验跨域关系。",
            "",
            "Python 设置读取进程环境和当前工作目录的 `.env`；Compose 使用 `.env` 插值并传给服务。后端修改后重启相关服务；`VITE_*` 修改后重新构建前端。",
            "",
            "运行 `make validate-config` 预检根目录 `.env`；`make frontend-build` 会预检 `frontend/.env`。",
            "",
            "生产组合校验见 [部署配置](deployment-configuration.md)。配置声明变更后运行 `make env-docs` 同步中英文页面。",
            "",
            "| 功能 | 内容 | 变量数 |",
            "| --- | --- | ---: |",
        ]
    else:
        index = [
            "# Environment Variable Reference",
            "",
            "Browse the project's environment variables by function. Each page lists code defaults, validation bounds, and examples for variables outside Python settings.",
            "",
            "Application settings use separate Pydantic models for runtime queries, MCP exports, ingestion workers, identity and email, and storage connections, assembled by `ApplicationSettings`. RustFS, storage GC, frontend build, and Compose have separate models in [core/config.py](../../src/tricycle_reaction_db/core/config.py). `load_configuration()` loads backend settings; `load_project_configuration()` assembles all models and checks cross-domain consistency.",
            "",
            "Python settings read the process environment and the current working directory's `.env`; Compose interpolates `.env` and passes it to services. Restart affected backend services after changes; rebuild the frontend after changing `VITE_*`.",
            "",
            "Run `make validate-config` to preflight the root `.env`; `make frontend-build` preflights `frontend/.env`.",
            "",
            "See [deployment configuration](deployment-configuration.md) for production cross-field checks. Run `make env-docs` after changing declarations to regenerate both languages.",
            "",
            "| Function | Contents | Variables |",
            "| --- | --- | ---: |",
        ]
    for slug, info in PAGES.items():
        title = info[0 if language == "zh" else 1]
        description = info[2 if language == "zh" else 3]
        count = sum(len(rows) for rows in grouped[slug].values())
        index.append(f"| [{title}](environment/{slug}.md) | {description} | {count} |")
    index.append("")
    source.write_text("\n".join(index))


if __name__ == "__main__":
    for lang in ("zh", "en"):
        split(lang)
