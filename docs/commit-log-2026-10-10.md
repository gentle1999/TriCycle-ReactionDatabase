# 提交日志：来源导入保留完整功能并移除全图搜索回退

- 日期：2026-10-10（北京时间）
- 源码基线：上游 `main` 的 `3bc86911b2eb9675b9eaac565259d1e84e5f9282`。
- 提交主题：`fix: preserve source import expansion without graph searches`。

## 问题与结果

导入 Gaussian 日志时，来源原子序号应作为权威映射证据。9 月 25 日的修复已要求
来源导入跳过拓扑和反应图匹配；更新到 `3bc8691` 后，协调阶段重新执行具体反应
扩展。反向查找逻辑参与物时，缺少 DAG witness 的候选再次落入 RDKit 全图搜索，
导致 75 原子分子的四个日志文件触发 5 秒图匹配超时。

实际失败调用链：

```text
reconcile_molop_geometry_context
  -> ensure_mapped_reactions_for_concrete_topology
  -> ensure_concrete_topology_memberships
  -> logical_participant_matches_for_concrete_topology
  -> find_topology_matches
  -> get_substruct_matches
  -> MolecularGraphMatchTimeoutError
```

本次修复保留具体映射扩展、前后体先到/后到、TS 证据继承和来源原子编号。
来源权威导入通过已验证的 DAG、规范排列及线性立体验证获取派生对应，不再回退
到 RDKit 全图/子结构搜索。普通非来源权威调用仍保留原有匹配语义。

## 实现

1. `reaction_topology_membership.py` 的正向成员持久化与反向参与物查询共用
   `_topology_mapping_evidence`：优先组合立体抽象 DAG witness；来源权威或
   legacy bulk 模式下，缺失 witness 时使用规范原子排列的完整双射验证，或
   规范无立体排列加逐原子、逐键的保留立体约束验证。
2. 规范对应只作为派生关系的证据，不替换历史来源标签、不声明枚举全部对称匹配。
   持久化 metadata 区分 `stereo_abstraction_dag`、`canonical_atom_order`、
   `canonical_abstraction`，并保留 witness 与未枚举全部候选的事实。
3. 无法验证完整对应、元素/同位素或保留立体约束时不接受该候选；来源路径不会
   调用全图搜索来寻找另一个排列，也不猜测 atom mapping。
4. 线性立体抽象验证比较实际隐式氢数量，替代 `NoImplicit` 控制标志的相等检查。
   显式氢完整的两个图不会因解析器标志差异丢失关系；实际隐式氢、电子字段、
   键连接、键类型和保留立体约束仍需一致。
5. 中英文开发指南记录来源导入在协调、具体反应扩展和 TS 继承阶段的无搜索约束。
   本次未改变数据库 schema 或几何/科学数组导出格式。

## 回归验证

- 相关单元测试：94 passed，覆盖成员关系、来源权威协调、立体抽象、反应记录和
  规范原子对应。新增覆盖包含来源/legacy 两种模式、非恒等原子重排、严格构型
  与抽象构型、相反手性、同位素差异、连接差异及已有 DAG witness 的优先复用。
- `tests/integration/test_concrete_reaction_mapping.py`：15 passed，15.98 秒。
  来源导入与 TS 继承测试把各 RDKit 搜索入口替换为直接失败的钩子，仍验证
  三种观测到的 imine 构型、前后体到达顺序、来源编号、TS 坐标和端点不变。
  使用数据库事务回滚，测试数据未作为业务导入保留。
- 上传、批次和 worker 扩展验证首轮 76 passed、4 failed。四项 HTTP 单元测试
  读取部署认证配置后返回 401；用测试所需的 `TRICYCLE_AUTH_MODE=development`
  单独重跑这四项，4 passed、6 deselected。线上认证配置未改变。此结果不等于
  宣称首轮 80 项全部通过或本次执行了完整后端测试套件。
- 两个变更源码文件的 Mypy 通过；四个变更 Python 文件的 Ruff 检查、格式检查及
  `git diff --check` 通过。

## 真实失败文件验证

在修复镜像的独立验证进程中，将 RDKit 全图/子结构搜索入口替换为直接失败的钩子，
通过真实 worker 的解析与持久化业务入口重试四个超时文件。四项全部成功，验证
进程退出码为 0；原始对象身份保留，成功结果写入数据库并完成队列确认。

| 文件 | 成功保留的帧 | TS 帧 |
| --- | ---: | ---: |
| `1401.log` | 572 | 1 |
| `51214.log` | 70 | 1 |
| `51230.log` | 43 | 1 |
| `51233.log` | 125 | 0 |
| 合计 | 810 | 3 |

## 本次部署与导入操作

- 工作区 `main` 从 `885f5b2` 快进至上游 `3bc8691`。先部署上游版本，再部署本次
  修复；沿用 `reaction-database-public` 项目和原 Compose 覆盖顺序，保留数据卷。
- 当前修复镜像为
  `tricycle-reaction-database-api:main-3bc8691-source-correspondence-20261010`。
  API、upload-worker、profile-refresh-worker 与 units-ts-dataset-worker 均使用
  该镜像；前端保留 `main-3bc8691`。镜像标签含提交前基线 SHA，不代表本次提交后的
  Git SHA。迁移/bootstrap 退出码为 0，HTTPS 就绪接口返回 `status=ok`。
- 在现有组织内通过项目管理业务服务创建 `new_ts_data`（slug `new-ts-data`），
  项目 UUID 为 `01a1258f-89d2-7911-8686-a1ebb12c285a`。
- 导入 `/home/UniTSdb/ts_data/new_tsdata/*.log`：19,812 个文件，78,002,301,526
  字节。源目录只读挂载，使用逐文件 JSONL 检查点；文件入队阶段 19,812 staged、
  0 failed、0 filtered，进程退出码为 0。逐项核对数据库文件名、大小、SHA-256 和
  `storage_status=available` 与本批次源清单一致。
- 首轮导入使用了错误的管理员 UUID，被权限检查拒绝，未暂存文件；修正后用同一
  检查点重试，全部入队。解析遇到图匹配超时后暂停消费，文件暂存继续；本次修复
  和真实文件验证完成后恢复解析。
- 应用户要求将 `TRICYCLE_MOLOP_BATCH_N_JOBS` 从 16 调整到 32，同时更新 `.env`
  和当前本地 Compose 覆盖。仅重启 upload-worker；启动日志确认 MolOP 池为 32、
  RustFS 池为 8，OpenMP/OpenBLAS/MKL 各保持 1。重启自动恢复 52 项中断租约。

## 导入状态与证据边界

截至北京时间 2026-10-10 19:48:09，数据库队列快照如下：

| 队列状态 | 解析状态 | 文件数 |
| --- | --- | ---: |
| `failed` | `failed` | 1 |
| `processing` | `pending` | 91 |
| `staged` | `pending` | 19,394 |
| `succeeded` | `partial` | 2 |
| `succeeded` | `succeeded` | 324 |

原始文件上传与登记已完成，后台结构解析仍持续进行。本记录不宣称全批次解析完成，
也不把部分解析计作完整成功。`51746.log` 的失败码为 `no_calculation_frames`，
错误说明源文件没有 QM calculation frames 并被过滤；它不是 RDKit 图匹配超时。

本提交仅包含上述源码、测试、中英文说明及本记录。部署 `.env`、本地 `/tmp`
Compose 覆盖、数据库内容、原始日志和操作证据不纳入 Git。检查点、文件清单、
状态查询脚本、暂存摘要及四个真实文件验证结果保存在本机
`.tmp/new-ts-data-import-20261010/`。未执行远端推送或远端 CI。
