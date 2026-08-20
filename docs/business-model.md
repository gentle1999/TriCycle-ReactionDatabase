# 环加成反应计算数据库业务模型

> 当前实现：Alembic `20260813_0038`

## 业务聚合

### LogicalReaction 聚合

LogicalReaction 是与计算文件无关的拓扑反应身份。

| 表 | 职责 | 关键身份 |
| --- | --- | --- |
| `logical_reaction` | 净拓扑反应 | 全局唯一 `reaction_hash` |
| `logical_reaction_participant` | 一侧的 topology 与计量 | `(logical_reaction_id, side, participant_index)` |

`logical_reaction_participant.topology_id` 是指向 `molecular_topology.id` 的非空外键，
删除策略为 `RESTRICT`。LogicalReaction 不拥有 manifest、日志、Geometry 或 Frame。

### MappedReaction 聚合

MappedReaction 是 LogicalReaction 下的一套具体 atom mapping，并拥有实际反应节点。

| 表 | 职责 | 关键身份 |
| --- | --- | --- |
| `mapped_reaction` | 一条规范 mapped reaction SMILES | `(logical_reaction_id, mapping_hash)` |
| `mapped_reaction_participant` | template 到 logical participant 的映射 | `(mapped_reaction_id, side, template_index)` |
| `mapped_reaction_node` | 实际反应中的逻辑状态 | `(mapped_reaction_id, node_key/node_index)` |
| `mapped_reaction_edge` | Node 间有向关系 | `(mapped_reaction_id, edge_key)` |

一个 LogicalReaction 可以有多条 MappedReaction。每条 MappedReaction 只保存一条 mapped
reaction SMILES，不再增加一对一的 PathMapping 表。一个 MappedReaction 可以绑定多个 TS
Geometry；这些是同一 transition-state Node 下的坐标构象候选，而不是重复的反应节点。

### 坐标绑定与计算事实

| 表 | 职责 |
| --- | --- |
| `mapped_reaction_node_geometry` | Node 下的 component/coordinate 候选 |
| `mapped_reaction_node_geometry_mapping` | mapped reaction 到 Geometry 规范原子序的 map 验证 |

端点 NodeGeometry 直接引用 `mapped_reaction_participant_id`。
TS/中间体允许该 FK 为空，但必须通过 GeometryMapping 证明其 map 来自所属
MappedReaction。

`MappedReactionNodeGeometry` 只表达反应语义：哪个 mapped node 的哪个 component 使用
哪个 Geometry，以及 reaction atom map 在 Geometry 规范原子序中的对应关系。`CalculationFrame` 通过
`geometry_id` 表达物理计算事实；优化状态、源坐标、坐标匹配误差、计算级别、能量和热化学
结果都只归 Frame 或其结果表所有，绝不复制到反应表。

Geometry 只有在至少一个关联 Frame 提供真实热力学属性时才可绑定反应节点。可接受属性为
ZPE/热校正、内能、焓、Gibbs 自由能、熵或定容热容；温度、压力、分子质量和旋转对称数
本身不构成热力学属性。自动协调跳过不合格 Geometry，显式绑定则拒绝；数据库升级会移除
此前已经存在的不合格 NodeGeometry 及其从属 atom mapping，但保留 Geometry、Frame 和计算结果。

对每条完整 MappedReaction，后端按可比较的电子能来源、热化学来源、温度和压力分组。每组中，
反应物和产物的每个 mapped participant 都只从本映射反应对应的 NodeGeometry 中选择 Gibbs 自由能
最低、且同时具有复合 H、G、S 的 Geometry；H、G、S 始终来自同一个已选 Geometry。TS 候选也
必须属于本 MappedReaction 的 elementary edge 所引用的 TS Node。随后计算活化和反应两组 `ΔH`、
`ΔG`（kcal/mol）及 `ΔS`（cal/mol/K）。不同映射、理论层级或温压不会混算，而是作为独立 profile
返回；缺少任一端点或 TS 的完整 H/G/S 时不返回 profile。

反应详情从 Geometry 展开全部可见 Frame，并请求时计算 `GeometryEnergyView`。它按方法族、
泛函、基组和色散比较电子能候选；只有电荷、多重度、电子态、溶剂、温度和压力兼容时，才将
选出的电子能与热化学校正组合。视图显式返回两个来源的 Frame/Protocol ID，以及
`selected`、`missing` 或 `ambiguous` 状态，因此不需要持久化反应专属的 source role。

