# 数据库架构重构计划

[English](en/database-architecture-refactor-plan.md) | [文档索引](README.md)

> 建立日期：2026-09-10
>
> 状态：批次 A 的最小修复、项目级查询边界和历史派生数据清理已在代码/远端操作中实施；
> 源码与远端数据库已到 `0038_geometry_match_index`。从 RustFS 的双项目全量重导入
> 已启动并按 checkpoint 持续运行，最终双项目验收仍在进行。
>
> 评估基线：Git `522405a` 加当时工作区已有改动；ORM 元数据包含 63 张表。
>
> 代码迁移末端：`0038_geometry_match_index`；`0038` 为项目内 Geometry 候选匹配索引，
> 已在部署数据库应用并通过 `alembic current` 核验。
>
> 范围：科学事实、来源授权、热力学查询、版本与完整性、派生刷新、几何匹配和数组存储。

本文是本轮数据库重构的执行计划，不表示目标模型已经完全实现。现行行为仍以
[数据模型与存储边界](data-model.md)、代码和测试为准；完成各阶段后同步更新当前契约。
[技术路线图](technical-roadmap.md)、[旧实施清单](implementation-backlog.md)和
[上线计划](refactor-plan.md)保留原日期及验收记录，本轮新增工作统一使用 `DB-*` 编号。
计划文本本身不把生产发布、数据清理或历史事实改写视为完成；本轮部署记录单独列出。

## 本轮实施记录（批次 A 最小修复）

- DB-01：成功的 `ParseRevision` 与 `ArtifactIngestion`、`ArtifactFile` 生命周期及来源
  Frame 可见性已进入统一读路径；profile 新写入会保存电子能和热化学来源 Frame，旧
  profile 暂以可见 Geometry 兼容回退。独立 `profile-source` 关系、历史回填和双项目
  集成验收仍未完成。
- DB-02：同分但协议不同、或同协议数值冲突的候选不再按最早 Frame 任意选择；只有协议
  完整且观测值在声明容差内等价时才允许确定性选择。完整协议策略、QC 和有效 revision
  模型仍待后续批次。
- DB-03：列表、详情、筛选、统计和导出统一使用同一可见 profile 的精确 `EXISTS` 条件；
  `MappedReaction` 旧 min/max 仅保留兼容字段，不再作为受限用户的最终判定依据。
- 历史数据跨项目隔离：源码迁移 `0035`～`0037` 已将 Formula、Topology、Geometry、
  LogicalReaction、MappedReaction、TS inference、CalculationProtocol 及其关系改为项目
  归属，并由数据库触发器拒绝跨项目写入。随后已按约定清空全部派生表，仅保留
  `ArtifactFile`、用户/组织/项目数据和 RustFS 原始对象；旧的派生回填/隔离台账不再作为
  当前数据源，待最终迁移后从每个 ArtifactFile 所属项目重新物化。
- 查询边界：所有派生查询都要求显式 `project_id`，并用当前认证用户的项目权限作为第二个
  范围条件；缺少项目或无权访问时 fail closed。只有不可变 `ArtifactFile` 可作为跨用户/项目
  的原始对象缓存，且仍按项目过滤、不带出派生状态或元数据。
- 查询性能：项目权限先通过单目标索引 `EXISTS` 校验；项目级派生查询使用直接的
  `project_id = :project_id` 条件，避免逐行遍历来源链。Geometry 候选匹配增加项目优先的
  复合索引；实际 `EXPLAIN` 已观察到该条件进入 `Index Cond`。
- 文件导入恢复：新增 `0034_ingestion_recovery_lease`，worker 会认领没有活动
  `UploadBatchItem` 的过期 pending ingestion，并校验 RustFS 对象后续解析；lease fencing
  防止迟到结果覆盖新尝试。此前 16 条孤儿任务收敛的结果仅作为清理前证据；清库后必须
  使用新 checkpoint 从 RustFS 重新导入，不能把旧 checkpoint 当成完成证明。
