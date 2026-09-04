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
                         |                 `-> MolecularTopologyDerivation
                         `-> MolecularTopologyAbstraction
                              specific_topology -> general_topology

反应路径
TransitionStateInference -> LogicalReaction -> MappedReaction -> Node -> NodeGeometry -> Geometry

逻辑组分与具体化
LogicalReactionParticipant -> LogicalParticipantConcreteTopology -> concrete MolecularTopology
MappedReactionParticipant -> concrete MolecularTopology
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

Geometry 匹配先固定 topology、canonicalization version、总电荷和自旋多重度，再比较版本化
内部坐标。多个候选都满足等价容差时，按几何距离选择确定性的最近候选；这个选择只解决
Geometry 归属歧义，不修改拓扑、E/Z、R/S、显式氢或 atom mapping。

### 立体拓扑抽象与具体成员

分子拓扑有两种语义，但都保存为独立的 `MolecularTopology` 行：

- **严格具体拓扑（concrete topology）**：由 MolGR 从实际计算端点重建，保留该三维构型
  能够证明的原子、键、显式氢、电子标记和立体标记；它是 Geometry 和严格
  `MappedReaction` 的事实源。
- **逻辑抽象拓扑（general/logical topology）**：只在某一条已确定的抽象需求出现时，从
  严格拓扑复制并选择性清除指定 stereo feature；它用于 LogicalReaction 检索，不承载
  计算坐标，也不能反过来覆盖严格拓扑。

严格拓扑到抽象拓扑的关系保存在 `MolecularTopologyAbstraction` 中，方向固定为：

```text
specific concrete topology -> general abstract topology
```

一条边必须满足以下条件：候选拓扑具有相同的元素/同位素组成、原子数、形式电荷和连接图；
抽象端保留的原子/键立体约束必须被具体端满足；具体端至少比抽象端多一个已指定的原子或
键立体特征。写入时使用立体感知的图匹配，拒绝自环和会形成回边的环。边同时记录
abstraction policy、匹配 schema、原子对应和被抽象的 feature，便于审计和按版本重算。

`molecular_topology.is_stereo_abstraction_upstream` 是“允许作为抽象上游”的显式能力标记。
它不能由 `unknown`、`unassigned`、`ambiguous` 或普通解析失败状态推断；只有由受审查的
抽象投影产生的拓扑才可以设置为 true。找不到已标记且匹配的上游时，有效上游退化为自身，
但不写入自边。

该关系是延迟物化的 DAG，而不是构型生成器：

- 创建严格拓扑时，只检索当前已经存在且被标记的同组成上游并注册匹配边；
- 创建抽象拓扑时，反向检查已经存在的具体下游并补建边；
- 从抽象节点检索具体成员时，沿 `general_topology -> specific_topology` 反向遍历已有边；
- 不枚举理论上所有 stereo feature 的幂集，也不创建未被请求的假想构型。

因此一个含两个独立立体中心的分子可以形成菱形 DAG：两中心具体拓扑分别连接到“只明确
中心 A”和“只明确中心 B”，再连接到“不明确中心”的共同抽象节点。同一具体拓扑可以有
多个抽象上游，同一抽象拓扑也可以汇聚多个具体下游。

抽象投影只清除规则明确选中的 feature。清除 E/Z 时，同时移除 RDKit 用来书写该 E/Z 的
相邻单键方向；与规则无关的 E/Z、R/S 和其他立体信息必须保留。不能使用全局
`useChirality=False` 或“只要是 N 就清除全部 stereo”来代替选择性投影。

当前化学规则是代码拥有的版本化配置，位于
`src/tricycle_reaction_db/core/chemistry_config.py`：

