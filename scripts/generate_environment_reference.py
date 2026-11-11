"""Generate the repository's environment-variable reference from source declarations."""

# ruff: noqa: E501

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic_core import PydanticUndefined

from tricycle_reaction_db.core.config import (
    IdentityEmailSettings,
    IngestionWorkerSettings,
    McpExportSettings,
    RuntimeSettings,
    RustFSSettings,
    StorageClientSettings,
    StorageGarbageCollectionSettings,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / ".env.example"
OUTPUT = ROOT / "docs" / "environment-variables.md"


def display(value: Any) -> str:
    if value is PydanticUndefined or value is None:
        return "未设置"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return str(value).replace("|", "\\|").replace("`", "\\`") or "（空）"


def bounds(field: Any) -> str:
    constraints: list[str] = []
    for item in field.metadata:
        for name, symbol in (("ge", "≥"), ("gt", ">"), ("le", "≤"), ("lt", "<")):
            value = getattr(item, name, None)
            if value is not None:
                constraints.append(f"{symbol}{value}")
        pattern = getattr(item, "pattern", None)
        if pattern:
            constraints.append(f"regex: {pattern}")
    return ", ".join(constraints) or "—"


def settings_rows(settings: type[Any], prefix: str) -> tuple[list[str], set[str]]:
    rows: list[str] = []
    names: set[str] = set()
    for name, field in settings.model_fields.items():
        alias = field.validation_alias
        variable = str(alias) if alias else prefix + name.upper()
        if variable in names:
            continue
        names.add(variable)
        default = field.default
        if default is PydanticUndefined and field.default_factory is not None:
            default = field.default_factory()
        rows.append(f"| `{variable}` | `{display(default)}` | {bounds(field)} |")
    return rows, names


def main() -> None:
    example_defaults: dict[str, str] = {}
    for line in EXAMPLE.read_text().splitlines():
        match = re.match(r"(?:#\s*)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if match:
            example_defaults.setdefault(match.group(1), match.group(2))

    app_rows: list[str] = []
    app_names: set[str] = set()
    for domain in (
        RuntimeSettings,
        McpExportSettings,
        IngestionWorkerSettings,
        IdentityEmailSettings,
        StorageClientSettings,
    ):
        rows, names = settings_rows(domain, "TRICYCLE_")
        if app_names & names:
            raise ValueError(f"duplicate application settings: {app_names & names}")
        app_rows.extend(rows)
        app_names.update(names)
    rustfs_rows, rustfs_names = settings_rows(RustFSSettings, "TRICYCLE_RUSTFS_")
    gc_rows, gc_names = settings_rows(StorageGarbageCollectionSettings, "TRICYCLE_STORAGE_GC_")
    declared = app_names | rustfs_names | gc_names

    compose_names: set[str] = set()
    for path in ROOT.glob("compose*.yaml"):
        compose_names.update(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", path.read_text()))
    vite_names = set(
        re.findall(r"\b(VITE_[A-Z0-9_]+)\b", (ROOT / "frontend/src/env.d.ts").read_text())
    )
    direct_names = {
        "TRICYCLE_FAST_COPY",
        "TRICYCLE_BENCHMARK_SYNCHRONOUS_COMMIT_OFF",
        "TRICYCLE_BENCHMARK_DISABLE_PROCESS_MONITOR",
        "TRICYCLE_ACCEPTANCE_BEARER_TOKEN",
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NO_PROXY",
        "no_proxy",
    }
    other_names = (set(example_defaults) | compose_names | vite_names | direct_names) - declared

    lines = [
        "# 环境变量参考",
        "",
        "本页列出项目直接读取的应用配置、RustFS 和存储 GC 配置、Compose 变量、前端构建变量及脚本开关。",
        "Python 配置读取进程环境和当前工作目录的 `.env`；Compose 使用 `.env` 插值并将其传给 API/worker。",
        "调整后端变量后重启相应服务；`VITE_*` 是构建时变量，调整后重新构建前端。",
        "生产环境必填项及组合校验见 [部署配置](deployment-configuration.md)。",
        "配置定义变化后运行 `make env-docs` 同步中英文索引。",
        "",
        "## 应用配置（按功能拆分）",
        "",
        "变量名由 `TRICYCLE_` 加字段名大写组成。以下默认值来自代码，不读取本机 `.env`。数值边界为配置校验边界。",
        "",
        "| 变量 | 代码默认值 | 数值或格式约束 |",
        "| --- | --- | --- |",
        *app_rows,
        "",
        "## RustFS 配置",
        "",
        "| 变量 | 代码默认值 | 数值或格式约束 |",
        "| --- | --- | --- |",
        *rustfs_rows,
        "",
        "## 存储垃圾回收配置",
        "",
        "| 变量 | 代码默认值 | 数值或格式约束 |",
        "| --- | --- | --- |",
        *gc_rows,
        "",
        "## Compose、前端与脚本变量",
        "",
        "下表列出不属于上述 Python 设置类、但由项目 Compose、前端或脚本读取的变量。默认值以 `.env.example` 为准；“未列出”表示按使用位置决定或需要显式提供。",
        "",
        "| 变量 | `.env.example` 示例值 | 来源 |",
        "| --- | --- | --- |",
    ]
    for name in sorted(other_names):
        source = (
            ", ".join(
                label
                for label, membership in (
                    ("Compose", name in compose_names),
                    ("Vite", name in vite_names),
                    ("脚本/进程", name in direct_names),
                )
                if membership
            )
            or "`.env.example`"
        )
        lines.append(f"| `{name}` | `{display(example_defaults.get(name))}` | {source} |")

    lines.extend(
        [
            "",
            "## 配置关系",
            "",
            "- `TRICYCLE_MCP_TS_GEOMETRY_EXPORT_MAX_BYTES` 只控制 TS 几何 MCP 导出；其他 MCP JSONL/CSV 使用 `TRICYCLE_MCP_EXPORT_MAX_BYTES`。字节上限按序列化记录累计，响应封装本身另有少量开销。",
            "- `TRICYCLE_UNITS_TS_MAX_REACTIVE_ATOMS`、`TRICYCLE_UNITS_TS_MAX_RC_BONDS`、`TRICYCLE_UNITS_TS_MAX_RC_ANGLES` 会改变 UniTS 导出数组形状。变更前应确认消费端支持新形状，并保持同一次导出的 worker 配置一致。",
            "- `VITE_DEFAULT_REACTION_PAGE_SIZE` 和 `VITE_DEFAULT_CATALOG_PAGE_SIZE` 会在前端按各自最大页大小截断；`VITE_PAGE_PREFETCH_RADIUS` 可以设为 `0` 关闭邻页预取。无效的前端整数值回退到代码默认值。",
            "- `TRICYCLE_COMPOSE_DATABASE_URL` 和 `TRICYCLE_COMPOSE_RUSTFS_ENDPOINT_URL` 是容器内覆盖地址；宿主机进程分别使用 `TRICYCLE_DATABASE_URL` 和 `TRICYCLE_RUSTFS_ENDPOINT_URL`。",
            "- `TRICYCLE_UPLOAD_WORKER_STATEMENT_TIMEOUT_MS`、`TRICYCLE_PROFILE_REFRESH_STATEMENT_TIMEOUT_MS` 和 `TRICYCLE_UNITS_DATASET_WORKER_STATEMENT_TIMEOUT_MS` 由 Compose 映射到各 worker 的 `TRICYCLE_QUERY_STATEMENT_TIMEOUT_MS`。",
            "- `TRICYCLE_IMPORT_*` 和 `TRICYCLE_RECONCILE_*` 为本地命令的默认值；同名命令行选项优先。",
            "- `.env.example` 中的密码和密钥是开发示例。生产环境需按 [部署配置](deployment-configuration.md) 设置真实凭据。",
            "",
        ]
    )
    content = "\n".join(lines)
    OUTPUT.write_text(content)
    translations = {
        "# 环境变量参考": "# Environment Variable Reference",
        "本页列出项目直接读取的应用配置、RustFS 和存储 GC 配置、Compose 变量、前端构建变量及脚本开关。": "This page lists application, RustFS, storage GC, Compose, frontend build, and script environment variables read by the project.",
        "Python 配置读取进程环境和当前工作目录的 `.env`；Compose 使用 `.env` 插值并将其传给 API/worker。": "Python settings read the process environment and the current working directory's `.env`. Compose interpolates `.env` and passes it to API and workers.",
        "调整后端变量后重启相应服务；`VITE_*` 是构建时变量，调整后重新构建前端。": "Restart affected backend services after changing variables. Rebuild the frontend after changing build-time `VITE_*` variables.",
        "生产环境必填项及组合校验见 [部署配置](deployment-configuration.md)。": "For production requirements and cross-field validation, see [deployment configuration](deployment-configuration.md).",
        "配置定义变化后运行 `make env-docs` 同步中英文索引。": "Run `make env-docs` after configuration changes to refresh both indexes.",
        "## 应用配置（按功能拆分）": "## Application settings (by function)",
        "变量名由 `TRICYCLE_` 加字段名大写组成。以下默认值来自代码，不读取本机 `.env`。数值边界为配置校验边界。": "Names use `TRICYCLE_` plus the uppercase field name. Defaults come from code, not the local `.env`. Bounds are enforced by settings validation.",
        "| 变量 | 代码默认值 | 数值或格式约束 |": "| Variable | Code default | Value constraints |",
        "## RustFS 配置": "## RustFS settings",
        "## 存储垃圾回收配置": "## Storage garbage collection settings",
        "## Compose、前端与脚本变量": "## Compose, frontend, and script variables",
        "下表列出不属于上述 Python 设置类、但由项目 Compose、前端或脚本读取的变量。默认值以 `.env.example` 为准；“未列出”表示按使用位置决定或需要显式提供。": "These variables are read by Compose, the frontend, or scripts outside the Python settings classes. Values come from `.env.example`; an unset entry is determined at its use site or must be supplied explicitly.",
        "| 变量 | `.env.example` 示例值 | 来源 |": "| Variable | `.env.example` example | Source |",
        "## 配置关系": "## Configuration relationships",
        "- `TRICYCLE_MCP_TS_GEOMETRY_EXPORT_MAX_BYTES` 只控制 TS 几何 MCP 导出；其他 MCP JSONL/CSV 使用 `TRICYCLE_MCP_EXPORT_MAX_BYTES`。字节上限按序列化记录累计，响应封装本身另有少量开销。": "- `TRICYCLE_MCP_TS_GEOMETRY_EXPORT_MAX_BYTES` controls only the TS geometry MCP export; other MCP JSONL/CSV exports use `TRICYCLE_MCP_EXPORT_MAX_BYTES`. The budget sums serialized records and excludes small response-wrapper overhead.",
        "- `TRICYCLE_UNITS_TS_MAX_REACTIVE_ATOMS`、`TRICYCLE_UNITS_TS_MAX_RC_BONDS`、`TRICYCLE_UNITS_TS_MAX_RC_ANGLES` 会改变 UniTS 导出数组形状。变更前应确认消费端支持新形状，并保持同一次导出的 worker 配置一致。": "- `TRICYCLE_UNITS_TS_MAX_REACTIVE_ATOMS`, `TRICYCLE_UNITS_TS_MAX_RC_BONDS`, and `TRICYCLE_UNITS_TS_MAX_RC_ANGLES` change UniTS export array shapes. Check consumer support and keep worker settings consistent within an export.",
        "- `VITE_DEFAULT_REACTION_PAGE_SIZE` 和 `VITE_DEFAULT_CATALOG_PAGE_SIZE` 会在前端按各自最大页大小截断；`VITE_PAGE_PREFETCH_RADIUS` 可以设为 `0` 关闭邻页预取。无效的前端整数值回退到代码默认值。": "- Frontend defaults are clamped to their respective maximum page sizes. Set `VITE_PAGE_PREFETCH_RADIUS=0` to disable adjacent-page prefetch. Invalid frontend integers fall back to code defaults.",
        "- `TRICYCLE_COMPOSE_DATABASE_URL` 和 `TRICYCLE_COMPOSE_RUSTFS_ENDPOINT_URL` 是容器内覆盖地址；宿主机进程分别使用 `TRICYCLE_DATABASE_URL` 和 `TRICYCLE_RUSTFS_ENDPOINT_URL`。": "- `TRICYCLE_COMPOSE_DATABASE_URL` and `TRICYCLE_COMPOSE_RUSTFS_ENDPOINT_URL` override container endpoints; host processes use `TRICYCLE_DATABASE_URL` and `TRICYCLE_RUSTFS_ENDPOINT_URL`.",
        "- `TRICYCLE_UPLOAD_WORKER_STATEMENT_TIMEOUT_MS`、`TRICYCLE_PROFILE_REFRESH_STATEMENT_TIMEOUT_MS` 和 `TRICYCLE_UNITS_DATASET_WORKER_STATEMENT_TIMEOUT_MS` 由 Compose 映射到各 worker 的 `TRICYCLE_QUERY_STATEMENT_TIMEOUT_MS`。": "- Compose maps the upload, profile-refresh, and dataset-worker statement timeout variables to each worker's `TRICYCLE_QUERY_STATEMENT_TIMEOUT_MS`.",
        "- `TRICYCLE_IMPORT_*` 和 `TRICYCLE_RECONCILE_*` 为本地命令的默认值；同名命令行选项优先。": "- `TRICYCLE_IMPORT_*` and `TRICYCLE_RECONCILE_*` set local-command defaults; corresponding CLI options take precedence.",
        "- `.env.example` 中的密码和密钥是开发示例。生产环境需按 [部署配置](deployment-configuration.md) 设置真实凭据。": "- Passwords and keys in `.env.example` are development examples. Set production credentials according to [deployment configuration](deployment-configuration.md).",
        "未设置": "unset",
        "（空）": "(empty)",
        "脚本/进程": "script/process",
    }
    english = content
    for original, translated in translations.items():
        english = english.replace(original, translated)
    (ROOT / "docs/en/environment-variables.md").write_text(english)

    from scripts.split_environment_reference import split

    for language in ("zh", "en"):
        split(language)


if __name__ == "__main__":
    main()