- 验证记录：`0038` 已部署，服务 readiness 已恢复，代码级 unit、Ruff、编译和查询计划
  检查已通过；双项目重导入、最终隔离审计和完整数据计数仍未完成。清理前的
  `artifact_file=125355`、AutoDE 活跃文件 `36342` 等数量仅是历史基线。

## 1. 证据与问题清单

初始评估读取了 ORM、迁移、核心写入和查询代码，并进行了内存级验证；当时配置的数据库
连接不可用。实施阶段已核对部署数据库、应用迁移并观察恢复 worker，但隔离 fixture、完整
执行计划和并发基准仍未完成。代码可表达的风险不等于已经观测到的生产事故；以下证据边界
按当前状态维护。

| 工作项 | 优先级 | 已有证据与问题 | 证据边界 |
| --- | --- | --- | --- |
| DB-00 | 前置 | 冻结数据、迁移和性能基线 | 已记录部署 revision、核心行数和隔离审计；隔离 fixture、完整基线和计划测量待补 |
| DB-01 | P0 | [profile 来源加载](../src/tricycle_reaction_db/application/services/mapped_reaction_thermodynamics_persistence.py)、详情、列表、统计和 CSV 已统一按成功 ingestion/revision、退役状态和来源 Frame 可见性过滤；派生根历史数据已按项目隔离 | 最小修复与历史 containment 已落地；独立 profile-source 表、profile 回填和双项目集成验收待补 |
| DB-02 | P1 | [能量选择](../src/tricycle_reaction_db/application/services/geometry_energy.py)对同分不同协议或同协议冲突结果返回歧义；数值等价候选才可确定性选择 | 单元反例已覆盖；完整协议策略、QC 和有效 revision 接入待补 |
| DB-03 | P1 | [范围查询](../src/tricycle_reaction_db/application/services/queries.py)及统计/导出已使用真实可见 profile 的同一 `EXISTS` 谓词，不再用全局 min/max 包络作最终判定 | SQL 编译回归已通过；隔离数据集 EXPLAIN 和规模基准待补 |
| DB-04 | P1 | [可见版本](../src/tricycle_reaction_db/application/services/query_visibility.py)包含全部解析 revision；[membership](../src/tricycle_reaction_db/application/services/reaction_topology_membership.py)原地更新证据；profile 删除后重建 | 历史记录存在，但当前选择与历史分析复现缺少独立模型 |
| DB-05 | P1 | [反应关系](../src/tricycle_reaction_db/db/models/reactions.py)、[TS](../src/tricycle_reaction_db/db/models/uploads.py)、[数组 owner](../src/tricycle_reaction_db/db/models/calculations.py)已增加项目归属字段与历史隔离台账；写入路径不再跨项目复用派生身份 | 历史归属/来源链审计已为 0；父级复合 FK、非法写入数据库拒绝和完整关系约束仍待补 |
| DB-06 | P1 | [导入收尾](../src/tricycle_reaction_db/application/services/molop_artifact_ingestion.py)同步展开关联和热力学；[项目计数](../migrations/versions/0012_project_geometry_catalog_listing_summary.py)集中更新 | 锁竞争与写入放大为结构性风险，未做实测 |
| DB-07 | P1/P2 | [连续内坐标](../src/tricycle_reaction_db/domain/internal_coordinates.py)存在共线退化；[匹配](../src/tricycle_reaction_db/application/services/molecular_geometry.py)容差受最低打印精度影响 | 内存验证不同坐标可得到相同内坐标哈希；[归一化](../src/tricycle_reaction_db/ingestion/normalization.py)有重建误差拒绝保护，未证明已经错误合并 |
| DB-08 | P2 | inference 唯一键为 revision/frame，endpoint 唯一键为 frame/direction | 不便在同一 Frame 上并列保存多个模式、参数或算法的推断 |
| DB-09 | P2 | [ScientificArray](../src/tricycle_reaction_db/db/models/calculations.py)全部内联；[NPY](../src/tricycle_reaction_db/db/types/numpy_array.py)单载荷默认上限 64 MiB | 容量分层方向明确，外置阈值须由真实数组分布决定 |

