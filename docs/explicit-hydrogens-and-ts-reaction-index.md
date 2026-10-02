# 显式氢与 TS 反应索引重构

## 数据契约和文件职责

| 层 | 文件（相对 `src/tricycle_reaction_db/`） | 职责 |
| --- | --- | --- |
| 原子完整性 | `domain/explicit_hydrogens.py` | 检查隐式氢和 atom explicit-H count；氢必须是独立原子节点，`allHsExplicit=True` 本身不满足该要求 |
| 解析与标准化 | `ingestion/molop.py`、`ingestion/normalization.py` | 比较原始计算帧的元素序列；可信、回退、普通图统一接受完整原子库存检查；几何数据禁止凭空补氢坐标 |
| 持久化边界 | `application/dtos/chemistry.py`、`application/services/molecular_geometry.py`、`db/types/annotated_mol.py` | DTO、批处理/缓存路径、ORM MOL 写入均校验；MOL 使用 binary pickle，读取不去氢 |
| 标准反应身份 | `application/services/canonical_reaction_identity.py` | 接收前后体已确认的原子对应关系；输出标准 mapped RXN SMILES 和输入 map 到标准 map 的置换 |
| TS 创建 | `application/services/reaction_commands.py`、`application/services/artifact_uploads.py` | 参与者拓扑正常标准化；标准 SMILES 的 SHA256 作为 mapping hash；TS 原始端点及计算数组保留原始顺序 |
| 其他写入入口 | `application/services/reaction_mapping_resolution.py`、`dev/seed_da_bench.py`、`application/services/reactions.py` | 自动扩展和基准导入也生成标准索引；持久化入口拒绝非标准编号 |
| 映射转换 | `application/services/canonical_atom_mapping.py`、`application/services/reaction_geometry_reconciliation.py` | 对两个端点的完整映射图同时验证，组合 source→Geometry→reaction；不能仅凭元素或单原子对称类判定对应关系 |
| 导出 | `application/services/mapped_calculation_order.py`、`application/services/mapped_reaction_geometry_export.py` | 统一转换全部受支持原子轴、索引和矩阵轴；JSONL schema 升级为 v3 |
| 存量审计 | `scripts/audit_explicit_hydrogens_and_reaction_identity.py` | 只读审计，产生问题报告与原始文件重建清单 |
| 存量重建 | `scripts/reparse_overlapping_artifacts.py --candidate-manifest …` | 复用原有清理/重解析流程、不可变源文件和可恢复 checkpoint |

## 标准化过程

1. 前后体仍在同一 TS 原始原子序下；临时 map 为原始下标加一。元素、同位素和两侧原子库存必须守恒。
2. 清除临时 map 对标准化排序的影响。组合前体全部片段，计算标准 isomeric SMILES 的原子遍历顺序。
3. 对前体的对称歧义，先以包含前后体连接关系及原子对应边的双层图确定等价遍历代表，再执行前体标准遍历。后体仅参与消歧；最终 map 号仍取前体标准遍历的 1-based 原子序。
4. 将同一置换应用到后体并标准序列化。map 不参与分子拓扑的身份。
5. 在反应 SMILES 解析边界，将斜杠方向解码为 BondStereo，再参与身份计算，避免 RDKit reaction parser 留下 direction-only 图导致 E/Z 信息丢失。
6. 持久化标准 mapped RXN SMILES；原始文件顺序、片段出现次序或已有 map 值不再直接充当索引。

`MappedReaction.mapped_reaction_smiles` 是本次流程实际选定的标准形式。
`normalization_metadata` 记录 `policy`、`rdkit_version` 与
`selected_mapped_reaction_smiles`。导入、映射扩展和种子数据入口将该次
`CanonicalReactionIdentity` 直接传给持久化层，避免再次解析并标准化已选定的字符串。
审计检查记录的标准形式、hash、原子库存与对应关系，不要求 RDKit round-trip 必须达到字符串不动点。
TS inference 的反应字符串快照读取实际关联的 MappedReaction，不再独立重新计算其字符串。

部署这些字段前需执行 schema revision `0061_reaction_normal_form`。历史记录的元数据保持
NULL，不通过 schema 迁移伪造其标准化过程。记录标准形式避免重复标准化覆盖已选代表，
但不证明不同输入一定不会选到其他等价代表；这类差异仍需通过完整端点映射验证后才能合并。

