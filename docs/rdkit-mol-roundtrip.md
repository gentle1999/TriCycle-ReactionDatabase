# RDKit Mol 对象数据库往返契约

> 状态：已验证  
> 验证日期：2026-07-12  
> 适用基线：MolAlchemy 0.0.7、Python RDKit 2025.09.6、PostgreSQL RDKit
> cartridge 4.8.0 / toolkit 2025.09.6

## 结论

Python `rdkit.Chem.Mol` 可以通过 MolAlchemy 写入 PostgreSQL `mol` 并重新读取为
`Chem.Mol`。当前链路足以无损传递本项目需要的化学图主信息，但不能定义为完整
Python 对象或 Gaussian 几何的无损归档。

当前 Python 与 PostgreSQL 两端的 `rdkit.__version__` / `rdkit_toolkit_version()`
均为 `2025.09.6`。集成测试要求两端版本严格一致；只升级一端不属于受支持配置。

`MolecularTopology.mol` 是权威化学图 ORM 属性，其职责固定为：

- 保存可检索的化学图、结构身份和立体化学；入库前移除 conformer、atom map 和临时属性。
- 支持 RDKit cartridge 的精确、子结构和相似度查询。
- 原子顺序不参与 topology identity；`graph_hash` 由规范化化学图生成。

`Geometry.mol` 按 Topology atom order 保存，并包含且只包含一个由 InternalCoords
重建的规范 3D conformer。几何身份和精确匹配使用独立的
`internal_coordinates` NPY `BYTEA`；
上传文件的 source Cartesian 坐标、source→Geometry permutation 和方向变换只保存在
CalculationFrame。Gaussian 坐标和量化矩阵仍使用独立 ScientificArray 的 NumPy
`BYTEA` 映射；工作流元数据和任意 RDKit property 使用明确字段或小型版本化 JSONB
保存。

## 实际传输链路

MolAlchemy 0.0.7 的 `RdkitMol(return_type="mol")` 使用以下链路：

```text
Chem.Mol.ToBinary()
  -> PostgreSQL mol_from_pkl()
  -> PostgreSQL mol
  -> PostgreSQL mol_send()
  -> Chem.Mol(bytes)
```

`ToBinary()` 使用 RDKit 默认 pickle flags。默认不包含自定义 properties，坐标按
单精度写入。cartridge 读取后也会按自己的固定 flags 重新序列化，因此即使调用方
自行请求双精度 pickle，也不能将 `mol` 列视为双精度几何存储。

实现依据：

- [MolAlchemy 0.0.7 `RdkitMol`](https://github.com/asiomchen/molalchemy/blob/v0.0.7/src/molalchemy/rdkit/types.py)
- [RDKit 2025.09.6 `MolPickler` flags](https://github.com/rdkit/rdkit/blob/Release_2025_09_6/Code/GraphMol/MolPickler.h)
- [RDKit PostgreSQL adapter serialization](https://github.com/rdkit/rdkit/blob/Release_2025_09_6/Code/PgSQL/rdkit/adapter.cpp)
- [RDKit PostgreSQL `mol_from_pkl` / `mol_send`](https://github.com/rdkit/rdkit/blob/Release_2025_09_6/Code/PgSQL/rdkit/rdkit_io.c)

## 已验证边界

| 信息 | 结果 | 项目契约 |
| --- | --- | --- |
| 原子顺序、元素、连通性和键级 | 保留 | Geometry 必须保持；Topology identity 与顺序无关 |
| 芳香性和环信息 | 保留 | 必须保持 |
| 四面体手性与双键 E/Z | 保留 | 重新计算 CIP 后必须一致 |
| enhanced stereo groups | 保留 | 必须保持 |
| 同位素和显式氢 | 保留 | 必须保持 |
| 形式电荷和自由基电子数 | 保留 | 必须保持 |
| atom map number | 保留 | 传输能力已验证；业务 mol 入库前移除，映射存反应专用表 |
| dative / coordinate bond | 保留 | 必须保持 |
| canonical isomeric mapped CXSMILES | 保留 | 必须保持 |
| conformer 数量、ID、`Is3D()` | 当前保留 | Topology 禁止；Geometry 必须且只能有一个 3D conformer |
| conformer 坐标 | 约 `10^-7 Å` 量化误差 | Geometry.mol 只用于规范展示；`internal_coordinates` 是几何身份权威值 |
| mol/atom/bond/conformer 自定义 property | 默认链路丢失 | 禁止依赖 |
| `_CIPCode` 等 computed property/cache | 不保证保留 | 从化学图重新计算 |

真实数据库测试覆盖 R/S、E/Z、同位素、离子、自由基、芳香环、显式氢、atom map、
配位键、enhanced stereo groups 和多个 3D conformer。测试以原子/键逐项签名、
非 canonical 与 canonical CXSMILES、重算 CIP/E/Z 和坐标容差共同判断，不以 Python
对象 identity 或 pickle bytes 判断。

## ORM 与 API 边界

SQLModel table entity 可以直接使用 `Chem.Mol`：

```python
from pydantic import ConfigDict
from rdkit import Chem
from sqlmodel import Field, SQLModel

from molalchemy.rdkit.types import RdkitMol


class MolecularTopology(SQLModel, table=True):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: int | None = Field(default=None, primary_key=True)
    mol: Chem.Mol = Field(sa_type=RdkitMol(return_type="mol"), nullable=False)
```

`mol` 往返能够保留 atom map 和 conformer，不代表它们应进入 Topology。reaction
atom map 属于反应/节点上下文；Topology.mol 是无构象图，Geometry.mol 是规范顺序的
单构象访问对象，Geometry.internal_coordinates 是几何身份权威值，
CalculationFrame.observed_coordinates 是源坐标证据。

`arbitrary_types_allowed` 只解决 Pydantic 对 `Chem.Mol` 的运行时接纳，不提供 JSON
序列化。REST、GraphQL 和 MCP DTO 应显式输出 canonical mapped SMILES、MolBlock 或
项目定义的结构 DTO，不直接暴露 table entity。

SMARTS/query molecule 不属于普通结构列契约。需要保存查询模板时使用 PostgreSQL
RDKit 的 `qmol`/`xqmol` 类型，并建立独立模型和查询测试。SGroups 当前不属于环加成
路径数据库的业务契约；未来若引入，必须先增加真实数据库往返样本。

## 验证命令

```bash
docker compose up -d --wait postgres
uv run alembic upgrade head
make test-db
```

测试入口是 `tests/integration/test_rdkit_mol_object_roundtrip.py`。升级 Python RDKit、
MolAlchemy、cartridge 镜像或 PostgreSQL major version 时，必须重新运行该测试并复核
此文档中的边界。
