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