| 配置 | 当前版本/值 | 语义 |
| --- | --- | --- |
| `CALCULATION_PROTOCOL_VERSION` | `calculation-protocol-v1` | 计算协议身份 |
| `FORMULA_COMPOSITION_VERSION` | `formula-composition-v1` | 分子式/同位素组成身份 |
| `TOPOLOGY_IDENTITY_VERSION` | `topology-identity-v1` | 严格分子图身份 |
| `TOPOLOGY_SOURCE_ORDER_STEREO_IDENTITY_VERSION` | `topology-source-order-stereo-identity-v1` | 源原子顺序与立体身份 |
| `TOPOLOGY_DERIVATION_VERSION` | `topology-derivation-v1` | 拓扑重建证据版本 |
| `GEOMETRY_CANONICALIZATION_VERSION` | `geometry-internal-coordinates-v1` | Geometry 内坐标身份 |
| `STEREO_ABSTRACTION_POLICY_VERSION` | `topology-stereo-abstraction-v1` | DAG 边和抽象投影策略 |
| `STEREO_ABSTRACTION_MATCH_SCHEMA_VERSION` | `topology-stereo-abstraction-match-v1` | 抽象匹配证据格式 |
| `INVERSION_STEREO_PROJECTION_POLICY_VERSION` | `reaction-inversion-stereo-projection-v1` | 反应级双侧 inversion 投影 |
| `LOGICAL_PARTICIPANT_CONCRETE_MATCH_POLICY_VERSION` | `logical-participant-concrete-match-v1` | 逻辑组分到具体拓扑匹配 |
| `GEOMETRY_MATCH_POLICY_VERSION` | `geometry-internal-coordinate-match-v4` | Geometry 匹配策略 |
| `REACTION_GEOMETRY_LINK_METHOD` | `topology-identity` | 反应 Geometry 联系方法 |
| `REACTION_GEOMETRY_LINK_POLICY_VERSION` | `reaction-geometry-link-v1` | 反应 Geometry 联系策略 |
| `GEOMETRY_ENERGY_POLICY_VERSION` | `geometry-energy-view-v1` | Geometry 能量派生视图 |
| `MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION` | `mapped-reaction-thermodynamics-v1` | mapped reaction 热力学派生策略 |

当前唯一登记的可翻转规则是 `neutral-trivalent-nitrogen`，原子 SMARTS 为
`[N;X3;v3;+0]`。这是原子规则，不是键规则；规则匹配到原子后，再检查该原子所在的
相邻键及其 stereo reference atom，从而找到受该中心影响的 C=N E/Z 或其他邻接 stereo
feature。新增可翻转中心必须增加经过审查的规则和版本，不能通过环境变量静默改变持久化
化学身份。

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

`ParseRevision.running_time_seconds` 保存 MolOP 报告的文件级计算用时；
`CalculationFrame.running_time_seconds` 保存逐帧用时。两者语义不同，文件级用时不能用逐帧用时
求和替代。

本地导入是流式候选队列：`IMPORT_PIPELINE_WINDOW_FILES` 决定预取候选池，
`TRICYCLE_MOLOP_BATCH_N_JOBS` 决定同时运行的文件 worker，
`IMPORT_COMMIT_BATCH_FILES` 只决定已完成结果的持久化/checkpoint 微批。候选池应大于 worker
数，以便任一文件完成或超时后立即接替下一个文件。单文件解析预算以
`TRICYCLE_MOLOP_FILE_PARSE_TIMEOUT_SECONDS` 为 10 MiB 基准并随文件大小线性放大；超时只
终止该文件任务并释放槽位。限制 `OMP_NUM_THREADS`、`OPENBLAS_NUM_THREADS` 和
`MKL_NUM_THREADS`，而不是把文件并发误当作 native thread 限制。

## TS 前后体推断和反应

### TS 端点是三维事实源

全部 frame 都会正常入库。只要 Frame 属于 TS 或 `suspicious_fallback`，系统就会请求 MolOP
沿虚频正、负方向生成候选端点，并保存 `TransitionStateInference` 证据。Frame 本身为
`suspicious_fallback` 仍会尝试推断；任一端点为 `suspicious_fallback` 时，该 inference 拒绝
生成反应。基准坐标、虚频模式和正负位移使用同一源原子顺序，因此端点推断不通过图同构找回
原子对应，也不做规范原子重排。

端点的整体 charge 和 multiplicity 继承 TS；多片段图的形式电荷和自由基电子数从实际原子
标记求和。三维端点按以下顺序形成严格反应事实：

```text
TS frame + imaginary mode
  -> positive/negative displaced 3D endpoints
  -> MolGR endpoint graph with source atom order
  -> strict concrete MolecularTopology and Geometry
  -> exact atom maps from source correspondence
  -> strict MappedReaction and mapped_reaction_smiles
```

