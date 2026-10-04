# 提交日志：TS 立体化学规范化与逐帧解析诊断

- 日期：2026-10-04
- 基线提交：`a5abb32`（依赖安全更新）；沿用 `25800c7` 的显式氢和 TS 反应编号规则。
- 范围：TS/E/Z 规范化、分子属性持久化、来源映射审计、前端解析诊断、回归夹具和文档。

## 问题与修复

### 保留配位立体化学与源图电子态

- 保留非四面体配位中心的 `_chiralPermutation`。仅保留 ChiralTag 会丢失 SP/TB/OH 的具体排列，从而导致规范化后的参与物与带标签 TS 端点不一致。
- 在既有 `mol_atom_properties` JSONB 中按持久化原子下标记录 `chiral_permutation:N`，读取数据库 MOL 时恢复该属性；无需数据库 schema 迁移。
- SMILES 稳定化循环始终以源图的电子态及原子属性为依据，按 writer 输出顺序重排源图。无 sanitization 的重解析图仅用于验证表达及恢复控制关系，不再成为下一轮化学事实。
- TS 参与物投影采用 `removeHs=False`、`sanitize=False` 读取已验证的显式氢图，再统一恢复双键立体信息，避免默认去氢或再次 sanitization 改写芳香、价态和配位信息。

### 修复 E/Z 投影边界

- 确定性选择双键控制原子；更换单侧控制原子时同步翻转相对 E/Z，双侧同时更换时保持相对构型。
- 清理方向恢复在非立体双键、配位小环上产生的冗余标记，判断仍以源电子态和源图已有立体指定为准。
- 删除共轭共享方向中冗余的斜杠时，逐步验证全部源双键的物理控制关系；最终继续严格验证完整图、原子标签及 E/Z。
- 增加统一的 `recover_smiles_double_bond_stereochemistry`，处理 `119819.log` 的等价 TS 参与物表达在小环上恢复出不同冗余标记的问题。
- 沿用前体标准遍历与双端点联合消歧规则；不提高稳定化轮数上限，也不关闭立体校验。新增、翻转或丢失真实 E/Z，以及错误元素、同位素和 map 库存仍须拒绝。

### 按创建证据审计 TS 映射

- 审计已存 Geometry 映射与来源原子编号快照，再校验该映射下前后体的带标签完整图。
- 避免对对称图重新选择另一组合法标准编号后，仅因编号不同而误报。
- 回归继续拒绝来源向量篡改、破坏连通性的原子交换及相反立体构型。

### 在前端追溯失败原因到源文件帧

- 文件详情新增“解析诊断”，汇总解析版本的诊断、版本级错误、当前版本失败的 TS 推导，以及文件级错误。
- 展示失败阶段、错误码、具体原因、源文件帧号/段号和原始 JSON 证据；支持搜索、每页 20 条诊断和加载失败重试。
- TS 失败接口按每批 200 条完整读取；解析版本按 API 的旧到新排序取最后一条，修复超过 50 次重解析后显示过期版本的问题。
- 帧号在界面从 1 开始；按解析版本与源文件帧号匹配计算记录，直接打开帧详情。未成功入库的源帧保留编号与原因，并明确提示没有可查看记录；文件级错误不虚构帧号。
- 重解析进行中显示处理状态，完成后刷新版本和帧；异常诊断 JSON、接口失败不会被误显示为“没有错误”。
- 列表将诊断入口融入状态标签：失败为浅红色、部分成功为浅琥珀色，并带小箭头、悬停提示和键盘焦点。移除单独的“查看原因”文字链接，保持单行布局。

## 回归覆盖与提交前验证

新增真实 E/Z 图夹具来自 `109494.log`、`112574.log`、`119819.log`、`121567.log`、`131267.log`、`133279.log`。用例覆盖有/无 map、原子重排、源图不变、电子态保留、严格拒绝错误立体指定，以及 SP/TB/OH 排列保留和异构体区分。

以下为最终待提交代码重新执行的检查：

