# 数据模型与存储边界

> 生效 schema：Alembic `20260813_0038`

## 四条主轴

```text
化学主轴
MolecularFormula -> MolecularTopology -> Geometry

文件主轴
RustFS object -> ArtifactFile -> ParseRevision -> CalculationSegment
                    ^                    `-> CalculationFrame -> Geometry
Organization -> Project ----------------|
                    `-> ArtifactIngestion -> TransitionStateInference
                                              `-> LogicalReaction / MappedReaction

访问控制主轴
ExternalIdentity -> UserAccount -> OrganizationMembership -> Organization
                            `----> ProjectMembership ------> Project

反应主轴
LogicalReaction -> LogicalReactionParticipant -> MolecularTopology
       -> MappedReaction -> MappedReactionParticipant
            -> MappedReactionNode -> NodeGeometry -> Geometry
                                      `-> GeometryEnergyView (query-time)

CalculationFrame -> Geometry
```

文件、帧和计算级别都不是反应身份。创建反应时只需要一个 RDKit 可解析的 `reaction`
字符串；后端按 component graph identity 自动复用或创建 MolecularTopology。Geometry、
日志和 Frame 可以稍后独立导入并绑定。

## Formula、Topology 与 Geometry

`MolecularFormula` 只保存元素/同位素组成。电荷、自由基、键级和立体化学不属于 formula。
权威检索表示是按原子序数排列的 118 维 `element_count_vector`，任意元素上下界查询直接
比较对应数组位置。常见的精确元素计数会同时投影为数据库 generated
`element_count_tokens`（例如 `6:2`），并由标准 PostgreSQL GIN `array_ops` 加速包含查询；
该投影不进入 DTO、不取代数值范围谓词，也不引入 pgvector。

`MolecularTopology` 保存权威分子图。优先直接消费 MolOP 导出的 MolGR 图和可信
source-to-topology permutation；数据库不重复实现 MolGR。Topology.mol 入库前会尝试执行 RDKit
sanitize。无论 sanitize 成功与否，连接图都会保留在 `mol` 中；失败图仍可作为 PostgreSQL
RDKit cartridge 的 SMARTS/子结构检索候选，但不进入依赖化学有效性的派生计算。入库时会：

- 移除全部 conformer；
- 移除 atom map 和临时计算属性；
- 保留元素、同位素、键、形式电荷、自由基和已赋值立体信息；
- 使用规范图序列化生成 `graph_hash`，原子顺序不参与身份。
- 写入 `sanitization_status` 与 `sanitization_error`，把 MolGR 重建可信度和 RDKit 化学
  可用性分开记录。

只有 sanitize 成功且存在 `canonical_isomeric_smiles` 的拓扑才生成版本化的
`morgan-bfp-r2-v1` `bfp` 投影，并通过 RDKit GiST 索引支持 Tanimoto/Dice 阈值和 Top-K
相似度检索。失败图的 Morgan、MW、LogP、TPSA、HBA/HBD、环数和 scaffold 结果为 `NULL`；
它们不参与相似度或描述符筛选，但仍可通过 `mol` 的 SMARTS/子结构谓词检索。该投影不参与
图身份，升级指纹规范时必须新增版本并重建索引。

`MolecularTopologyDerivation` 将重建证据与图身份分离。同一 Topology 可以由不同
MolGR/MolOP 版本、配置或 reaction representation 得到，每条 derivation 以版本化
provenance hash 去重。CalculationFrame 通过 FK 指向实际使用的 derivation，因而重解析
相同图不会产生“首个写入版本覆盖后续证据”的歧义。

`Geometry` 保存某一 topology 下的可复用坐标等价类代表：

- `mol`：按 Topology 原子顺序保存、包含且只包含一个规范 3D conformer 的 RDKit Mol；
- `internal_coordinates`：`float64 (N, 3)` NPY/`BYTEA`，列为 distance (angstrom)、angle
  (degree)、dihedral (degree)，参考原子固定为 `i-1/i-2/i-3`；
- `internal_coordinate_*` 检索投影：同一矩阵拆分出的 PostgreSQL `float8[]` 三列，以及
  Geometry 已见 Frame 的最小坐标小数位；它们只用于数据库侧容差匹配，NPY 仍是保真源；
- `internal_coordinate_hash`：规范内坐标矩阵的 SHA-256；
- `geometry_hash`：覆盖 topology graph、内坐标 hash 和单位/规范化版本。

Geometry 先按 `(topology_id, canonicalization_version, geometry_hash)` 精确索引命中。未命中
时，PostgreSQL 函数在检索投影上执行长度、键角、周期二面角和近线性二面角豁免的容差
判断，只返回唯一 Geometry ID 或歧义 ID；不将候选 NPY 载入 Python。只有选定候选后才计算
一次 proper rigid transform，用于 CalculationFrame 的 source 坐标证据。Geometry 不保存任何
上传文件的 source Cartesian 坐标或 source permutation。

PostgreSQL RDKit `mol` 往返会保留 conformer 和原子顺序，但坐标按 cartridge 精度恢复；
精确的几何身份和回配使用 `internal_coordinates`，SDF 展示使用 `Geometry.mol`。
CalculationFrame 独立保存源程序的 Cartesian 坐标、哈希、source→Geometry permutation、
proper transform 以及打印精度，振动/方向相关数组因此仍依赖原始朝向。

## LogicalReaction

`logical_reaction` 表示拓扑层面的净反应。其 participant 直接 FK 到
`molecular_topology`，并保存 side、计量、可选角色和 side 内 index。

`reaction_hash` 按两侧 topology graph hash、形式电荷和计量排序计算：

- 与日志和 Geometry 无关；
- 进入 topology 规范化时先移除全部 atom map，因此与 atom map 和 participant
  输入顺序无关；
- reactant/product 方向有关；
- 全局唯一，重复创建返回同一 logical reaction。

Topology 规范化会显式补氢、清除 atom map 和瞬态 RDKit 属性，按包含手性与同位素的
canonical rank 排序，并从 canonical explicit-H isomeric graph SMILES 生成 graph
hash。因此
键级、形式电荷、自由基、同位素和立体信息仍属于 LogicalReaction 身份；元素、同位素和总
形式电荷必须守恒。LogicalReaction 的权威表示是排序后的 participant 关系和
`reaction_hash`，不额外保存一条未映射 reaction SMILES。v1 的 participant
`stoichiometric_coefficient` 固定为 1，重复组分用多条 participant 表示；反应方向保留，
agent template 不进入净反应身份。

## MappedReaction

`mapped_reaction` 是 logical reaction 的一套具体 atom mapping。完整 reaction
SMILES 由 RDKit `ReactionFromSmarts(..., useSmiles=True)` 解析并用
`ReactionToSmiles` 规范化，不手工拆分 `>` 或 `>>`。

数据库从该规范 mapped reaction SMILES 生成 RDKit cartridge `reaction` 列和版本化的
`reaction-structural-bfp-r5-v1` structural fingerprint。两个投影均不参与反应身份；
reaction SMARTS 使用 reaction GiST，Tanimoto/Dice 阈值与 Top-K KNN 使用 `bfp` GiST，
查询不得逐行从文本临时重建 reaction 或 fingerprint。

每个 reactant/product template 通过图同构匹配一个 logical participant，并在
`mapped_reaction_participant` 保存：

- template side 与 index；
- logical participant FK；
- topology order 的 atom-map 数组；
- 规范 mapped component SMILES。

两侧每个原子必须有唯一正整数 map，且净反应两侧 map 集相同。Agent template 不参与
净反应 topology participant 和 map 守恒。

一个 LogicalReaction 可以拥有多条 MappedReaction。真正不同的反应物到产物原子对应会
保存为不同 MappedReaction；每条映射拥有独立 participant、Node、NodeGeometry、计算绑定
和 Edge，不会覆盖同一逻辑反应下的其他映射。重复提交同一套 RDKit 规范 mapped reaction
SMILES 则按 `(logical_reaction_id, mapping_hash)` 复用。当前映射身份保留 map 数字，因此只
对全部 map label 做一致重命名也会得到另一条 MappedReaction；这不影响 LogicalReaction
去重，但若要在映射层合并此类别名，需要未来引入 map-label-invariant identity 和数据迁移。

## Node 与 Geometry

MappedReaction 拥有一组逻辑 Node。一个 Node 可绑定多个 component、多个坐标候选；
每个 component 最多一个 primary Geometry。

Reactant/Product 端点坐标必须引用 `mapped_reaction_participant`，并满足：

1. participant 属于同一个 mapped reaction；
2. participant side 与 Node role 一致；
3. participant 的 logical topology 等于 Geometry.topology；
4. `MappedReactionNodeGeometryMapping.geometry_atom_map_numbers` 按 Geometry 原子序保存
   reaction mapping；任一 CalculationFrame 的 source order 仅通过该 Frame 自己的
   permutation 转换后与它核对。

TS/中间体可以不对应单一 participant，但其 atom maps 必须是 mapped reaction
全局 map 集的子集，通常为完整集合。

## 统一上传、计算帧与 TS 反应推断

`POST /api/artifacts` 是统一单文件上传入口，`POST /api/artifacts/batch` 对每个文件
独立执行同一流程；两者只接受已认证且具有目标项目 `artifact:upload` 权限的请求。
`POST /api/artifacts/validate` 只用 MolOP 按内容 probe/解析并返回格式、帧数和逐
TS 推断，
不写 RustFS 或 PostgreSQL。正式上传先在 PostgreSQL 提交
`ArtifactFile(storage_status=pending)`，再写入
RustFS；RustFS 写入并通过 SHA-256/大小校验后，服务将该行更新为 `available`。新对象键为
`uploads/YYYY/MM/DD/HH/sha256/{prefix}/{digest}`，按 UTC 小时分区，重复 SHA-256
复用已有 locator。RustFS 磁盘层透明压缩符合条件的对象，但 GET/HEAD 仍返回原始逻辑
bytes。PostgreSQL 的 `ArtifactFile` 保存 locator、项目、创建者、kind 与存储校验信息；
两端不共享事务，pending 状态用于跨后端失败恢复。RustFS 写入、校验或状态提交失败时，
上传生命周期补偿 Hook 在同一内容 identity lock 内立即删除未变成 `available` 的本次对象，
并删除仍指向该 object key 的 pending 预约行；并发请求已完成 available 时 Hook 直接退出。
`missing` 保留给曾经 available、已形成科学或审计历史、后来对象不可用的事实，不用于保存
从未成功上传的预约。

`artifact_kind=calculation_output` 时，MolOP 统一解析全部 source segment 和 frame。
每个帧都创建或复用 Formula、Topology、TopologyDerivation 和 Geometry，并持久化
CalculationFrame、标量结果及 ScientificArray。CalculationFrame 另外保存原始 source-order
Å 坐标、观测坐标哈希、原子 permutation 和从原始坐标到 Geometry 代表的刚体变换；振动模、
梯度和 Hessian 仍以帧的原始坐标系为准。TS 不是另一种上传方式，只是全帧入库
完成后的额外分支。

`ArtifactIngestion` 对 artifact 唯一，是当前成功导入结果的汇总状态；不可变解析历史由
`ParseRevision` 链保存。普通上传按 artifact、export schema、parser provenance、parser
config 和 reconstruction config identity 复用最新 revision；显式
`POST /api/artifacts/{artifact_id}/reparse` 从 RustFS 读取并校验原始 bytes，随后创建
artifact
内单调递增的 `revision_number`，并用 `reparse_of_id` 指向前一 revision。显式 reparse
不会用随机 nonce 伪造 parser/config hash；失败也不会覆盖已有成功 ingestion 汇总。

系统遍历全部 MolOP 帧，但仅当 `frame.is_TS is True` 时调用
`frame.possible_pre_post_ts(show_3D=True, ...)`。每个 TS 帧各有一条
`TransitionStateInference`：

- 同一 ParseRevision + file frame index 唯一，重复上传复用已有 revision 和 inference；
- 前后体按 source atom order 设置完整 atom map；
- LogicalReaction 仍按两侧 topology identity 去重；
- MappedReaction 按 logical reaction + 规范 mapped reaction SMILES hash 去重，创建方式不参与
  身份；
- curated 创建与 TS 推断命中相同映射时复用同一 LogicalReaction 和 MappedReaction；
- 创建 MappedReaction 时反查 participant topology 的已有 Geometry/Frame；创建
  Geometry 或 Frame/热力学结果时也反查全部匹配反应；只有 Geometry 至少有一个
  Frame 提供 ZPE/热校正、内能、焓、Gibbs 自由能、熵或定容热容时，才为每条反应
  独立建立 endpoint NodeGeometry 和 mapping；数据库迁移会删除历史上不满足该条件的
  NodeGeometry 及其从属 mapping，不删除 Geometry、Frame 或计算结果；
- 成功的 TransitionStateInference 创建或复用一个 `transition-state` Node 和 reactant 到
  product 的 elementary Edge；只有 TS Geometry 具备上述热力学属性时才建立 TS
  NodeGeometry；
- 不同 TS 帧推断出同一前后体时不重复创建反应或 TS Node；不同 Geometry 作为同一 TS
  Node 的多个坐标候选保存，并分配不同 `coordinate_index`；
- 反应详情展示绑定 Geometry 的全部可见 CalculationFrame，包括优化 intermediate 帧；
  Frame 是否适合提供特定能量组分由 GeometryEnergyView 的来源选择规则决定，而不产生
  reaction-owned calculation binding；
- `GET /api/mapped-reactions/{id}/thermodynamics` 和对应 NexusX query 在不持久化派生值的
  前提下返回单条 MappedReaction 的热力学 profile。它仅使用该映射反应 NodeGeometry 的
  GeometryEnergyView 单点优选加热校正复合值；每个 endpoint mapped participant 选择最低 Gibbs
  的完整 Geometry，TS 仅来自该映射反应 edge 引用的 TS Node，并在同一理论层级、温度和压力内
  计算活化及反应的 `ΔH`、`ΔG`、`ΔS`。每个 profile 保留 mapped participant、Topology、Geometry
  和 mapped reaction ID 以便追溯；没有完整、相容的反应物/产物/TS 集合则 `profiles` 为空。
- 单帧推断失败只记录该帧错误，其余 TS 帧继续；没有 TS 帧时解析成功但不创建反应。

MolOP 的虚频正负方向本身不定义化学方向。当前按 MolOP 返回语义优先把 fragment 较多的
端点作为 reactant；fragment 数相同的情形保留 mode sign 的任意性，并在 inference
settings 中明确记录。

## 文件与计算

原始 Gaussian/ORCA 文件保存在 RustFS；PostgreSQL 只保存不可变 artifact 索引、解析
revision、source span 和结构化结果。首次成功解析创建 revision 1；普通幂等导入复用相同
科学 identity 的最新 revision；只有显式 reparse 创建下一条可审计 ParseRevision。
不可变指内容 identity 和科学事实不能原地改写，不代表用户不能移除项目文件。
`DELETE /api/artifacts/{artifact_id}` 要求 `artifact:delete` 权限，先将
ArtifactFile 标记为 `retired` tombstone，再校验 SHA-256/大小并移除 RustFS 对象。
退役记录不再参与目录、详情、下载和派生事实可见性；对象存储临时失败时重复 DELETE
可继续清理。同一项目以相同 Artifact 类型重新上传完全相同的内容时，恢复原 ArtifactFile、
RustFS 对象和既有解析历史；跨项目或不同类型的同内容上传仍作为 identity 冲突拒绝。

### 上传补偿与可选 RustFS 垃圾回收

上传补偿 Hook 是默认路径，不列举 bucket，只处理本次上传的 object key。进程强制终止、
宿主机断电、外部写入或 Hook 自身失败不会经过生命周期末端，因此可选的
`tricycle-rustfs-gc` 可由 cron、systemd timer 或 Kubernetes CronJob 低频执行。每个
`bucket + root_prefix` 在 PostgreSQL 的 `storage_garbage_collection_state` 保存上次成功
水位；每次运行在 `storage_garbage_collection_run` 保存扫描窗口、计数和错误。扫描上次水位
到当前时间减 `TRICYCLE_STORAGE_GC_GRACE_PERIOD_SECONDS` 的小时分区，运行期间
新写入的对象
留给下一次运行，并用 advisory lock 防止并发 GC。

对列出的每个对象，`ArtifactFile(bucket, object_key)` 是唯一关系权威：
`available` 保留，没有关系的对象删除；超过宽限期的 `pending` 在内容 identity
lock 内先
删除对象、再删除预约行。数据库已提交但尚未写对象的旧 pending 行也通过
`(storage_status, created_at)` 索引定点清理。任意列举、
删除或数据库错误都会使本次运行失败且不推进水位；重复执行是幂等的。旧版
`uploads/sha256/...` 和非托管前缀不被新增量扫描隐式迁移，需单独安排一次性审计。

每个 `ArtifactFile` 必须关联 `project_id` 和 `created_by_user_id`。`visibility=public`
允许匿名列表、预览和下载；`visibility=project` 只允许具有 `artifact:read` 或
`artifact:download` 权限的项目成员访问。RustFS bucket 不公开，所有读取均由 API
根据 PostgreSQL 授权关系检查后代理。新建 Artifact 默认使用 `project`；只有受控的
发布流程才能显式设置为 `public`。

用户、OIDC 外部身份、组织、项目和成员关系都存放在 PostgreSQL。本系统不保存本地
密码；开发环境使用固定的持久化开发用户，生产环境只接受配置的 OIDC issuer/JWT。
`/api/projects` 提供可访问项目列表、创建、详情和重命名/归档；
`/api/projects/{project_id}/members` 提供已有本地用户的成员列表、添加、角色变更和移除。
创建项目要求组织 owner/admin，项目修改和成员操作要求项目 manager 或组织 owner/admin；
最后一名项目 manager 不能被降级或移除。上传、Artifact 退役和项目管理要求认证。
归档项目不再进入普通项目上下文或数据权限，但管理 API 可用 `include_archived=true`
显式列出并恢复。项目 manager 可查询活跃本地用户目录以添加成员；只有 system
organization owner/admin 可以查看完整用户目录并在不能停用自己或系统服务账号的前提下
切换 `active/suspended`。生产首次管理员由部署侧建立该 system organization 成员关系。
当前统一入口支持 calculation output、input、
workflow manifest 和 auxiliary artifact；只有 calculation output 自动进入 MolOP
全帧解析。Artifact 显示文件名和 `public/project` 可见性可由项目 manager 修改，内容、
hash、对象地址和解析事实不可覆盖。`ProjectInvitation` 保存一次性 token 摘要、过期/接受/
撤销时间和邮件投递状态；接受邀请要求 OIDC 邮箱与邀请邮箱一致。`/api/organizations` 返回
当前用户的组织角色和创建项目权限，允许空组织创建第一个项目。

`ParseRevision.parser_provenance` 原样保存 MolOP 公共
`model_dump(mode="json")` 中的 provenance mapping；`parser_id`、`parser_version`、
`molop_version`、`molgr_version` 和 `rdkit_version` 是用于查询的关系型投影。PyPI
构建不伪造 Git commit，历史 VCS 构建的 `parser_commit`/`molgr_commit` 只作为可选
补充证据。普通解析身份仍由 artifact、导出 schema、provenance hash 和配置 hash 共同
确定；`revision_number/reparse_of_id` 记录显式重解析次序而不改变这些科学
identity hash。

同一 Geometry 可绑定任意量化软件、方法、基组或电子势能面上的多个 Frame。
`parsed_exact` 表示观测坐标与 Geometry 的精确身份一致；`matched_existing_geometry`
必须同时保存原始观测坐标、观测坐标哈希、原子 permutation、刚体变换、RMSD、最大偏差
和匹配策略版本。
Geometry 的 Frame 集合是全部计算事实的唯一归属。`GeometryEnergyView` 是查询时的、
版本化的派生视图：以理论层级选择电子能，以物理上下文筛选热化学校正，返回所选
Frame/Protocol ID、候选 ID 和 `selected`、`missing` 或 `ambiguous` 状态。它不会写回或
创建 reaction-owned energy/source/coordinate-evidence 行。反应详情只是复用该 Geometry
视图；同一 Geometry 被多个 MappedReaction 引用时必然得到同一能量解释。

Frame 子模型的标量结果进入专用一对一表；可重复结构进入子行表。当前覆盖
`MolecularOrbitals`、`ChargeSpinPopulations`、`Polarizability`、`NMR`、
`BondOrders`、`TotalSpin`、`SinglePointProperties`、`ElectronicStates`、
`MultireferenceResult` 和 `ImplicitSolvation`。

forces、Hessian、normal modes、population values、NMR tensor/coupling matrix、
bond-order matrix 和多极矩等数值数组进入 `ScientificArray`。每个由结果模型产生的数组
必须通过 `ScientificArrayAssignment` 连接到具体结果或子行，并保留 MolOP 字段 slot；
原始数值不放 JSONB。
