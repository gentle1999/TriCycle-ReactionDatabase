# RDKit Mol 对象数据库往返契约

> English edition: [RDKit Mol database round-trip contract](en/rdkit-mol-roundtrip.md).
>
> 状态：当前持久化契约。

## 结论

PostgreSQL RDKit cartridge 用于持久化和恢复结构查询/展示所需的二进制 `Chem.Mol`。它会在
cartridge 精度边界内保留 atom order 与 conformer，但不是 Geometry 身份的保真来源。Geometry
匹配以无损内部坐标为准，CalculationFrame 的源 Cartesian 坐标和 source-to-Geometry
permutation 是振动、梯度与 Hessian 的唯一坐标参照。

## 可信 MolGR 图的边界

对 MolGR 产生的可信分子图，归一化只清理临时属性并建立 RDKit ring info。它不会：

- 调用 `SanitizeMol`；
- 推断或补充隐式氢；
- 因不完整价态额外写入自由基电子；
- canonicalize/reorder 原子，或改变 source atom correspondence。

`canonical_isomeric_smiles` 是 API 兼容字段名，其实际值是显式氢 isomeric SMILES，不是
骨架 SMILES。该字符串、形式电荷和自由基电子数必须与 MolGR 图实际标记一致。无法生成
SMILES 的图仍可通过 binary Mol 和 `graph_hash` 入库；不得因展示字符串不可用而丢弃计算帧。

## Geometry 与 Frame

Geometry 的唯一身份包括 topology、坐标规范版本/哈希、总电荷和总自旋多重度。因此坐标相同但
电子状态不同的观测必须生成不同 Geometry。Frame 保存源坐标、原子 permutation、刚体变换和
打印精度；下游数组一律按 Frame 源原子顺序解释。XYZ 下载 comment 行必须输出 Geometry 的
charge/multiplicity。

## 查询与测试规则

- 使用 RDKit GiST 与 fingerprint 索引执行结构谓词；普通查询不得逐行从文本重建 Mol/fingerprint。
- cartridge 的坐标精度与 property 丢失行为是经过测试的传输边界，不是改变科学图的理由。
- 更新 RDKit、PostgreSQL、MolAlchemy 或数据库 driver 后必须运行 `make test-db`。

`make test-db` 覆盖 binary Mol 往返、conformer 精度、property 行为、子结构查询、索引和
ready endpoint。数据模型的完整边界见[数据模型与存储边界](data-model.md)。