`mapped_reaction_smiles` 是严格端点三维图的带 atom map、显式氢和 isomeric stereo 的序列化
投影；它不是拓扑或 Geometry 的来源。不能先从普通 SMILES 投影立体构型，再用该 SMILES
覆盖三维事实。端点的 topology、charge、multiplicity、位移比例和源坐标哈希都属于可审计的
推断证据。前体与后体的图不同是正常结果，不要求两边显式氢 SMILES、去自由基 SMILES 或
graph hash 相等。

前体/后体只是存储顺序约定，不是 inversion 规则的化学方向。虚频正负号本身也不决定化学
方向；方向规则和所有拒绝原因必须保留在 inference evidence 中。

### 严格反应、逻辑反应和可翻转中心

`MappedReaction` 表示一条严格的具体反应：每个参与物都有 concrete topology、完整 atom
mapping 和由这些严格拓扑序列化得到的 mapped reaction text。`LogicalReaction` 是其检索
抽象，使用抽象 topology 参与物，不应被当作具体 Geometry 或具体构型。一个
`LogicalReaction` 可以包含多条构型不同的 `MappedReaction`。

逻辑投影只对完整严格 mapping 的反应组件执行，并对两侧对偶处理：

```text
strict mapped endpoints
  -> inspect inversion-labile atoms on both sides
  -> collect their authoritative atom-map numbers
  -> propagate the same map set to both endpoints
  -> clear only stereo features dependent on those atom maps
  -> persist/reuse logical topology and abstraction DAG edges
  -> create/reuse LogicalReaction
```

当前登记的可翻转规则只有 `[N;X3;v3;+0]`，即中性三价 N。规则匹配的是**原子**而不是键；
匹配原子后检查其相邻键、相邻原子和 double-bond stereo reference atom。这样，后体或 TS 中
的 sp3 N 可以提供 labile atom map，即使前体中的对应 N 已经是 sp2，也能清除该 map 依赖的
C=N E/Z。该规则同时覆盖前体和后体，不能只按“前体”或“后体”单向判断。

投影时只清除命中的原子手性、相关双键 E/Z 及其书写所需的相邻单键方向；不相关的 E/Z、
R/S、键连接、显式氢、形式电荷和自由基标记全部保留。没有完整 mapping 时不能跨端点传播
规则，也不能伪造一条严格 `MappedReaction`。

### LogicalReaction 与 concrete topology 的双向归属

`LogicalReactionParticipant.topology_id` 保存抽象查询拓扑，
`MappedReactionParticipant.concrete_topology_id` 保存严格具体拓扑。二者通过
`LogicalParticipantConcreteTopology` 独立连接：

```text
LogicalReaction
  -> LogicalReactionParticipant (abstract topology)
      -> LogicalParticipantConcreteTopology
          -> concrete MolecularTopology

MappedReaction
  -> MappedReactionParticipant
      -> concrete MolecularTopology
```

具体成员匹配的候选先按 formula、原子数和形式电荷过滤，再以抽象 topology 作为 query、
具体 topology 作为 target 执行立体感知的分子图匹配。最终关系不是由 topology hash 相等
决定；hash 只能作候选索引。匹配必须保持相同连接图和抽象端的所有剩余立体约束，不能把
不同 fragment、不同电子状态或不同组成当作同一成员。

该成员关系可以在没有完整 reaction mapping 时独立建立，记录 match policy、schema、候选
数量、原子对应和可重算的审计 metadata；它不因此伪造 `MappedReaction`。如果存在多个合法
图匹配，必须保留全部候选，交由反应级 mapping、side 和 occurrence 约束消歧，不能任意取
第一个。

归属必须支持两个创建方向：

1. **先有具体拓扑，后有逻辑反应：** 创建逻辑参与物时，反向遍历当前 DAG 中已经物化的
   具体下游，建立所有匹配的 concrete membership；不展开理论上不存在的构型。
2. **先有逻辑反应，后有具体拓扑：** 新拓扑创建时检索现有逻辑参与物，按同一图匹配建立
   membership；若该 LogicalReaction 下已有完整 mapped reaction template，再补建新的严格
   mapped reaction，否则只保留 concrete membership。

### 新具体构型的 mapping 转移

新具体拓扑不需要自己携带一套新的 atom-map 文本。只要它属于某个逻辑参与物，且同一逻辑
反应下存在完整严格 mapping，就通过两次图匹配转移：

```text
source concrete atom
  -> abstract topology temporary atom index
  -> target concrete atom
```

