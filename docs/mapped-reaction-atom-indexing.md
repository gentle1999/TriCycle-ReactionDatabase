# 映射反应导出的原子索引约定

映射反应相关导出中的原子顺序由 atom-map number 定义，而不是由源 Geometry、计算文件或 SMILES 的遍历顺序定义。映射号 `n` 对应所有逐原子数组的零基索引 `n - 1`。

## 适用范围

本约定适用于映射反应 TS Geometry JSONL、UniTS TS JSONL 与 NPY 导出。对应实现集中在 `mapped_geometry_atom_order.py`、`mapped_calculation_order.py`、`mapped_reaction_geometry_export.py` 和 `units_ts_dataset_export.py`。

Geometry 绑定必须将反应中的原子映射完整地对应到 Geometry 原子；映射号须唯一且连续为 `1..N`，并且每个映射号在反应两侧及 Geometry 中对应相同元素与同位素。无法验证的记录应跳过或报错，不能用猜测的排列继续导出。

## 顺序与数据范围

- `atoms`、RDKit Mol block 和 UniTS 的 `atom_symbols`、原子序数、质量、节点特征及坐标均按 map number 升序排列；map `n` 位于下标 `n - 1`。
- 图边端点、反应中心原子、反应中心键/角、分子 fragment 索引都引用该同一原子顺序。边特征按其对应边记录排列。
- Calculation frame 的源坐标顺序先通过 frame→Geometry 对应关系，再通过 Geometry→atom-map 关系投影到映射反应顺序。导出的坐标仍处于源笛卡尔参考系，不因重排而旋转或平移。
- 逐原子 scientific arrays 必须按其类型声明的 atom axis 重排；不能仅凭数组形状猜轴。Hessian 的两个 `3N` 笛卡尔轴都按原子块重排，normal modes、力、原子布居、键级矩阵和 Fukui/分数占据数据使用各自声明的轴。
- NMR coupling 子集的矩阵行列与其原子索引同步排序，并导出零基 `atom_indices` 及一基 `atom_map_numbers`。逐原子 NMR shielding/principal-value 记录同时提供零基 `atom_index` 与一基 `atom_map_number`。
- 没有原子索引轴的分子向量、频率等数据保留自身分量顺序。数据库源数组不得被原地修改。

新增逐原子字段或 scientific array kind 时，必须明确其 atom axis 和变换方式，并增加置换非恒等的测试，验证数值、索引元数据和映射号仍彼此一致。不要把未经分类的新数组静默当作原子顺序数据。

## 唯一索引与样本身份

`(mapped_reaction_smiles, atom_map_number)` 可作为映射反应内的原子身份键。对于只保留零基数组索引的字段，可按 `array[n - 1]` 读取映射号为 `n` 的原子；显式逐原子记录应使用其 `atom_map_number` 字段进行连接。

映射反应 SMILES 是反应键，不是 TS 样本键。同一映射反应可以有多个 Geometry binding，因此 JSONL 的 `key` 会重复。区分几何样本时使用 `(key, geometry_binding_id)`；区分某个计算帧中的数据时再加入 `frame_id`。UniTS 样本也带有 `mapped_reaction_id`、`geometry_binding_id` 和 `geometry_id`。

按 Geometry ID 下载的通用 SDF/XYZ，以及没有选择映射反应的计算帧 TS-anchor 导出，没有唯一的映射反应顺序，仍使用 Geometry 或源计算帧顺序。需要直接连接 atom map 时，应使用上述映射反应导出。

详细字段与格式见 [UniTS TS Geometry 导出说明](units-ts-dataset-export.md)；实现或维护导出时另见 [mapped-reaction-exports skill](../.agents/skills/mapped-reaction-exports/SKILL.md)。