## 2. 目标边界与固定决策

继续使用 PostgreSQL/RDKit、RustFS/S3 和统一 application service。保留 Formula、Topology、
Geometry、LogicalReaction、MappedReaction 的领域分层，保持源坐标、atom order、显式氢、
电子标记和立体化学证据。新增关联表和投影，不批量重写既有科学身份。

| 职责 | 目标模型方向；名称为设计候选 | 规则 |
| --- | --- | --- |
| 原始事实 | ArtifactFile、ParseRevision、CalculationFrame、原始结果 | 追加保存，原始 bytes 和解析事实不原地替换 |
| 有效解析 | `ArtifactRevisionSelection` 或等价显式选择记录 | 当前有效 revision 与历史 revision 分开；记录选择策略、原因和时间 |
| 来源授权 | profile 到实际 Frame/Revision 的关系，例如 `ThermodynamicProfileSource` | 共享化学身份不能授予私有计算访问权；筛选、排序和统计也必须遵守来源权限 |
| 分析复现 | `AnalysisSnapshot`、带版本的 profile 与 membership evidence | 固定实际来源、配置和算法；历史快照读取仍须通过当前授权 |
| 独立推断 | `InferenceRun`、run-owned Endpoint | 推断由 Frame、模式、版本和配置标识，不要求重新解析原文件 |
| 当前读模型 | scoped thermodynamic projection、Geometry catalogue | 可重建，记录输入水位、策略和刷新状态；不能作为最终授权依据 |
| 派生任务 | 同事务写入的 `ProjectionRefreshTask` 或 outbox | 去重、lease、重试、追赶更新；沿用数据库 worker，无需引入新消息中间件 |
| 数组内容 | 小载荷内联，大载荷引用内容对象 | 元数据关系化；下载显式授权，ORM 属性不隐式发起对象存储请求 |

固定约束：

1. P0 修复不得等待完整 schema 重构。先在现有读路径对可见来源重算或严格验证来源；
   无法证明安全的派生数值返回明确的不可用状态，不能回退到全局汇总。
2. project/public 投影是优化方式，不是权限真相。跨项目组合查询必须按请求者实际可见的
   来源集合计算；不能通过简单合并各项目最优值代替重新选源。
3. 退役、权限撤销和可见性变更在后续请求中立即生效。缓存键与授权/来源水位关联，缓存命中
   也不能跳过授权。历史快照不授予已经撤销的访问权。
4. 成功 reparse、选源策略变化和缓存刷新是不同操作。默认不自动采用 failed、filtered、pending
   revision；partial revision 仅经显式 QC 选择，不覆盖上一有效成功版本。
5. 科学方法评分只能作为显式分析策略，不能把未知方法或同分的不同协议视为等价；
   方法、基组、电子态、溶剂、温压和适用标准态须能追溯。
6. Geometry 的精确身份和近似匹配分开。原始 Frame 坐标、匹配证据和版本保留；不放松
   stereochemistry、电子态或原子对应来提高命中率。
7. 不以清库、重新导入全部文件或删除旧 revision 作为默认迁移方案。阶段完成需有代码、
   迁移/接口、测试和文档证据；跳过或环境不可用不等于通过。
8. 跨用户/项目共享边界固定为不可变的 `ArtifactFile`。`ParseRevision`、`CalculationFrame`
   以及 Formula、Topology、Geometry、Reaction 和所有派生关系必须沿同一个 ArtifactFile
   的项目归属访问；不能因为内容哈希、图身份或 reaction hash 相同而跨项目复用同一派生行。

## 3. 工作项与验收

### DB-00：冻结基线与回归样本

- 状态：`todo`。依赖：无。
- 记录 Git revision、工作区差异、实际 Alembic revision/head、PG/RDKit 版本；已有用户改动
  单独列入记录，不混入本轮实施。统计表/索引体积、行数、数组大小分布及可用查询计划。
