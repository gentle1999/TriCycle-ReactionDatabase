# 数据模型与存储边界

> 当前实现契约。英文版见 [English](en/data-model.md)。带日期的设计和验收记录在
> `refactor-plan.md`、`technical-roadmap.md` 等文件中；它们是历史证据，不应代替本文。

## 领域轴

```text
原始事实
RustFS object -> ArtifactFile -> ParseRevision -> CalculationSegment -> CalculationFrame
                                      |                                  |
                                      `-> ArtifactIngestion               `-> Geometry

化学身份
MolecularFormula -> MolecularTopology -> Geometry
                         |
                         `-> MolecularTopologyDerivation

反应路径
TransitionStateInference -> LogicalReaction -> MappedReaction -> Node -> NodeGeometry -> Geometry
```

`ArtifactFile`、`ParseRevision` 和 `CalculationFrame` 是文件及解析溯源，不是化学或反应
身份。`MolecularFormula -> MolecularTopology -> Geometry` 是可复用的化学事实；二者在
CalculationFrame 对 Geometry 的绑定处相遇。组织、项目、成员和外部身份独立组成访问控制轴。

## Formula、Topology 和 Geometry

`MolecularFormula` 仅描述元素与同位素组成。权威范围检索数据是按原子序数排列的
`element_count_vector`；常见精确组成还投影为 GIN 索引的 `element_count_tokens`。电荷、
自由基、键和立体化学属于 topology，不属于 formula。

`MolecularTopology` 保存 MolGR 重建出的分子图、形式电荷、自由基电子数、片段数、立体
状态和图哈希。对 MolGR 产出的可信图，数据库只清理临时属性、初始化 RDKit ring info，并
保留 MolGR 给出的原子顺序和电子标记；**不**再执行 `SanitizeMol`、补氢、自由基推断或
规范原子重排。这样不会改变量化计算坐标的原子对应关系。`suspicious_fallback` 仍会入库并
带有相应 provenance，但不能作为 TS 前后体端点。

`canonical_isomeric_smiles` 是为 API 兼容保留的字段名，实际存储的是 MolGR 图的显式氢
isomeric SMILES；它不是骨架 SMILES，也不是另一个隐式氢表示。无法生成 SMILES 的图仍可
以二进制 Mol 与 `graph_hash` 保存、展示并参加适用的结构检索。`graph_hash` 的输入是版本化
图序列化，包含元素、同位素、键、形式电荷、自由基、立体信息和显式氢；它不以文件名、
目录或坐标为身份依据。`MolecularTopologyDerivation` 记录 MolOP/MolGR 版本、配置和重建
证据，因此重建过程与图身份可独立审计。

`Geometry` 是同一 topology 下的 E(3)-不变坐标等价类。它保存一个展示用 RDKit 3D Mol、
保真内部坐标矩阵及其检索投影。几何身份由 topology、版本化内部坐标和**总电荷、总自旋
多重度**共同决定：坐标完全相同但 charge 或 multiplicity 不同，必须是两个 Geometry。
CalculationFrame 保留源 Cartesian 坐标、打印精度、source-to-geometry permutation 和刚体
变换；振动、梯度和 Hessian 始终解释为该帧源原子顺序下的数组。XYZ 下载在 comment 行写出
Geometry 的全局电荷和多重度。

## 导入与解析状态

浏览器上传、批量上传、显式 reparse 和本地 `tricycle-import-artifacts` CLI 共用同一个
application upload service；CLI 只把本地路径作为字节来源。上传先创建 pending 的
`ArtifactFile`，写入并核验 RustFS/S3 原始对象后转为 `available`，再解析
`calculation_output`。对象、内容哈希、解析 revision 和科学事实都不可原地覆盖。

一次解析创建一个 `ParseRevision`，尽可能保留所有可恢复 segment 和 frame。单个帧的
归一化/持久化错误不会丢弃同一文件中已成功的帧；文件没有可恢复计算帧时，Artifact 仍保存，
但 ingestion 标记为 `filtered`，而不是 `succeeded`。`ArtifactIngestion` 的可见状态为
`pending`、`succeeded`、`partial`、`filtered` 或 `failed`，前端据此区分正在解析、部分成功、
无计算帧与真正失败。显式 reparse 创建新的 revision，不覆盖已有成功结果。

本地导入是流式候选队列：`IMPORT_PIPELINE_WINDOW_FILES` 决定预取候选池，
`TRICYCLE_MOLOP_BATCH_N_JOBS` 决定同时运行的文件 worker，
`IMPORT_COMMIT_BATCH_FILES` 只决定已完成结果的持久化/checkpoint 微批。候选池应大于 worker
数，以便任一文件完成或超时后立即接替下一个文件。单文件解析预算以
`TRICYCLE_MOLOP_FILE_PARSE_TIMEOUT_SECONDS` 为 10 MiB 基准并随文件大小线性放大；超时只
终止该文件任务并释放槽位。限制 `OMP_NUM_THREADS`、`OPENBLAS_NUM_THREADS` 和
`MKL_NUM_THREADS`，而不是把文件并发误当作 native thread 限制。

## TS 前后体推断和反应

全部 frame 都会正常入库。只要 Frame 属于 TS 或 `suspicious_fallback`，系统就会请求 MolOP
对虚频正、负方向给出候选端点并保存 `TransitionStateInference` 证据。Frame 本身为
`suspicious_fallback` 仍会尝试推断；任一端点为 `suspicious_fallback` 时，该 inference 拒绝
生成反应。推断不使用图同构来找回原子对应，也不会将分子规范重排：基准坐标、虚频模式和
正负位移的原子顺序天然一一对应。

端点的整体 charge 和 multiplicity 继承 TS；若 MolGR 输出的是多片段图，各片段的形式电荷
和自由基电子数从图的实际原子标记求和。系统据此构造带 atom map 的 component，创建或复用
`LogicalReaction` 和 `MappedReaction`，并保存端点的 topology、charge、multiplicity、
位移比例及源坐标哈希。前后体图不同正是推断的预期结果，因此不会要求两边显式氢 SMILES、
去自由基 SMILES 或 graph hash 相等。

`LogicalReaction` 以两侧 topology multiset、方向和化学电子标记去重；`MappedReaction`
保存一组确切 atom mapping；Node/NodeGeometry 将反应节点连接到实际 Geometry。系统不会
根据推断自动标记“环加成”或任何反应类别。虚频正负号本身不决定化学方向；方向规则和所有
拒绝原因都作为 inference evidence 持久化。反应和 TS 检索可按“前后体 topology 是否变化”
过滤，该判定比较标准 topology multiset，而非坐标或名称。

## 查询、可见性和派生值

所有 Artifact、Frame、Geometry、Topology、Reaction 和 Inference 查询均经项目可见性过滤。
大规模 Geometry 列表先使用项目几何目录和元素组成等低成本谓词缩小候选，再执行结构、频率
或热化学条件；分页使用确定性排序与页缓存。列表 API 支持明确的排序字段和方向，前端在
请求期间显示查询状态并预取相邻页。

RDKit binary Mol、reaction 和 fingerprint 是查询投影，不替代权威 graph/geometry identity。
`GeometryEnergyView` 与反应热力学 profile 是版本化派生读模型，选择来源 Frame/Protocol 并
标记 `selected`、`missing` 或 `ambiguous`；不会改写原始计算结果。

## 存储与删除

原始文件在 RustFS/S3，PostgreSQL 保存对象定位、内容哈希、权限与解析结果。RustFS bucket
不公开，所有下载由 API 做项目授权。上传失败时同步补偿清理本次 pending 对象；
`tricycle-rustfs-gc` 是用于崩溃恢复的低频、可审计补偿。删除 Artifact 创建 `retired`
tombstone 并删除受控对象，不抹除已经形成的审计事实。