| 检查 | 结果 |
| --- | --- |
| 完整后端单元测试 `pytest -q tests/unit` | 785 passed，1 warning，335.10s；退出码 0 |
| 数据库 `tests/integration/test_mol_atom_properties.py` | 5 passed；临时表/事务回滚，覆盖金属自旋、快速入库及 SP/TB/OH 排列与别名查询 |
| `npm --prefix frontend run test:unit` | 5 passed，0 failed，0 skipped |
| `npm --prefix frontend run build` | vue-tsc 和 Vite 构建通过 |
| `ruff check src tests migrations scripts` | All checks passed |
| `ruff format --check src tests migrations scripts` | 382 files already formatted |
| 修改的 4 个 Python 源码/脚本的 mypy | Success: no issues found in 4 source files |
| 同一组文件的 Pyright | 0 errors, 0 warnings |
| `git diff --check` | 通过 |

后端单元测试从 `/tmp` 执行绝对路径的测试目录，以隔离部署 `.env`；数据库往返测试使用现有 PostgreSQL/RDKit 容器，凭据仅在进程环境中传递。mypy/Pyright 的范围为 `normalization.py`、`mol_properties.py`、`reaction_geometry_reconciliation.py` 和 `audit_mapped_reaction_atom_mappings.py`。后端仅有 FastAPI/Starlette 的 HTTP 422 常量弃用警告；前端构建仍有已有的 bundle 大小提示，均不影响成功退出。

本轮此前已通过 agent-browser 对实际 Vue 页面进行隔离 API 样本验证：第 61 个解析版本、202 条诊断跨接口分页完整加载、缺失第 79 帧无错误链接、正确打开当前版本第 98 帧、搜索和原始证据展开、无解析版本的文件级失败，以及手机布局。状态标签调整后再次验证其 24px 单行布局、键盘聚焦和点击跳转。浏览器使用本机样本接口，没有调整生产文件访问权限；这不代表已用登录会话逐一检查所有项目内文件。未在本轮运行完整 Playwright 或全部集成测试。

## 已执行的部署与实际重解析

- API、upload-worker、profile-refresh-worker、units-ts-dataset-worker 已重建并运行 `tricycle-reaction-database-api:main-a5abb32-ezfix2-20261004`。
- 前端最终运行 `tricycle-reaction-database-frontend:main-a5abb32-diagstatus-20261004`，容器健康，代理 `/health/ready` 返回 `status=ok`。
- 镜像标签含本次提交前的基线 SHA 和修复后缀，镜像中包含这次尚未提交时的源码改动。
- 前期 312 个 TS 失败文件已从原始对象实际重解析，19,984 帧、312 个 TS 均成功。
- E/Z 修复后的 6 个代表文件共 1,023 帧实际重解析成功，文件状态均为 succeeded/complete；`119819.log` 的 TS 推导和映射绑定成功。该批验收时 `stereo_projection_failed` 计数为 0，这是当时的快照结果。
- 全量 241,309 个可用计算文件已重新入队，常驻 worker 持续处理；本轮前端更新未重启或重置解析队列，保留现有数据卷。

## 未完成事项与验收边界

测试通过不等同于存量数据全部修复。以下记录来自后台运行快照，必须保留在后续验收中：

- 2026-10-04 15:21:24（Asia/Shanghai）监控：succeeded 15,375，partial 23，failed 19，processing 256，pending 225,636；队列尚未排空。
- 同一快照有 6 条 TS 持久化失败：5 条 `reused topology is not the same labelled stereochemical graph`，1 条 `reaction creation witness disagrees with labelled TS endpoints`，仍需追溯处理。
- 后续较大只读审计快照覆盖 7,312 个反应和 8,290 条 TS 几何映射：1 个反应不合法、5 条 TS 映射不合法，另有 44 条参与物表达需要规范化；共 50 个 finding。14 条端点映射均合法。
- 早期 392 个反应/412 条 TS 映射的零发现快照不能替代上述较大审计。全量队列完成后仍需重新审计与验收。

## 提交内容与排除项

提交业务源码、前端组件、回归测试和六个 JSON 夹具、规范化说明及本日志。`.env`、连接凭据、数据库备份、原始运行日志、浏览器样本服务、截图和 `.tmp` 验收输出均不纳入 Git。此操作创建本地提交，不执行远端推送。
