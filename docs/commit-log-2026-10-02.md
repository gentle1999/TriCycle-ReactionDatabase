# 提交日志：显式氢、规范反应索引与 TS 关联映射重构

- 日期：2026-10-02
- 基线提交：`b577f78`（`fix: require exact topology for thermodynamic profiles`）。
- 范围：基线之后本次提交中的后端、数据库迁移、前端、维护脚本、文档及测试变更。
- 本日志随实现一并提交；包含它的提交即为本次变更提交。此前来源映射工作参见 [2026-09-25 日志](commit-log-2026-09-25.md)。

## 一、目标与数据契约

1. QM 分子图必须保留全部氢原子节点，并与真实坐标中的原子库存一致。已经缺失的氢坐标必须从原始文件重建，不能凭空生成。
2. TS 的原始前后体保持逐原子对应，以前体的标准遍历顺序生成整个反应的 1-based map 号；后体使用同一变换。
3. 相同反应的原子重排或 map 重编号应复用同一规范反应身份；不同原子对应关系及真实立体异构体仍须区分。
4. Geometry 保留自身原子序。到具体 mapped reaction 的变换属于反应节点—几何关联，同一 Geometry 在不同反应下可以具有不同变换。
5. TS 导出中的坐标和所有具有原子序的科学属性使用一致的排列。独立前后体分子的几何不会被拼装为完整反应几何。
6. 保存实际选定的规范形式及来源映射见证，不假设 RDKit 反复标准化必然达到字符串不动点。

## 二、实现变更与文件职责

### 1. 显式氢与完整分子存储

- 新增 `domain/explicit_hydrogens.py`，统一检查隐式氢、原子上的氢计数及独立氢节点契约。
- `ingestion/molop.py`、`ingestion/normalization.py` 和化学 DTO 对来源原子库存及完整分子图实施校验。
- `molecular_geometry.py` 与 `db/types/annotated_mol.py` 在普通、复用及持久化边界拒绝不完整 QM 分子。
- `MolecularTopology` 增加带原子序的完整分子二进制字段；配合现有 RDKit 查询 MOL 保留坐标对应关系和原子属性。

### 2. 规范 mapped reaction 身份

- 新增 `canonical_reaction_identity.py`：联合前后体连接关系及对应边消除前体对称歧义，再以前体标准遍历生成最终 map 号。
- 新增 `canonical_atom_mapping.py`：通过标准遍历提出置换，再用带唯一标签的完整图验证，不能仅凭元素序列或局部原子对称类认定映射相同。
- `reaction_commands.py`、`reactions.py`、`reaction_mapping_resolution.py` 和 `dev/seed_da_bench.py` 接入同一身份生成流程；以标准 reaction SMILES 的 SHA256 作为 mapping hash。
- `MappedReaction.normalization_metadata` 保存策略、RDKit 版本及实际选定的标准 SMILES；写入路径传递本次选定身份，避免重复标准化改变代表形式。
- 原始反应 mapping 在拓扑解析、复用和持久化前检查完整性、每侧唯一性及双侧集合一致性；混合有映射/无映射组件也会被拒绝，避免误报立体结构不一致。

### 3. 关联映射与立体结构复用

- `reaction_geometry_reconciliation.py` 将来源帧→Geometry→reaction 的置换组合到 `MappedReactionNodeGeometryMapping`，并验证两个端点的完整带标签图。
- 同一关联的映射必须逐项一致；缓存与查询按关联身份维护，不能只按 Geometry ID 复用。
- TS inference 保存实际关联反应的字符串快照及 `source_atom_map_numbers`；创建时的映射见证贯穿绑定流程。
- 拓扑复用比较采用与持久化一致的立体序列化方式，清除重排后失效的 RDKit 环立体缓存，并投影已确定的双键立体信息。
- 上述修复保留反应索引原有遍历策略和严格立体校验，不将真正的 E/Z 或对映异构体合并。
- 重新推导成功状态与必需外键一起赋值，避免恢复脚本在补齐关联前因查询自动 flush 触发约束失败。

### 4. 前后体关联与 profile 一致性

- 普通及批量解析入口均执行已有前后体几何的反向关联，修复来源原子序权威模式下遗漏关联的问题。
- 单条和批量 profile 物化使用同一端点几何快照补齐关联，并在同一事务提交。
- 关联资格仍要求同项目、具体拓扑一致、符合优化/热力学/虚频条件，不降低化学身份要求。
- 新增 `backfill_reaction_endpoint_geometries.py`，支持默认统计和有界批量补关联、验证映射及标记 profile 待刷新。

### 5. TS 坐标及科学属性导出

- 新增 `mapped_calculation_order.py`，统一处理原子索引和数组轴；`mapped_reaction_geometry_export.py` 输出 schema `mapped-reaction-ts-geometry-v3`。
- 覆盖坐标、力、normal modes、原子 population、键级、Fukui 等原子数组，Hessian 的两个 3N 轴，以及 NMR coupling 子集索引/矩阵和 shielding 原子索引。
- 无原子轴的分子向量、张量及轨道/基函数数据不因维度恰好相同而重排。
- 计算坐标和科学数组保持来源笛卡尔参考系，单独导出的标准 Geometry 保持其自身参考系；没有隐式旋转，也不修改数据库中的来源数组。
- 数组维度或置换无效时拒绝导出不一致数据；同步更新 UniTS 导出文档。

### 6. 原始文件页面与查询