单独给前体标准排序无法唯一命名完全对称的氢。按前后体分别计算单原子对称类也不能证明整个反应的原子对应：例如对称六元环仅交换两个相邻原子，各个原子的 rooted identity 都相同，但整个环的对应已经改变。回归测试覆盖此反例。

## 通用反应—结构关联与来源映射

反应 map 编号属于具体关联，不是 Geometry 的固有属性。现有模型的所有权链为：

```text
MappedReaction → MappedReactionNode → MappedReactionNodeGeometry → Mapping
                                              ↓
                                           Geometry
```

`Mapping` 即 `MappedReactionNodeGeometryMapping`。其唯一键、查询键和缓存键均为
`mapped_reaction_node_geometry_id`，不得改成 `geometry_id`。同一 Geometry 在不同
mapped reaction、不同节点或不同参与物位置下可以拥有不同的映射向量。这一层同时适用于
TS、前体和后体中间体；单个中间体只覆盖该参与物的 map 子集，编号可以不连续。

- `TransitionStateInference` 已关联 `calculation_frame_id` 与 `mapped_reaction_id`。其 `inference_settings.source_atom_map_numbers[i]` 保存原始计算帧第 i 个原子的标准 reaction map，并记录 `reaction_index_policy` 和标准反应字符串。它是该次推断的来源快照，不能作为该 Geometry 在其他反应中的映射。
- `MappedReactionNodeGeometryMapping.geometry_atom_map_numbers[g]` 保存 Geometry 第 g 个原子在**所属 mapped reaction**中的 map，是该结构关联的权威变换。
- `CalculationFrame.observed_to_geometry_atom_indices[i]` 为原始计算帧第 i 个原子对应的 Geometry 下标，与具体反应无关。
- 对已绑定的 TS：`source_atom_map_numbers[i] == geometry_atom_map_numbers[observed_to_geometry_atom_indices[i]]`。

多个原始计算帧复用同一 Geometry 时，只有在同一个反应节点—结构关联内才可以复用关联映射，并且必须验证当前原始前后体在该映射下均与目标反应的**带标签完整图**一致。随后给每个 inference 保存各自的原始序映射。仅元素一致不能作为复用依据。

同一关联的映射写入和缓存复用要求向量逐项一致，对 TS 和中间体使用相同规则。
对称原子交换后即使 mapped SMILES 相同，也不能据此认定坐标及科学属性的变换相同。
不同关联之间不比较向量是否相等，更不能因为共用 Geometry 而覆盖彼此的映射。

Geometry 自身保持现有标准序；原始 QM 数据保留在 CalculationFrame 中。导出不会修改这些数据库事实。

## 导出规则

标准输出的 map n 对应数组下标 n−1。原子标量/力重排原子轴；normal modes 重排第二轴；Hessian 同时重排两个 3N 轴；键级同时重排两个 N 轴。NMR coupling 的子集索引及矩阵行列一起转换，shielding 的 atom_index 一起转换。

偶极矩、分子级张量、轨道能量等无原子轴的数组不能因形状恰巧等于 N 而重排。轨道系数保持原有轨道/基函数轴，不能把基函数下标当作原子下标。源 metadata 的 axis_order 会被输出轴描述替换。

`value.geometry` 与 Mol block 使用标准 Geometry 的笛卡尔参考系；`value.calculations` 中的原始坐标和科学数组共同保留 source Cartesian 参考系，只转换原子顺序。若要统一旋转参考系，需进一步显式执行向量/张量旋转，不能只旋转坐标。

## 存量处理

全量反应更新和旧记录清理使用 `scripts/rebuild_mapped_reactions.py`，
参见 [全量更新脚本说明](rebuild-mapped-reactions.md)。该入口提供范围快照、数据库续跑标记、
逐 TS 事务和独立清理阶段；下面的旧维护入口仍用于单项检查和缺氢文件重解析。

新写入入口的标准化不会自动更新旧数据。旧 TS 原始序编号的 mapped reaction 必须全部
纳入审计；个别旧编号可能恰好等于标准编号，不能只凭创建时间或策略缺失判定字符串一定变化。

迁移必须从原始 TS 计算帧重新推导前后体，所有旧 TS 都走该流程；不能通过重新标准化旧
reaction SMILES 或组合旧 map 向量代替重推导。审计报告中的 `reaction_reindex_candidates`
与 `reaction_merge_groups` 仅是基于旧字符串的诊断线索，不是迁移输入。即使旧字符串碰巧
等于本次标准化输出，缺少来源重推导证据的记录也必须重建。报告按 project 执行。

