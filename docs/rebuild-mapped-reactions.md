# 全量重建 mapped reaction

入口：`scripts/rebuild_mapped_reactions.py`。要求数据库版本为
`0061_reaction_normal_form`。默认只预演；`--apply` 才提交更新和清理。

## 执行流程

1. 固定本次项目范围内的反应、TS 推断和来源文件清单，保存旧反应字符串、来源设置及关联映射快照。
2. 从对象存储读取原始计算文件并校验 SHA256，按原始 file frame index 定位 TS，重新推导前后体。
3. 复用首次导入的拓扑标准化、标准反应创建及 TS 绑定流程。直接保存该流程选定的标准形式，
   不把旧 reaction SMILES 当作重建输入，不要求重复 RDKit 标准化收敛。
4. 核验原始帧→Geometry→标准反应的组合映射与来源快照一致。每个 TS 独立事务提交，失败则回滚该项。
5. 仅当本次全部来源成功后清理旧反应：检查旧推断引用已经迁移、旧 TS Geometry 证据已覆盖，
   先删旧路径边，再由外键级联删除旧参与物、节点、关联映射和派生热力学记录；删除已空的旧逻辑反应。
6. 检查剩余旧版记录和运行期间新增的推断。只有全部完成才返回退出码 0；任何阻塞均返回 1。

不会删除原始文件、CalculationFrame、Geometry、拓扑或科学数组。它不修补缺失的 QM 氢坐标；
若旧 Geometry 已缺氢，该项会失败保留，需先使用原始文件全量重解析流程修复几何。

没有可追溯 TS 来源的旧记录会保留并报告，不因它没有 inference 引用就认定可删除。
自动扩展的 `other` / `mapping:*` 反应允许通过同一旧逻辑反应的 TS 来源核验后清理；
人工路径及未被重建覆盖的 TS 几何关联不会被自动清除。返回非零时不能视为“全库更新完成”。

## 命令

在可以直接访问后端的环境中：

```bash
python scripts/rebuild_mapped_reactions.py \
  --all-projects --state-file /state/reaction-rebuild.json --dry-run

python scripts/rebuild_mapped_reactions.py \
  --all-projects --state-file /state/reaction-rebuild.json --apply
```

单项目把 `--all-projects` 替换为 `--project-id PROJECT_UUID`。预演会实际调用持久化路径，
每个 TS 完成后回滚，因此不是纯只读扫描。它不删除旧反应，也不会把预演成功写成数据库续跑标记。

本机宿主网络无法直连数据库时，使用部署技能中的计算容器网络；保持业务容器和远端服务不变：

```bash
mkdir -p .tmp/reaction-rebuild

docker compose -f compose.yaml -f compose.compute.yaml \
  --project-name reaction-database-compute \
  run --rm --no-deps -T --entrypoint python \
  -e PYTHONPATH=/workspace/src \
  -v "$PWD:/workspace:ro" \
  -v "$PWD/.tmp/reaction-rebuild:/state" \
  -w /workspace api /workspace/scripts/rebuild_mapped_reactions.py \
  --all-projects --state-file /state/reaction-rebuild.json --dry-run
```

执行更新时，使用同一命令、同一状态文件，将 `--dry-run` 改为 `--apply`。
脚本从配置提取数据库和对象存储主机，仅为这些主机追加命令级 `NO_PROXY` 和 `no_proxy`。
不会输出连接串或凭据，也不会启动、修改远端数据服务。

## 续跑和报告

- `reaction-rebuild.json`：不可变范围快照、run ID、策略、数据库及 schema 指纹、内容摘要。
- `reaction-rebuild.events.jsonl`：追加式执行事件，每项写入后 fsync。
- `reaction-rebuild.report.json`：预演或执行结果，包括阻塞原因、新增推断和剩余旧反应 ID。
- `TransitionStateInference.inference_settings.mapped_reaction_rebuild`：与本次更新同事务提交的
  run ID、原始文件 SHA256、旧反应 ID 和策略，是续跑的权威完成标记。

重复使用相同参数和状态文件即可续跑；脚本会重新核验数据库标记及关联映射，不仅凭本地日志跳过。
所以数据库提交后、本地报告写入前中断也不会丢失完成状态。范围快照在首次执行时固定，不会静默扩展。
禁止把状态文件换到另一个数据库、schema、项目范围或算法版本。

存在来源失败时，已成功更新的 TS 保留，整个旧记录清理阶段推迟。解决问题后复用原状态文件继续。
源文件需被替换、出现新推断或计划范围变化时，保留原报告并用新状态文件启动新一轮。
执行期间应避免旧版导入器继续生成旧索引；脚本会报告并发新增记录，不会擅自停服务或删除快照外记录。

本脚本已增加范围校验、续跑标记、清理保护和隔离数据库控制流程测试。完整生产数据规模的原始文件
重推导尚未执行，预演和正式报告应作为实际验收依据。

## 补齐具体化反应继承的 TS 关联