Geometry 是可复用的计算坐标事实，不专属于某一反应。一个常见分子的同一 Geometry 可以
同时绑定多条 MappedReaction 的端点 Node；每条绑定分别记录所属 participant 和 atom-map
转换。反过来，一个 Node 也可以拥有多个 Geometry 候选。同一 MappedReaction 的不同
TS 构象复用一个 `transition-state` Node，并以不同 `coordinate_index` 保存多个
NodeGeometry；第一个候选为 primary，后续候选保留为非 primary 构象。

## 化学实体

### MolecularTopology

Topology.mol 是 graph-only `rdkit.Chem.Mol`：

- 图优先来源为 MolOP 中 MolGR 输出；
- 不含 conformer；
- atom map 和计算临时属性被移除；
- 原子顺序不参与 `graph_hash`；
- PostgreSQL 使用 RDKit `mol`，并建立 GiST 结构索引。

计算帧的图优先采用 MolOP/MolGR。创建逻辑反应时，数据库也可以从结构完整的 RDKit
reaction component 创建无 Geometry 的 Topology，并记录 `rdkit/reaction-representation`
来源；后续计算文件按相同 `graph_hash` 自动复用它，不能静默覆盖已有图身份。

重建来源不属于 graph identity。`MolecularTopologyDerivation` 为同一 Topology 保存多条
版本化的 method、version、metadata 和 provenance hash；每个 CalculationFrame 明确
引用产生其图的 derivation。升级 MolGR/MolOP 后重新得到相同图时，会复用 Topology、
新增或复用对应 derivation，不覆盖早期证据。

### Geometry

Geometry 保存 E(3)-不变内坐标和规范 RDKit Mol：

| 字段 | 语义 |
| --- | --- |
| `topology_id` | 实际对应的 graph-only topology |
| `mol` | Topology atom order、规范单 3D conformer 的 RDKit Mol |
| `internal_coordinates` | `[distance Å, angle degree, dihedral degree]` 的 NPY 数组 |
| `internal_coordinate_hash` | 规范内坐标 hash |
| `geometry_hash` | topology、内坐标和规范化版本的联合身份 |

Geometry.mol 便于 ORM 直接获取带坐标分子；CalculationFrame 单独保持日志原始 Cartesian
坐标和 source→Geometry permutation。数据库往返后允许 RDKit cartridge 的约
`1e-6 Å` 坐标误差，几何身份仍以内坐标为准。

## 文件与解析

| 实体 | 职责 |
| --- | --- |
| `ArtifactFile` | RustFS 不可变对象索引、所属项目、创建者、可见性与内容 hash |
| `ParseRevision` | 完整 MolOP parser provenance、配置 hash、artifact 内 revision 序号和 reparse 前驱 |
| `CalculationSegment` | Link1/job 等计算段和 protocol |
| `CalculationFrame` | 文件中的有序帧，引用 Geometry 与 TopologyDerivation，并保存坐标打印精度 |
| Frame result tables | energy、optimization、vibration、thermal、status |
| `ScientificArray` | forces、Hessian、normal modes 等数组 |

WorkflowManifest 只描述一次导入工作流及 artifact 解析位置，不拥有 LogicalReaction。
删除 manifest 不应删除 topology reaction、Geometry 或计算事实。

## 身份与项目授权

| 实体 | 职责 |
| --- | --- |
| `UserAccount` | 本地授权主体，不保存密码 |
| `ExternalIdentity` | OIDC `issuer + subject` 到本地用户的唯一映射 |
| `Organization` / `OrganizationMembership` | 组织边界与 owner/admin/member 角色 |
| `Project` / `ProjectMembership` | Artifact 所属项目与 manager/contributor/viewer 角色 |

公开 Artifact 允许匿名列表、预览和下载。项目内 Artifact 要求成员具有读取权限；上传
要求 contributor 或 manager，修改显示文件名/可见性、退役文件和项目管理要求 manager
或组织 owner/admin。
新建 Artifact 默认是项目内可见，RustFS 不直接暴露公共 bucket 或预签名写入入口。