实现上由已有 mapped reaction 提供唯一的源 atom-map 标签；源 concrete topology 和目标
concrete topology 都必须匹配同一个抽象 topology，再组合两条对应关系，得到目标 topology
的完整 atom maps。目标 SMILES 只在 mapping 完成后从目标三维拓扑序列化，绝不作为 mapping
来源。

如果图对称或立体约束导致多个完整 mapping 候选，系统必须记录/抛出 mapping ambiguity，
不得任意选择一个。成功转移后，用目标 strict mapped SMILES 计算 `mapping_hash`，并在同一
`LogicalReaction` 下按 `(logical_reaction_id, mapping_hash)` 做严格文本幂等；相同 canonical
mapped text 不允许重复形成两条 mapped reaction。

由 template 派生的 mapped reaction 共享源 mapped reaction 已有的 TS/端点证据和可复用
Geometry 联系，但保留自己的 mapped reaction、participant 和 node 记录。它不会复制或伪造
新的计算事实。

### Geometry 与反应的双向绑定

Geometry 必须绑定 concrete topology，满足：

```text
Geometry.topology_id == MappedReactionParticipant.concrete_topology_id
```

`MappedReactionNode` 描述反应路径状态，`MappedReactionNodeGeometry` 将节点接到实际
Geometry，`MappedReactionNodeGeometryMapping` 保存 Geometry atom order 与 mapped reaction
atom maps 的对应及验证证据。创建 mapped reaction 时会回溯已有 concrete Geometry、热力学
和无虚频计算帧；创建 Geometry 时也会反向检索已经存在的 mapped reaction participant，补建
节点和 Geometry 联系。逻辑抽象 topology 本身没有三维构象，不能直接充当 Geometry 证据。

### Reaction 身份和校验边界

`LogicalReaction` 以两侧抽象 topology multiset、side/participant 顺序和化学电子标记去重；
它不依赖文件名、目录、坐标、Geometry 或反应名称。`MappedReaction` 以 canonical strict
mapped reaction text 的 `mapping_hash` 作为文本身份，并要求 reaction 两侧原子映射守恒、
参与物数量/角色/计量与逻辑参与物一致。

映射持久化阶段的校验只确认：序列化文本与已经确定的 concrete topology 和 atom maps 一致、
RDKit 表示可解析、canonical text 与 `mapping_hash` 一致、两侧 mapping 覆盖一致。校验不能
重新推断、修正或改写三维来源已经确定的 E/Z、R/S、显式氢或原子对应；发现不一致应拒绝
持久化并保留错误证据，而不是静默改变标记。

系统不会根据三维推断自动赋予“环加成”或其他反应类别。反应和 TS 检索在需要具体构型时按
`MappedReaction` 行进行，再按 `LogicalReaction` 分组展示。

## 查询、可见性和派生值

所有 Artifact、Frame、Geometry、Topology、Reaction 和 Inference 查询均经项目可见性过滤。
大规模 Geometry 列表先使用项目几何目录和元素组成等低成本谓词缩小候选，再执行结构、频率
或热化学条件；分页使用确定性排序与页缓存。列表 API 支持明确的排序字段和方向，前端在
请求期间显示查询状态并预取相邻页。

RDKit binary Mol、reaction 和 fingerprint 是查询投影，不替代权威 graph/geometry identity。
`GeometryEnergyView` 与反应热力学 profile 是版本化派生读模型，选择来源 Frame/Protocol 并
标记 `selected`、`missing` 或 `ambiguous`；不会改写原始计算结果。

每个 `MappedReaction` 热力学 profile 还记录前体、过渡态、后体和全路径文件级计算用时。
这些值按 profile 实际涉及的计算帧所属 `ArtifactFile` 去重后，将各文件对应最新解析 revision
的 `ParseRevision.running_time_seconds` 相加；全路径总用时对三部分的文件并集再次去重。反应路径
CSV 导出包含这四列，不能用逐帧用时之和替代。

## 存储与删除

原始文件在 RustFS/S3，PostgreSQL 保存对象定位、内容哈希、权限与解析结果。RustFS bucket
不公开，所有下载由 API 做项目授权。上传失败时同步补偿清理本次 pending 对象；
`tricycle-rustfs-gc` 是用于崩溃恢复的低频、可审计补偿。删除 Artifact 创建 `retired`
tombstone 并删除受控对象，不抹除已经形成的审计事实。