`scripts/backfill_inherited_transition_states.py` 按同一项目和逻辑反应查找缺失的 TS
关联，仅继承当前策略下已验证的 TS 原子映射，并校验完整反应图的跨侧原子对应。
具体端点的立体标记差异不会阻止继承；原始 Geometry、计算帧和端点拓扑保持原样。
每个反应对独立事务提交，受影响的热力学 profile 入队刷新，重复执行跳过已有绑定。

```bash
python scripts/backfill_inherited_transition_states.py --project-id PROJECT_UUID
python scripts/backfill_inherited_transition_states.py --project-id PROJECT_UUID --apply
```

可追加 `--logical-reaction-id REACTION_UUID` 限定到单个逻辑反应。默认仅做只读校验；
执行结果中的 `remaining_pairs: 0` 表示候选关联已补齐。全项目范围使用
`--all-projects` 替代 `--project-id PROJECT_UUID`，每次继承仍严格限制在同一项目内。

解析完成后的统一协调步骤也会处理保留源原子顺序的导入：展开已验证的具体成员，
将当前策略已验证的 TS 证据传播给同一逻辑反应的具体映射。已有映射接收新 TS 时
同样执行传播，并保持源 TS 的原子映射与坐标不变。

## 补齐缺失的具体化 mapping

`scripts/backfill_concrete_reaction_mappings.py` 使用已保存的拓扑和反应原子映射模板，
调用正常导入的具体化扩展、TS 继承和前后体几何关联服务。先补全经验证的抽象关系，
再按同一项目内的实际具体成员展开；不会生成理论异构体或挂回源文件重解析。
立体原子对应可证明等价的旧拓扑序列化只作为一个扩展候选，优先使用已有 Geometry 的拓扑。

```bash
python scripts/backfill_concrete_reaction_mappings.py --all-projects
python scripts/backfill_concrete_reaction_mappings.py --all-projects --apply
```

默认逐逻辑反应预演后回滚，包含实际服务调用和数据库身份锁，并非纯只读统计。
`--apply` 逐反应独立提交，保留原始文件、解析修订、Geometry 和源原子映射，
并将受影响的 profile 入队刷新。单项目使用 `--project-id PROJECT_UUID`；可追加
`--logical-reaction-id REACTION_UUID` 或 `--limit N` 限定本轮范围。
失败项完整回滚并报告，任一失败返回非零；重复执行应报告 `created: 0`。

新导入在解析后的协调步骤中执行同样的发现与扩展。反应创建前已缓存的空具体成员集合
不能阻止读取本次新建的成员；反应路径节点也复用同一协调缓存，避免重复创建。

## 补齐前后体几何关联

若前后体几何先于 mapped reaction 导入，旧批量解析入口可能遗漏反向补关联。
`backfill_reaction_endpoint_geometries.py` 按具体拓扑、项目和既有几何资格规则
（优化收敛、有热力学属性、无虚频）筛选缺失关联，调用正常导入使用的关联与映射
校验服务，并将受影响的 profile 标记为待刷新。不修改原始文件、几何或 TS 映射。

```bash
# 默认只统计；每批独立事务，失败的批次回滚，重新执行会跳过已完成关联。
python scripts/backfill_reaction_endpoint_geometries.py
python scripts/backfill_reaction_endpoint_geometries.py --apply --batch-size 100
```

完成时输出 `remaining_pairs: 0`。运行期间可继续解析文件；新增反应由修复后的
解析入口查找已有前后体几何。脚本的重复运行也可用于检查仍然缺失的关联。

Profile 的单条和批量物化路径也会使用本次读取的前后体几何快照补关联，
并与 profile 在同一事务中提交，避免并发导入使几何在解析补关联步骤之后才变得
合格时，出现 profile 已引用几何而节点尚未关联的窗口。

## 修正 mapping 的全部计算耗时

`scripts/backfill_mapped_reaction_runtimes.py` 按 mapping 汇总所有已记录的前体、TS
和后体候选计算文件，包括未收敛或未被最低能量 profile 选中的候选。前后体使用同项目
具体参与物拓扑；TS 使用该 mapping 的几何绑定和推断来源。已退役文件不计入。
每阶段和总耗时分别按文件去重，重复解析使用来源中最新的修订；任一计入文件缺少耗时
时，相应字段为空。同一 mapping 的多个 profile 重复显示相同搜索耗时，不能再按行相加。

```bash
python scripts/backfill_mapped_reaction_runtimes.py --all-projects
python scripts/backfill_mapped_reaction_runtimes.py \
  --all-projects --apply --backup /state/runtime-backup.jsonl
```

默认只预演；单项目用 `--project-id PROJECT_UUID`，`--batch-size` 默认 100。
正式执行要求新的备份路径，每批锁定 mapping，先写入并同步原耗时及其他字段的摘要，
再仅更新四个耗时字段、核验其他字段未改变并提交。不会重新选择能量、重解析原始文件
或修改源几何。每批独立提交，中断后保留原备份并使用新的备份路径重跑；重复执行会
重新核算并报告仍需修正的记录数。容器执行时将备份目录挂载为可写的 `/state`。