ParseRevision 以 PyPI distribution version 作为发布构建的版本证据。`parser_provenance`
保留 MolOP 公共 dump；关系型版本列服务筛选和兼容性审计。Git commit 仅在来源确实是
VCS 构建时填写，不以发布版本号或伪 commit 占位。普通导入复用相同科学 identity 的最新
revision；显式 reparse 创建递增 `revision_number` 并以 `reparse_of_id` 连接前驱，不修改
parser/config hash。已有成功 revision 时，失败 reparse 不覆盖 ArtifactIngestion 当前汇总。

## 创建反应 API

`ReactionCommandService.create_reaction` 输入：

- 必需 `reaction`：RDKit 可解析的 reaction SMILES 或 RXN block；
- 可选 label、reaction class、cycloaddition pattern 和 mapped reaction key；
- 不接受 Topology、Geometry、Frame 或 ArtifactFile ID；
- 不要求上传计算文件。

流程：

1. RDKit 通用解析反应表示，并把 component 规范为显式氢 graph identity；
2. 按 `graph_hash` 自动复用 MolecularTopology；不存在时创建无 Geometry 的
   Formula/Topology；
3. 计算或复用 LogicalReaction；
4. 完全无 atom mapping 时停在 LogicalReaction；部分 mapping 明确拒绝；
5. 全部拓扑原子具有守恒 mapping 时创建或复用 MappedReaction 及 endpoint Nodes，并按
   topology identity 反查已有 Geometry/Frame 建立绑定；
6. Geometry、Frame 和 artifact 后续独立导入时，也会反查所有匹配的 MappedReaction，
   为每条反应分别建立 NodeGeometry、atom-map 转换和计算帧绑定。

返回值包括解析到的 topology IDs、实际新建的 topology 数，以及 logical/mapped reaction
是否新建。REST、GraphQL 与 MCP 复用同一个 NexusX UseCase；不开放通用 ORM CRUD。

## 统一上传 API

`POST /api/artifacts` 接收单文件；`POST /api/artifacts/batch` 对多个文件逐个使用独立事务；
`POST /api/artifacts/validate` 仅 probe/解析而不持久化；
`POST /api/artifacts/{artifact_id}/reparse` 校验 RustFS 原始 bytes 后显式创建下一 revision。
Artifact 按内容哈希写入 RustFS。对 calculation output，`ArtifactIngestion` 对 artifact
唯一，MolOP 拆分并持久化全部 segment/frame、坐标、拓扑和计算结果。随后仅对
`frame.is_TS is True` 的帧调用 `possible_pre_post_ts()`；每个 ParseRevision 的每个 TS
帧保存一条 `TransitionStateInference`。

前后体使用同一 source atom order 生成完整 mapping，再复用创建反应 API 的 topology、
logical reaction 和 mapped reaction 唯一身份。重复文件、重复 TS 帧或不同文件推断到
相同前后体时均不会重复创建反应。`TransitionStateInference` 直接关联已入库的 TS
CalculationFrame、LogicalReaction 和 MappedReaction，是创建方式和推断参数的来源证据。
成功推断还会创建或复用该 MappedReaction 的 `transition-state` Node，将 TS Geometry
和 CalculationFrame 绑定到该 Node，并创建或复用 reactant 到 product 的 elementary
Edge。
同一反应的多个 TS 构象不会创建多个 TS Node，而是形成多个 NodeGeometry 坐标候选。

## 删除与不可变性

- Formula/Topology/Geometry、artifact、parse revision 和 calculation fact 是不可变事实。
- Artifact 的不可变性禁止覆盖内容或科学历史；项目 manager 可将 Artifact 退役并移除
  RustFS 对象，但 PostgreSQL tombstone 和已形成的 provenance FK 保留。同项目、同类型、
  同内容的再次上传恢复该 tombstone，不创建第二套科学历史。
- 被 LogicalReactionParticipant 引用的 Topology 不可删除。
- 删除 LogicalReaction 级联删除其 mapped reactions、participants、nodes 和绑定关系，
  不删除 Topology、Geometry、CalculationFrame 或 ArtifactFile。
- 删除 manifest 只删除 manifest 自身的 artifact bindings。