- 在隔离数据库构建 public、私有 A、私有 B 三种来源，包含共享反应/Geometry、同协议多次
  解析、不同协议同分、退役源、无匹配 profile 区间、退化坐标和重复刷新场景。
- 为 DB-01～DB-09 分别记录可重复的失败或结构风险，输出基线和命令结果；无需等完整规模
  压测结束才开始 DB-01。默认运行环境与测试数据库必须明确分离。
- 验收：样本哈希、环境信息和运行记录可复用；生产数据未变；性能数据明确区分实测和待测。

### DB-01：来源级授权与生命周期隔离

- 状态：`partial`（批次 A 最小修复）。依赖：DB-00 的隔离授权 fixture；独立
  profile-source 关系和完整跨项目验收仍待完成。
- 第一批改动覆盖热力学详情、反应摘要、范围/存在性筛选、排序、分页 total、统计和 CSV，
  REST/GraphQL/MCP 继续复用同一服务。不能只在返回 DTO 时隐藏数值。
- 选源前限定可见 Frame/Revision，并排除退役来源；汇总返回的 Frame ID、候选数、运行时间
  和缺失/歧义状态也不得携带隐藏来源信息。运行时间使用实际选中来源的文件/revision，
  按文件去重，不能累计同 Geometry 下所有无关文件。
- 增加 profile-source 关系，记录 role、component、Frame、Revision、Protocol、来源用途
  （电子能/热校正等）；用 FK 保持引用一致。缓存中无法重建来源的旧 profile 先标记待重算。
- `MappedReaction` 上旧全局 min/max 停止用于面向受限用户的最终返回和判定；保持 DTO 字段
  兼容但值由可见来源投影提供，缺失时按明确状态返回。
- 验收：A 的查询结果不因仅对 B 可见的计算增加、修改可见性或重解析而变化；public 用户
  同样隔离；取消公开、撤销成员和 retire 后下一请求不可继续读到旧数值。跨项目授权用户
  能获得组合来源的正确结果，各 transport 结果一致。

### DB-02：明确计算选源策略

- 状态：`partial`（批次 A 同分/冲突处理）。依赖：DB-01 的来源集合规则；完整协议
  策略和 DB-04 的有效版本选择仍待接入。
- 用完整协议与物理上下文分组保留候选，不先把每个 Geometry 永久压缩成一个“最高级别”结果。
  完整协议标识保留软件、版本和归一化配置；可跨软件比较须由显式兼容策略决定。
- 相同评分但不同协议返回独立候选或 `ambiguous`。同协议结果存在实质差异也不得任意取最早
  Frame；只有单位、语义和数值在声明容差内等价时，才使用确定性规则选择代表。
- 选源优先限定有效 revision，使用计算状态/QC，再应用显式策略。策略版本、候选、拒绝原因
  和精确来源随结果保存。复合电子能和热校正要求上下文兼容。
- 验收：未知泛函同分、相同评分但不同基组、重复等价结果、冲突结果、缺失协议、跨软件
  和不兼容溶剂/温压均有测试；交换导入顺序不改变科学选择；重解析修正可按选择策略生效。

### DB-03：真实 profile 区间命中

- 状态：`partial`（批次 A 精确谓词）。依赖：DB-01 的来源授权；隔离数据集 EXPLAIN
  和规模验收待补，可在 DB-02/DB-04 完成前继续使用该修复。
- min/max 仅作候选预筛，最终使用真实可见 profile 的 `EXISTS`。同时指定活化能、反应能、
  温压或协议时，由同一个兼容 profile 满足所有条件。
- 列表、total、统计和导出采用相同最终谓词；排序值也只来自符合查询条件的可见来源。
- 验收：5、25 查询 10～20 不命中；10 和 20 的边界行为明确；NULL 不当作零；多个条件
  不能由不同 profile 拼接满足；使用 EXPLAIN 证明预筛和精确过滤不存在无界重复扫描。

### DB-04：有效 revision、证据版本和分析快照

