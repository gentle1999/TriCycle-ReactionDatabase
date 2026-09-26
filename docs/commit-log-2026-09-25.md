# 提交日志：来源原子映射与上传 worker 修复

- 日期：2026-09-25
- 覆盖提交：`origin/main..HEAD` 中的 7 个提交（截至 `189a3d6`）
- 提交主题：保留来源原子映射权威，避免导入时执行拓扑或反应图匹配，并修复 worker 清理阶段的租约心跳行为。

## 背景

映射审计要求几何导出的原子编号与 mapped reaction SMILES 中的 atom mapping 保持守恒。对于从原始计算文件导入的数据，前后体及 TS 帧共享原子序号，来源映射应作为权威证据贯穿解析、协调与持久化，不能被后续的拓扑推断或反应匹配覆盖。

## 主要变更

1. **保留来源映射**：在几何 reconciliation 和回滚恢复路径中保留源文件原子顺序映射，避免推断结果取代来源证据。
2. **跳过不必要的图匹配**：对来源映射导入跳过拓扑匹配和 reaction 匹配步骤，降低不必要的计算并避免重排原子映射。
3. **撤回 Gaussian 可选字段容错改动**：`0b16ddd` 曾增加可选字段容错，随后 `d86936e` 将该改动完整撤回；最终代码不包含这项容错行为。
4. **修复 worker 清理阶段的租约处理**：解析清理期间不再执行租约 heartbeat，避免清理与租约续期交叠。
5. **补充回归覆盖**：更新 artifact upload、source-authoritative reconciliation、upload batch 和 upload worker 单元测试。

## 覆盖提交

| 提交 | 主题 |
| --- | --- |
| `c36ae94` | `fix: preserve source mapping through reconciliation` |
| `1ab73e6` | `fix: skip topology matching for source mappings` |
| `79a5666` | `fix: skip reaction matching for source imports` |
| `25d2291` | `fix: restore source authority after inference rollback` |
| `0b16ddd` | `fix: tolerate malformed Gaussian optional fields`（后续已撤回） |
| `d86936e` | `Revert "fix: tolerate malformed Gaussian optional fields"` |
| `189a3d6` | `fix: avoid lease heartbeat during parse cleanup` |

## 验证与范围

- 本日志记录的是上述代码提交；实时数据库导入及其结果不属于 Git 提交内容。
- 本次仅整理并提交日志，没有重新运行测试；测试结果应以各提交对应的 CI 或独立验证记录为准。
