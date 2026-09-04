"""Generate the complete physical database ERD from SQLAlchemy metadata."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.sql.schema import Column, ForeignKeyConstraint, Table
from sqlalchemy.sql.sqltypes import Enum, Uuid

from tricycle_reaction_db.db.models import metadata

SCHEMA_REVISION = "0028_restore_mapped_text_id"
OUTPUT_PATH = Path(__file__).parents[1] / "docs" / "database-erd.md"

POSTGRESQL_GROUPS = {
    "身份、组织与项目授权": (
        "user_account",
        "auth_session",
        "mcp_access_token",
        "external_identity",
        "organization",
        "organization_membership",
        "project",
        "project_membership",
        "project_invitation",
        "audit_event",
    ),
    "Artifact、解析与计算帧": (
        "artifact_file",
        "artifact_ingestion",
        "upload_batch",
        "upload_batch_item",
        "calculation_protocol",
        "parse_revision",
        "calculation_segment",
        "calculation_frame",
    ),
    "RustFS 增量垃圾回收": (
        "storage_garbage_collection_state",
        "storage_garbage_collection_run",
    ),
    "化学身份与几何": (
        "molecular_formula",
        "molecular_topology",
        "molecular_topology_abstraction",
        "molecular_topology_derivation",
        "geometry",
        "project_geometry_catalog",
        "project_geometry_catalog_count",
    ),
    "逐帧科学结果": (
        "frame_energy_result",
        "energy_observation",
        "geometry_optimization_result",
        "vibration_result",
        "calculation_status_result",
        "scientific_array",
        "thermochemistry_result",
        "molecular_orbital_result",
        "charge_spin_population_result",
        "atomic_population_series",
        "polarizability_result",
        "nmr_result",
        "nmr_shielding_tensor",
        "bond_order_result",
        "total_spin_result",
        "single_point_property_result",
        "electronic_state_set",
        "electronic_state",
        "electronic_configuration",
        "multireference_result",
        "implicit_solvation_result",
        "scientific_array_assignment",
    ),
    "Manifest 与反应语义": (
        "workflow_manifest",
        "manifest_artifact_binding",
        "logical_reaction",
        "logical_reaction_participant",
        "logical_participant_concrete_topology",
        "mapped_reaction",
        "mapped_reaction_thermodynamic_profile",
        "mapped_reaction_participant",
        "mapped_reaction_node",
        "mapped_reaction_node_geometry",
        "mapped_reaction_node_geometry_mapping",
        "mapped_reaction_edge",
        "transition_state_inference",
        "transition_state_endpoint",
    ),
}

SPECIAL_NODE_LABELS = {
    "artifact_file": "artifact_file<br/>RustFS pointer + visibility",
    "storage_garbage_collection_state": (
        "storage_garbage_collection_state<br/>PostgreSQL watermark"
    ),
    "storage_garbage_collection_run": "storage_garbage_collection_run<br/>audit",
    "external_identity": "external_identity<br/>OIDC issuer + subject",
    "molecular_topology": "molecular_topology<br/>RDKit mol",
    "molecular_topology_abstraction": "molecular_topology_abstraction<br/>stereo DAG edge",
    "logical_participant_concrete_topology": (
        "logical_participant_concrete_topology<br/>logical → concrete"
    ),
    "geometry": "geometry<br/>RDKit mol + NPY BYTEA",
    "scientific_array": "scientific_array<br/>NPY BYTEA",
}


def _assert_complete_grouping() -> None:
    grouped = [table for tables in POSTGRESQL_GROUPS.values() for table in tables]
    actual = set(metadata.tables)
    if len(grouped) != len(set(grouped)):
        raise RuntimeError("PostgreSQL ERD groups contain a duplicate table")
    missing = actual - set(grouped)
    unknown = set(grouped) - actual
    if missing or unknown:
        raise RuntimeError(f"ERD table grouping drifted: missing={missing}, unknown={unknown}")


def _mermaid_type(column: Column[object]) -> str:
    column_type = column.type
    class_name = type(column_type).__name__.lower()
    if class_name == "rdkitmol":
        return "mol"
    if class_name == "numpyarray":
        return "bytea"
    if class_name == "autostring":
        return "string"
    if isinstance(column_type, Uuid):
        return "uuid"
    if isinstance(column_type, JSONB):
        return "jsonb"
    if isinstance(column_type, ARRAY):
        return "array"
    if isinstance(column_type, LargeBinary):
        return "bytea"
    if isinstance(column_type, Boolean):
        return "boolean"
    if isinstance(column_type, DateTime):
        return "datetime"
    if isinstance(column_type, Float):
        return "float"
    if isinstance(column_type, Numeric):
        return f"numeric({column_type.precision},{column_type.scale})"
    if isinstance(column_type, BigInteger):
        return "bigint"
    if isinstance(column_type, SmallInteger):
        return "smallint"
    if isinstance(column_type, Integer):
        return "integer"
    if isinstance(column_type, Text):
        return "text"
    if isinstance(column_type, Enum):
        return "enum"
    if isinstance(column_type, String):
        return "string"
    return class_name


def _single_column_unique_names(table: Table) -> set[str]:
    unique_names = {
        next(iter(constraint.columns)).name
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint) and len(constraint.columns) == 1
    }
    unique_names.update(
        next(iter(index.columns)).name
        for index in table.indexes
        if index.unique and len(index.columns) == 1
    )
    return unique_names


def _unique_column_sets(table: Table) -> set[frozenset[str]]:
    unique_sets = {
        frozenset(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    unique_sets.update(
        frozenset(column.name for column in index.columns)
        for index in table.indexes
        if index.unique
    )
    unique_sets.add(frozenset(column.name for column in table.primary_key.columns))
    return unique_sets


def _column_line(table: Table, column: Column[object]) -> str:
    markers: list[str] = []
    if column.primary_key:
        markers.append("PK")
    if column.foreign_keys:
        markers.append("FK")
    if column.name in _single_column_unique_names(table):
        markers.append("UK")
    marker_text = f" {', '.join(markers)}" if markers else ""

    comments: list[str] = []
    if column.nullable:
        comments.append("nullable")
    if table.name == "artifact_file" and column.name in {"bucket", "object_key", "version_id"}:
        comments.append("RustFS locator")
    if _mermaid_type(column) == "mol":
        comments.append("PostgreSQL RDKit cartridge")
    if type(column.type).__name__.lower() == "numpyarray":
        comments.append("NPY encoded BYTEA")
    comment_text = f' "{"; ".join(comments)}"' if comments else ""
    return f"        {_mermaid_type(column)} {column.name}{marker_text}{comment_text}"


def _is_to_one(constraint: ForeignKeyConstraint) -> bool:
    local_names = frozenset(element.parent.name for element in constraint.elements)
    return local_names in _unique_column_sets(constraint.table)


def _relationship_line(constraint: ForeignKeyConstraint) -> str:
    elements = list(constraint.elements)
    parent_table = elements[0].column.table.name
    child_table = constraint.table.name
    local_columns = [element.parent.name for element in elements]
    required_parent = all(not element.parent.nullable for element in elements)
    parent_cardinality = "||" if required_parent else "o|"
    child_cardinality = "o|" if _is_to_one(constraint) else "o{"
    label = "__".join(local_columns)
    return f"    {parent_table} {parent_cardinality}--{child_cardinality} {child_table} : {label}"


def _storage_boundary_diagram() -> list[str]:
    lines = [
        "```mermaid",
        "flowchart TB",
        '    subgraph PERSISTENT["持久化后端"]',
        "        direction LR",
        '        subgraph RUSTFS["RustFS / S3-compatible object storage"]',
        (
            '            rustfs_object["原始 artifact object bytes<br/>'
            'Gaussian / ORCA / input / manifest"]'
        ),
        "        end",
        '        subgraph POSTGRES["PostgreSQL 18 + RDKit cartridge"]',
        "            direction TB",
    ]
    table_ids: list[str] = []
    for group_index, (group_name, table_names) in enumerate(POSTGRESQL_GROUPS.items(), start=1):
        group_id = f"PG_GROUP_{group_index}"
        lines.append(f'            subgraph {group_id}["{group_name}"]')
        for table_name in table_names:
            label = SPECIAL_NODE_LABELS.get(table_name, table_name)
            lines.append(f'                {table_name}["{label}"]')
            table_ids.append(table_name)
        lines.append("            end")
    lines.extend(
        [
            "        end",
            "    end",
            '    subgraph MEMORY["非持久化处理层 / process memory"]',
            '        molop_models["MolOP Pydantic models / model_dump payload"]',
            '        runtime_objects["RDKit Chem.Mol + NumPy ndarray"]',
            "    end",
            '    rustfs_object -. "bucket + object_key + version_id" .-> artifact_file',
            '    molop_models -->|"projection / normalization"| parse_revision',
            '    molop_models -->|"frame facts"| calculation_frame',
            (
                '    molop_models -->|"TS frame + imaginary mode endpoints"| '
                "transition_state_inference"
            ),
            '    runtime_objects -->|"RDKit mol"| molecular_topology',
            '    runtime_objects -->|"RDKit mol + NPY"| geometry',
            '    runtime_objects -->|"NPY"| scientific_array',
            "    classDef rustfs fill:#fff4d6,stroke:#9a6700,color:#1f2328",
            "    classDef postgres fill:#eaf2ff,stroke:#0969da,color:#1f2328",
            "    classDef memory fill:#f1f3f5,stroke:#57606a,color:#1f2328,stroke-dasharray: 5 5",
            "    class rustfs_object rustfs",
            f"    class {','.join(table_ids)} postgres",
            "    class molop_models,runtime_objects memory",
            "```",
        ]
    )
    return lines


def _complete_erd() -> list[str]:
    constraints = sorted(
        (
            constraint
            for table in metadata.sorted_tables
            for constraint in table.foreign_key_constraints
        ),
        key=lambda constraint: (
            constraint.table.name,
            tuple(element.parent.name for element in constraint.elements),
        ),
    )
    lines = [
        "```mermaid",
        "%% Generated from SQLAlchemy metadata. Do not hand-edit this block.",
        "erDiagram",
    ]
    lines.extend(_relationship_line(constraint) for constraint in constraints)
    lines.append("")
    for table in metadata.sorted_tables:
        lines.append(f"    {table.name} {{")
        lines.extend(_column_line(table, column) for column in table.columns)
        lines.append("    }")
    lines.append("```")
    return lines


def _constraint_inventory() -> list[str]:
    lines = [
        "| table | columns | FK constraints | UNIQUE constraints | CHECK constraints | indexes |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for table in metadata.sorted_tables:
        foreign_keys = len(table.foreign_key_constraints)
        unique_constraints = sum(
            isinstance(constraint, UniqueConstraint) for constraint in table.constraints
        )
        check_constraints = sum(
            isinstance(constraint, CheckConstraint) for constraint in table.constraints
        )
        indexes = sum(isinstance(index, Index) for index in table.indexes)
        lines.append(
            f"| `{table.name}` | {len(table.columns)} | {foreign_keys} | "
            f"{unique_constraints} | {check_constraints} | {indexes} |"
        )
    return lines


def _totals() -> dict[str, int]:
    tables = list(metadata.tables.values())
    return {
        "tables": len(tables),
        "columns": sum(len(table.columns) for table in tables),
        "foreign_keys": sum(len(table.foreign_key_constraints) for table in tables),
        "unique_constraints": sum(
            isinstance(constraint, UniqueConstraint)
            for table in tables
            for constraint in table.constraints
        ),
        "check_constraints": sum(
            isinstance(constraint, CheckConstraint)
            for table in tables
            for constraint in table.constraints
        ),
        "indexes": sum(len(table.indexes) for table in tables),
    }


def _document() -> str:
    totals = _totals()
    lines = [
        "# 数据库实体关系图",
        "",
        f"> 当前 schema：Alembic `{SCHEMA_REVISION}`",
        "> 生成来源：`tricycle_reaction_db.db.models.metadata`",
        f"> 完整性：{totals['tables']} 张表、{totals['columns']} 个列、",
        f"> {totals['foreign_keys']} 条外键约束，未省略物理表、列或 FK。",
        "",
        "本文区分物理持久化后端和进程内对象。RustFS 与 PostgreSQL 不共享事务；",
        "`artifact_file` 只保存 RustFS locator、内容 hash 和状态，原始逻辑字节不进入",
        "PostgreSQL。除原始 artifact object 外，所有领域实体和科学结果都存放在",
        "PostgreSQL；RDKit cartridge、ARRAY、JSONB 和 BYTEA 是 PostgreSQL 内部列类型，",
        "不是独立数据库后端。RustFS 磁盘层透明压缩可压缩对象，但 S3 GET/HEAD、",
        "Artifact SHA-256 和大小仍以原始逻辑字节为准。新上传对象按 UTC 小时分区，",
        "上传失败由生命周期 Hook 定点补偿；可选 GC 的水位和运行审计存放 PostgreSQL；",
        "对象是否保留以 ArtifactFile 关系为准。",
        "",
        "## 物理存储边界",
        "",
        *_storage_boundary_diagram(),
        "",
        "| 数据形态 | 持久化后端 | 权威内容 |",
        "| --- | --- | --- |",
        (
            "| 原始 Gaussian/ORCA/input/manifest bytes | RustFS | "
            "object bytes 和 object-store version/ETag |"
        ),
        (
            "| Artifact 索引、解析、化学、反应与结果实体 | PostgreSQL | "
            f"{totals['tables']} 张关系表及其约束 |"
        ),
        (
            "| 用户、外部身份、组织、项目与成员关系 | PostgreSQL | "
            "本地授权主体、OIDC 映射和角色权限边界 |"
        ),
        (
            "| `molecular_topology.mol`、`geometry.mol` | PostgreSQL + RDKit cartridge | "
            "分子图与带坐标 mol |"
        ),
        (
            "| `geometry.internal_coordinates`、`scientific_array.data` | PostgreSQL `BYTEA` | "
            "`allow_pickle=False` 的 NPY bytes |"
        ),
        "| 向量/枚举序列 | PostgreSQL `ARRAY` | 原子序、shape、occupancy、mode index 等 |",
        (
            "| provenance、diagnostics、metadata | PostgreSQL `JSONB` | "
            "结构化但不参与数值矩阵存储的事实 |"
        ),
        (
            "| MolOP models、临时 `Chem.Mol`、临时 `ndarray` | 不持久化；进程内 | "
            "解析和入库过程的临时对象 |"
        ),
        "",
        "## 全量物理 ERD",
        "",
        (
            f"下图逐列展开全部 {totals['tables']} 张 PostgreSQL 表，并为 "
            f"{totals['foreign_keys']} 条外键约束各生成一条关系线。"
        ),
        "关系标签是子表 FK 列名；复合 FK 使用 `__` 连接列名。`nullable` 表示列允许",
        "SQL `NULL`。单列唯一键标为 `UK`；复合 UNIQUE、CHECK 和 index 在后续清单中",
        "逐表计数，并以 SQLModel/Alembic 定义为权威。",
        "",
        *_complete_erd(),
        "",
        "## Schema 完整性清单",
        "",
        f"- `{totals['tables']}` tables；",
        f"- `{totals['columns']}` columns；",
        f"- `{totals['foreign_keys']}` FK；",
        f"- `{totals['unique_constraints']}` UNIQUE；",
        f"- `{totals['check_constraints']}` CHECK；",
        f"- `{totals['indexes']}` indexes。",
        "",
        *_constraint_inventory(),
        "",
        "## 关键跨后端约束",
        "",
        "- `artifact_file.bucket/object_key/version_id` 定位 RustFS object；",
        "  `content_sha256` 才是跨后端内容身份，S3 ETag 不替代 SHA-256。",
        "- `artifact_file.project_id/created_by_user_id/visibility` 存在 PostgreSQL；",
        "  `public` 允许匿名列表、预览和下载，`project` 要求有效项目成员权限。",
        "- `external_identity` 只保存外部 OIDC 的 issuer、subject、claims 与本地用户映射；",
        "  本系统不保存密码，用户、组织和项目成员关系均以 PostgreSQL 为权威。",
        "- RustFS object 的上传与 PostgreSQL transaction 不原子提交；",
        "  上传先提交 pending，再校验对象并更新 available；`storage_status` 和",
        "  `storage_verified_at` 显式记录一致性状态。失败出口 Hook 在 identity lock 内定点",
        "  删除未变成 available 的本次对象。",
        "- `storage_garbage_collection_state.watermark_at` 是每个 bucket/prefix 的上次成功",
        "  扫描水位；`storage_garbage_collection_run` 保存窗口、计数和错误。可选 GC 只列举",
        "  `uploads/YYYY/MM/DD/HH/` 新分区，宽限期内对象留给下一次运行，失败不推进水位。",
        "- `molecular_topology.mol` 和 `geometry.mol` 都在 PostgreSQL RDKit cartridge；",
        "  前者不含 conformer，后者按 Topology atom order 保存一个规范 3D conformer。",
        "- `geometry.internal_coordinates` 是 E(3)-不变几何身份权威值；RDKit conformer 用于",
        "  结构查询和展示，并允许 cartridge round-trip 的约 `1e-6 angstrom` 精度差。",
        "- 数值向量和矩阵不进入 JSONB，也不进入 RustFS。`scientific_array.data` 使用 NPY",
        "  `BYTEA`，`scientific_array_assignment` 以 owner FK 和 slot 保存 MolOP 字段语义。",
        "- LogicalReaction 身份不依赖 manifest、日志、Geometry 或 CalculationFrame；",
        "  反应轴通过 topology/geometry/frame 外键连接物理计算事实。",
        "",
        "## 更新方式",
        "",
        "模型或 migration 变化后运行：",
        "",
        "```bash",
        "uv run python scripts/generate_database_erd.py",
        "uv run alembic check",
        "```",
        "",
        "生成器会校验 PostgreSQL 分组与 metadata 表集合完全一致；新增、删除或重命名表后",
        "若未同步存储边界分组，会直接失败，不会静默生成不完整 ERD。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    _assert_complete_grouping()
    OUTPUT_PATH.write_text(_document(), encoding="utf-8")


if __name__ == "__main__":
    main()
