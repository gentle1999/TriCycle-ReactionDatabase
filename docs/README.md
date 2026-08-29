# 文档索引 / Documentation Index

[English root README](../README.md) | [中文根 README](../README.zh-CN.md) | [English index](en/README.md)

本目录的中文页面和 `docs/en/` 中的英文页面成对维护。命令、环境变量、实体名、API 路径和
文件名保持原样；说明文字按对应语言表达。当前操作契约会随实现更新，标为“历史记录”的页面
保留其日期、当时状态与证据，不能替代现行部署或开发指南。

Chinese pages in this directory are paired with English pages under `docs/en/`.
Commands, environment variables, entity names, API paths, and filenames remain
literal. Current operating contracts track the implementation; pages labelled
as historical records retain their original dates and evidence and are not
current operating instructions.

## 当前契约 / Current Contracts

| 中文 | English | 用途 / Purpose |
| --- | --- | --- |
| [开发环境](development.md) | [Development environment](en/development.md) | 宿主机开发、测试与本地导入 / host development, testing, and local import |
| [部署与配置指南](deployment-configuration.md) | [Deployment and configuration](en/deployment-configuration.md) | 开发、单机和多主机部署 / development, single-host, and multi-host deployment |
| [数据模型与存储边界](data-model.md) | [Data model and storage boundaries](en/data-model.md) | 当前化学、文件、反应和查询契约 / chemistry, artifact, reaction, and query contracts |
| [业务模型](business-model.md) | [Business model](en/business-model.md) | 面向用户的对象、流程和非目标 / user-facing objects, workflows, and non-goals |
| [生产运维与恢复 Runbook](operations-runbook.md) | [Production operations and recovery runbook](en/operations-runbook.md) | 备份、恢复、监控与定时任务 / backup, recovery, monitoring, and scheduled work |
| [MolOP 计算结果导出需求](molop-export-requirements.md) | [MolOP calculation-result export requirements](en/molop-export-requirements.md) | 上游导出与 ingestion 契约 / upstream export and ingestion contract |
| [数据库实体关系图](database-erd.md) | [Database entity relationship diagram](en/database-erd.md) | 数据库边界、ERD 和完整性约束 / database boundaries, ERD, and integrity constraints |
| [RDKit Mol 对象数据库往返契约](rdkit-mol-roundtrip.md) | [RDKit Mol database round-trip contract](en/rdkit-mol-roundtrip.md) | RDKit binary Mol 的持久化边界 / RDKit binary Mol persistence boundary |

## 历史记录 / Historical Records

这些文档记录了特定日期的计划、决策、基线或验收状态。实现可能已经变化；在作出当前决策前，
优先使用上面的当前契约并查看代码和测试。

| 中文 | English | 记录类型 / Record type |
| --- | --- | --- |
| [技术方案与实施路线图](technical-roadmap.md) | [Technical roadmap](en/technical-roadmap.md) | 架构与实施路线图 / architecture and delivery roadmap |
| [实施目标清单](implementation-backlog.md) | [Implementation backlog](en/implementation-backlog.md) | 分阶段 backlog / phased backlog |
| [项目重构与上线计划](refactor-plan.md) | [Refactor and release plan](en/refactor-plan.md) | 重构与验收记录 / refactor and acceptance record |
| [前端重构计划](frontend-refactor-plan.md) | [Frontend refactor plan](en/frontend-refactor-plan.md) | 前端设计计划 / frontend planning record |
| [身份提供方决策记录](identity-provider-decision.md) | [Identity-provider decision record](en/identity-provider-decision.md) | OIDC/IDP 决策 / OIDC and IDP decision |
| [安全与查询重构基线](security-query-baseline.md) | [Security and query baseline](en/security-query-baseline.md) | 安全与性能基线 / security and performance baseline |
| [安全、查询与身份服务重构修复计划](security-query-identity-remediation-plan.md) | [Security, query, and identity remediation plan](en/security-query-identity-remediation-plan.md) | 修复计划与阶段证据 / remediation plan and evidence |

## 辅助 README / Supporting READMEs

| 位置 | 说明 |
| --- | --- |
| [迁移说明](../migrations/README.md) | Alembic baseline 与 bootstrap 规则；页面内中英文对照 |
| [DA benchmark fixture](../tests/fixtures/da_bench_minimal/README.md) / [English](../tests/fixtures/da_bench_minimal/README.en.md) | 固定 Gaussian fixture / fixed Gaussian fixture |
| [QM parser fixtures](../tests/fixtures/qm/README.md) / [中文](../tests/fixtures/qm/README.zh-CN.md) | 最小 ORCA parser fixture / minimal ORCA parser fixture |
| [部署验收记录模板](../infra/deployment/acceptance-record.example.md) / [中文](../infra/deployment/acceptance-record.example.zh-CN.md) | 生产证据模板 / production evidence template |
| [ChemDoodle vendor note](../frontend/public/vendor/chemdoodle/README.md) / [中文](../frontend/public/vendor/chemdoodle/README.zh-CN.md) | 受审计上游资源与本地中文说明 / audited upstream assets and local Chinese note |

第三方 vendor 文档保留上游英文原文，并提供单独的本地中文说明，不修改被审计的上游资源。