现有 `scripts/backfill_transition_state_endpoints.py --reinfer-all --replace` 入口从不可变
计算文件定位原始帧，通过 `infer_transition_states_from_calculation_output → _infer_ts_frame`
重新推导两侧，再调用与首次导入相同的 `_prepare_inference_topology_records`、
`_resolve_and_bind_transition_state_reaction` 和端点持久化路径。它复用既有 CalculationFrame
和 Geometry，重新建立反应关联与来源映射；若原 Geometry 已缺氢，须使用后面的全文件重解析。
历史成功 TS 无法重现时保留原证据并报告失败，不将其静默改成失败推断或删除其旧反应。

该入口的 `--dry-run` 会在数据库 savepoint 内执行迁移路径并回滚，属于数据库预演，并非纯只读审计。
它尚未经过本次数据库集成验收，也不是完整的分组 checkpoint 迁移器；受其他来源或人工路径引用的
旧反应仍需单独核对引用与清理范围。不能仅因 TS 重新关联成功就宣布全库存量迁移完成。

合并时保留各自的来源、节点和几何关联证据；原子库存不完整或关联向量无法验证的记录走源文件
重建流程。TS 科学数组保持原始序，导出时使用迁移后的关联转换。修复后需重新审计并验证重复运行无变更。

数据库中已经丢失的 QM 氢坐标不能从重原子图可靠恢复。修复必须重解析不可变计算文件；拓扑查询入口可在无几何事实时显式 `AddHs`，QM 入口必须拒绝不完整数据。

先进行只读审计（输出路径必须不存在）：

```sh
uv run python scripts/audit_explicit_hydrogens_and_reaction_identity.py \
  --project-id PROJECT_UUID \
  --report .tmp/chemistry-audit.json \
  --reparse-manifest .tmp/chemistry-reparse-manifest.jsonl
```

核对报告后，预览重建范围：

```sh
uv run python scripts/reparse_overlapping_artifacts.py \
  --candidate-manifest .tmp/chemistry-reparse-manifest.jsonl \
  --state-file .tmp/chemistry-reparse-state.jsonl --dry-run
```

实际重建使用同一命令去掉 `--dry-run`。该流程会先清理清单内旧解析产物，再从不可变源文件重解析；操作及失败状态进入 checkpoint。断点恢复使用同一清单及 state-file，不得混用其他清单。

重建完成必须重新审计。没有原始文件、手动创建或仍被其他对象引用的坏记录不会被此流程猜测修复；报告中的剩余行需要单独处理。旧的无引用拓扑也可能保留，不能将“文件重解析成功”视为“全库已清零”。审计目前检测隐含/计数氢、图与拓扑原子数不符及非标准反应索引；对于连氢库存证据也丢失的图，必须回到原始源文件核验。

2026-09-30 连接排查：宿主机访问 `192.168.50.29:5433` 仍返回 `No route to host`，
但 `reaction-database-compute` 的应用容器可以连接。按部署技能，对 PostgreSQL/RustFS
配置中的具体主机添加命令级 `NO_PROXY`/`no_proxy`，保留原有条目；使用两个 Compose 文件、
显式 project name 和 `run --rm --no-deps` 临时应用容器挂载当前源码执行测试，无需启动数据服务。

已在远端独立临时 schema 中通过 6 项 RDKit/坐标数组往返测试，以及 7 项反应映射集成测试，
测试 schema 均已清理。反应映射测试使用当前 ORM 建表，不包含迁移脚本和完整 trigger 的验收。

同日已完成业务库 schema 升级：从 Git stash 恢复已经部署的
`0060_coordinate_complete_mol` 历史迁移（数据库字段及约束与原迁移一致），将新增字段的
revision 接为 `0061_reaction_normal_form`，形成唯一 head。ORM 保留历史的
`coordinate_complete_mol` / `coordinate_complete_mol_format` 字段及原约束，不将其用作反应映射。

通过挂载工作区源码的临时计算容器执行 Alembic upgrade，远端版本现为
`0061_reaction_normal_form`。实际 DDL 仅新增可空 JSONB `mapped_reaction.normalization_metadata`，
并更新版本记录；未降级、重置版本或删除历史字段。已核验历史字段及约束保持不变，重复升级无待执行项。
这次 schema 升级没有部署新版应用，也没有执行 TS 存量重推导；历史记录仍然需要按上述来源流程迁移。