- Artifact summary 增加 `latest_parse_at`，来自 ingestion 完成时间；列表和详情展示最新解析时间。
- 前端默认 `latest_parse_at desc`，支持表头排序及重新排队时的状态更新。
- 后端最新解析时间排序支持 NULL 最后及稳定 ID 次序；保留旧 cursor API 的默认排序契约。
- 修正排序引入外连接后 ingestion status EXISTS 的相关查询范围，解决对应筛选请求的 500 错误。
- profile 查询继续限定现行热力学策略；测试补充旧策略在统计和 CSV 中均不可见的验证。

## 三、数据库迁移与维护工具

| 文件 | 作用 |
| --- | --- |
| `0060_coordinate_complete_topology_mol.py` | 增加完整分子二进制及格式字段、成对约束；固定历史格式字面量 |
| `0061_reaction_normal_form.py` | 增加可空 `normalization_metadata`；旧记录保持未知，不伪造来源标准化证据 |
| `audit_explicit_hydrogens_and_reaction_identity.py` | 只读检查氢完整性、反应身份，产生原始文件重建清单 |
| `audit_mapped_reaction_atom_mappings.py` | 按完整反应及关联映射审计；修复已删除私有函数依赖，改用现行双端点身份流程 |
| `rebuild_mapped_reactions.py` | 从原始 TS 文件重新推导，固定范围快照、逐 TS 事务、数据库续跑标记、独立旧记录清理及结果报告 |
| `reset_all_parses.py` | 显式表清单、外键范围与不可变数据指纹检查后清除解析产物，将可用计算文件重新排队 |
| `backfill_reaction_endpoint_geometries.py` | 有界批量补齐合格前后体几何关联 |
| 原有 backfill/reparse 脚本 | 接入现行来源映射与标准化流程，支持重建清单和一致的 TS 快照 |

- 反应重建输入必须是原始计算帧，不以旧 reaction SMILES 再标准化代替重新推导。
- 无法追溯或不能验证的旧记录保留并报告；来源更新失败时不执行整体旧反应清理。
- 缺氢 Geometry 不能仅靠反应重索引修复，需要原始文件重解析。
- 全量解析重置是明确的数据操作；提交这些脚本不会自动执行重置或重新排队。
- 详细规则参见 [显式氢与索引设计](explicit-hydrogens-and-ts-reaction-index.md)、[反应重建说明](rebuild-mapped-reactions.md)。

## 四、测试修复与新增覆盖

- 更新旧 TS 原始序映射断言，以实际选定形式、来源映射见证及几何变换组合为依据；验证原始端点应用映射后的完整图。
- 更新等价重编号应复用反应、延迟协调保留已有绑定、显式氢和现行 profile 策略夹具，保留不同对应关系和立体异构体的区分断言。
- 前端检查默认最新解析时间降序及真实列表请求参数；指标栏 `limit=1` 请求单独排除。
- 两项解析清理测试自行解析真实 TS fixture，并在外层事务整体回滚，不再依赖现存数据库数据。
- 四项迁移控制流程测试自行创建隔离 schema，将脚本的引擎限定到该 schema，并在结束后删除，不再依赖额外临时启动器。
- 新增 14 项单元回归及 1 项数据库回归，覆盖无效 mapping 提前拒绝、map/原子重排等价、不同对应关系、立体异构体、非守恒标签及损坏 TS 关联映射。
- 修复脚本包识别、SQLModel 列表达式、可空类型、缺少函数标注等静态检查错误；第三方缺失签名仅采用局部注释，没有关闭全局类型检查。

## 五、已完成验证

| 验证范围 | 结果 |
| --- | --- |
| 启用 PostgreSQL、RustFS、Redis 的单次完整后端运行 | **912 passed，0 failed，0 skipped** |
| 随后新增的真实 TS 入库审计回归 | **1 passed**；当前收集总数为 **913**，全部已验证 |
| 完整 Playwright | **58 passed**；补强排序请求断言后再次全量通过 |
| 前端类型检查及构建 | 通过 |
| Ruff lint 与 format | 通过 |
| mypy | 181 个源文件通过 |
| Pyright | 0 errors，0 warnings |
| Git diff whitespace 检查 | 通过 |
| 全新临时数据库迁移与 bootstrap | 迁移至 `0061_reaction_normal_form`，成功 |
| 审计脚本实际读取 DA seed | 成功执行；manifest TS 缺少推断来源时保留不可验证报告，不伪报 verified |

测试使用本机开发服务中的独立临时数据库、bucket 和 Redis，以及独立测试 API。临时资源均已清理。完整运行存在 SQLModel/Starlette 弃用提示，未作为测试失败处理。原始日志保存在本机 `.tmp/test-audit-20261002/`，不进入 Git；本节为可随仓库保留的验证摘要。

迁移脚本控制流程测试的隔离 schema 由 ORM metadata 建表；完整 Alembic 链另在新数据库验证。这些测试不等同于对生产全库重推导的完成证明。

## 六、历史运行记录与本次提交边界

此前同一轮工作中按用户授权执行过生产解析重置、重解析、前后体关联补齐和失败文件重新排队。以下是当时操作报告中的快照，不代表提交时的实时队列状态：

- 全量解析重置保留 242,119 条原始文件目录记录，将 188,261 个可用计算文件置为待解析。
- 前后体关联补齐累计处理 6,842 对关联，当时的独立缺失扫描为 0。
- 后续将 1,114 个失败/部分成功文件重新排队，分为 18 个上传批次；来源文件及来源原子序设置保留。
- 15 个真实失败 TS 来源的复用修复回放通过并回滚；E/Z 与对映异构体对照仍产生不同反应身份。

这些数据库操作、容器更新和原始对象内容不属于 Git 提交。本次测试修复及本次提交没有再次部署生产、执行生产迁移、清理业务数据或启动全量重解析；不将历史操作计数作为当前生产任务完成状态。