- 状态：`todo`。依赖：DB-00；与 DB-02 接口协同，不能阻塞 DB-01 的最小修复。
- 增加 Artifact 的有效 revision 显式选择及其审计历史。同一 Artifact 的同一选择策略
  只能有一个当前选择，选择必须指向自己的 revision；并发选择使用版本检查或行锁。
- 历史回填默认选择符合 QC 的最高成功 revision；没有合格版本时保留未选择状态；partial
  和其他例外进入待复核清单。成功自动晋升须由固定版本策略定义，不以创建时间猜测。
- 默认科学查询使用有效集合；历史/指定 revision 查询单独表达。project Geometry 目录、
  frame_count 和能量来源切换到相同口径，保留历史计数时明确字段含义。
- membership 身份与每次匹配证据分开，旧策略证据追加保留。分析快照固定 revision、Frame、
  inference、协议、计算公式、单位/标准态、选源策略及输入哈希；当前投影可重建，已发布快照
  不删除后重建。存量历史 profile 缺失的证据明确标记，不能推造历史。
- 验收：成功修正、失败 reparse、partial reparse、并发晋升、切回历史版本均有确定结果；
  同一快照可复算，策略升级不会改写旧结果，权限撤销仍能阻止历史快照泄露。

### DB-05：补齐同父级与科学关系约束

- 状态：`partial`（已完成项目归属回填、历史隔离和写入复用边界）。依赖：DB-00；有效
  revision 的约束与 DB-04 一并设计。
- 迁移 `0035_project_owned_derived_data` 已为派生根对象增加 `project_id`，为无法唯一归属
  的历史对象建立 `derived_data_isolation_quarantine`；原始文件和解析事实保留，普通查询不
  读取隔离对象。
- 审计并补齐：mapped participant 与 logical participant 同属一个 logical reaction；
  node 与绑定 participant 同属一个 mapped reaction；inference 的 ingestion/revision/frame
  及 logical/mapped reaction 对应一致；reparse_of 与当前 revision 属于同一 Artifact。
- ScientificArray 与其 owner 同属一个 Frame；Frame 的 Geometry、topology derivation 和
  电子状态一致。优先使用父级组合唯一键与复合 FK，复杂跨表关系才使用受控约束触发器。
- 完成 `concrete_topology_id` 回填与核验后加 `NOT NULL`，移除逻辑 topology 的隐式回退。
  失败行进入可审计待修复清单，保留原始值，不能为通过约束而随意选一个拓扑。
- 验收：对每类关系直接执行非法 SQL 插入/更新都会被数据库拒绝；合法批量写入、合法删除
  和事务回滚通过；无悬挂引用；所有新增 FK/CHECK 已验证，兼容写路径已有明确移除节点。

### DB-06：派生刷新与事实事务解耦

- 状态：`todo`。依赖：DB-01、DB-04、DB-05；DB-00 提供写入基线。
- 事实事务同时写入去重的刷新任务；任务覆盖新 Frame、有效 revision 切换、关联改变、
  退役和策略更新。任务处理采用有界批次、`SKIP LOCKED`、lease、重试和错误可观测性。
- 刷新键包含目标、scope 和策略；变更计数/水位确保 worker 运行期间到达的新事件不会丢失。
  过期 lease 或旧输入水位不能覆盖较新投影；提交结果和推进任务状态保持事务一致。
- 用户结果可带 `pending/ready/failed` 与计算版本，但不能把旧全局 profile 当回退值。
  ACL 和来源撤销走即时检查，不能等待 worker。需要强一致结果时走有界同步计算。
- 精确身份唯一约束保留。逐步迁移昂贵图展开、热力学和计数，先测量再决定移除哪些触发器；
  提供投影全量重建、按目标重试和对账能力。
- 验收：并发导入、重复任务、worker 崩溃、过期结果提交、刷新途中新增源均收敛；离线重建
  与增量结果一致；披露锁等待、事务时长、吞吐及刷新滞后，不以放松一致性伪造提升。

### DB-07：稳定几何匹配与退化坐标支持

- 状态：`todo`。依赖：DB-00 的坐标样本、DB-04 的证据版本机制。
- 先补性质测试：固定 topology/电子态下旋转平移、合法原子排列、共线/近共线、多片段、
  镜像与 stereochemistry、不同打印精度和不同导入顺序。
- 明确精确表示与容差归属：低精度新观测不得改变已发布身份或无界扩大历史匹配簇；歧义
  显式记录；保留完整 source-to-geometry permutation、刚体变换与策略证据。
- 设计有参考原子索引、可处理退化情况的内坐标或等价保真表示。保留重建误差保护，在最终
  候选匹配时检查 Cartesian RMSD/最大偏差。规范查询投影不能改写 MolGR 事实图。
- 新表示使用新 schema/policy 版本，新增旧新身份映射或 assignment 版本；不原地替换旧哈希
  和 Frame 来源。迁移对照确认后才切换默认匹配策略。
- 验收：正常输入无精度回退；退化输入可保真保存或明确隔离；不同导入顺序给出相同的
  规则结果；不合并不同电子态/立体事实；旧数据仍能按原版本解释。

### DB-08：独立推断运行与端点

- 状态：`todo`。依赖：DB-04、DB-05。
- 增加 run 标识，幂等键至少包含 Frame、imaginary mode、算法/版本及配置哈希；重试 attempt
  与科学 run identity 分开，失败和拒绝也保留。Endpoint 归属于 run，按 run/direction 唯一。
- 映射/反应关联记录来源 run 和派生方式；由模板转移的 mapping 区分直接 TS 证据与转移
  证据，不能把复用关系解释成新增计算事实。当前采用的推断有显式选择。
- 旧 inference/endpoint 以已知 provenance 建立 legacy run；无法确认模式/参数的记录标记
  provenance 不完整，不能假设当前默认值就是历史设置。
- 验收：同一个 Frame 的多个模式、位移比例和算法结果并存；同配置重试不重复科学结果；
  切换推断不新建 ParseRevision；旧反应链和源原子顺序仍可追溯。

### DB-09：数组分层与规模验证

- 状态：`todo`。依赖：DB-00 的容量分布；内容迁移需 DB-05 归属规则。
- 记录数组数量、大小分位数、访问频率、重复率和 WAL/备份成本后决定外置阈值。
  小数组继续内联；大数组保留 frame/owner 元数据，引用校验过的不可变内容对象。
- 内容按明确的编码 schema、哈希和长度去重。先写对象并核验，再发布数据库引用；崩溃
  孤儿进入有宽限期的 GC。共享内容的可访问性仍从 owner 推导，哈希本身不是访问凭证。
- 迁移期间新旧载荷可双读，禁止 ORM 属性隐式网络 IO；NPY 显式下载验证 hash/dtype/shape/unit。
  全量内容验证、恢复演练和回退窗口结束前保留旧内联载荷。
- 一并测量深分页、total 和高频筛选。有证据后实施 keyset pagination、索引精简/补充和
  计数策略，不预设分区、分库或缓存一定必要。大表新增索引评估并发创建和锁预算。
- 验收：迁移前后载荷保真、权限一致；上传/发布间崩溃可恢复；GC 不删仍被引用的对象；
  有实测的存储、WAL、备份恢复和查询收益，阈值及未外置理由写入记录。

## 4. 交付批次与依赖

| 批次 | 工作项 | 合并/切换门槛 |
| --- | --- | --- |
| A：结果与权限 | DB-00 最小 fixture、DB-01、DB-03、DB-02 的同分处理 | 授权矩阵和明确的错误结果反例全部通过；不等待模型全面迁移 |
| B：版本与完整性 | DB-04、DB-05、DB-02 完整选源、DB-08 | 历史/当前口径明确；错误归属被数据库拒绝；来源快照可复现 |
| C：写入与几何 | DB-06、DB-07 | 重建/增量一致；崩溃收敛；几何性质测试和新旧版本迁移通过 |
| D：容量与运维 | DB-09 | 代表性规模实测、内容保真、恢复和回退验证完成 |

批次表示交付顺序，不要求一次合并所有工作项。每个工作项独立提交可评审的代码、迁移、
测试与文档；所有负责人和 PR 在实施时登记，不虚构排期、归属或完成状态。

## 5. 数据迁移与回退规则

1. 只读审计并导出异常清单，确认备份和恢复演练。核验真实 head；新增 forward migration，
   不修改已发布迁移或沿用旧计划中“只有 baseline”的历史假设。
2. Expand：先加表/列、所需索引和兼容读写。FK/CHECK 可先 `NOT VALID`，然后处理旧行并
   `VALIDATE CONSTRAINT`；唯一约束/NOT NULL 按各自可用机制设计，不套用 `NOT VALID`。
3. Backfill：按稳定主键有界分批、记录 checkpoint 和失败原因，保证可重入；不能将执行
   backfill 本身视为数据正确。历史证据不足保持明确状态。
4. Verify：核对数量、来源 FK、数组哈希、有效 revision、授权集合以及新旧查询差异。
   合理差异（例如过滤掉私有来源）须有解释，不追求无条件数值相等。
5. Switch：按项目/功能逐步切换，记录 schema、策略、水位和监控证据。权限修复不得通过
   切回旧不安全全局汇总“回滚”；必要时关闭该派生功能并返回明确不可用状态。
6. Contract：兼容期和恢复演练通过后再移除旧读路径、触发器或载荷；涉及数据删除另列精确
   范围、备份和恢复方式。不能自动通过 destructive downgrade 回退 schema。

## 6. 验证与完成标准

实施时先运行对应工作项的回归测试，再执行仓库发布检查。数据库/对象存储测试必须显式指向
隔离基础设施；以下是全库完成时的验收命令，本轮已执行的子集和结果见上方实施记录：

```bash
uv run alembic heads
uv run alembic current
uv run alembic check
make lint
make type
make test
make test-db
make test-infra
```

`make test-infra` 开启数据库与对象存储测试；被跳过的 Redis 或其他专项不能据此记为通过。
DTO/transport 变更补 REST、GraphQL、MCP 契约验证；前端消费字段改变时补 `make frontend-check`、
`make frontend-build` 和相关 E2E。含迁移的 PR 需测试空库升级与上一受支持版本的数据升级，
同时检查 ORM、generated columns、索引和触发器一致；`alembic check` 不替代触发器行为测试。

性能验收至少记录数据分布、并发度、冷/热缓存、P50/P95/P99、超时/错误率、锁等待、事务时长、
吞吐、WAL 和刷新滞后。根据 DB-00 基线在性能改动前登记验收预算，不事后为迁就结果放宽指标。
源/恢复比对按[运维 Runbook](operations-runbook.md)执行，RTO/RPO 使用实测值。

每个工作项更新下列记录后才能标为 `done`；数据库无法连接不妨碍计划固化，但会阻止依赖真实
数据库的工作项被标为已验收：

| 字段 | 当前值/填写规则 |
| --- | --- |
| 工作项 / 状态 | DB-00 `todo`；DB-01～DB-03 `partial`（批次 A 最小修复）；DB-04 `todo`；DB-05 `partial`（历史项目归属隔离）；DB-06～DB-09 `todo` |
| 实施负责人 / PR / Git revision | 待实施时登记 |
| schema / policy / data snapshot | 待验证时登记 |
| 命令与结果 | 分别列 passed、failed、skipped、环境不可用及证据位置 |
| 回填与异常 | 扫描/迁移数量、失败清单、checkpoint、核验结果 |
| 安全与正确性 | 对应工作项的反例、跨项目矩阵与同父级约束测试 |
| 性能与恢复 | 涉及的实测指标、恢复演练及回退验证 |
| 文档 | 同步中英文当前契约；模型变化更新 ERD；保留本计划的历史证据 |
