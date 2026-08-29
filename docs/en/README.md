# Documentation Index

[English root README](../../README.md) | [Chinese root README](../../README.zh-CN.md) | [中文索引](../README.md)

This directory is the English counterpart to the Chinese documentation in
`docs/`. Commands, environment variables, entity names, API paths, and
filenames are deliberately kept literal. The pages are grouped into current
operating contracts and dated historical records so that an old acceptance
snapshot is not mistaken for a current deployment instruction.

## Current Contracts

| English | 中文 | Purpose |
| --- | --- | --- |
| [Development environment](development.md) | [开发环境](../development.md) | Host development, testing, and local imports |
| [Deployment and configuration](deployment-configuration.md) | [部署与配置指南](../deployment-configuration.md) | Development, single-host, and multi-host deployment |
| [Data model and storage boundaries](data-model.md) | [数据模型与存储边界](../data-model.md) | Chemistry, artifact, reaction, and query contracts |
| [Business model](business-model.md) | [业务模型](../business-model.md) | User-facing objects, workflows, and non-goals |
| [Production operations and recovery runbook](operations-runbook.md) | [生产运维与恢复 Runbook](../operations-runbook.md) | Backup, recovery, monitoring, and scheduled work |
| [MolOP calculation-result export requirements](molop-export-requirements.md) | [MolOP 计算结果导出需求](../molop-export-requirements.md) | Upstream export and ingestion contract |
| [Database entity relationship diagram](database-erd.md) | [数据库实体关系图](../database-erd.md) | Database boundaries, ERD, and integrity constraints |
| [RDKit Mol database round-trip contract](rdkit-mol-roundtrip.md) | [RDKit Mol 对象数据库往返契约](../rdkit-mol-roundtrip.md) | RDKit binary Mol persistence boundary |

## Historical Records

These pages retain their dates, decisions, and evidence. Check the current
contracts and the code before relying on them for a new deployment or design.

| English | 中文 | Record type |
| --- | --- | --- |
| [Technical roadmap](technical-roadmap.md) | [技术方案与实施路线图](../technical-roadmap.md) | Architecture and delivery roadmap |
| [Implementation backlog](implementation-backlog.md) | [实施目标清单](../implementation-backlog.md) | Phased backlog |
| [Refactor and release plan](refactor-plan.md) | [项目重构与上线计划](../refactor-plan.md) | Refactor and acceptance record |
| [Frontend refactor plan](frontend-refactor-plan.md) | [前端重构计划](../frontend-refactor-plan.md) | Frontend planning record |
| [Identity-provider decision record](identity-provider-decision.md) | [身份提供方决策记录](../identity-provider-decision.md) | OIDC/IDP decision |
| [Security and query baseline](security-query-baseline.md) | [安全与查询重构基线](../security-query-baseline.md) | Security and performance baseline |
| [Security, query, and identity remediation plan](security-query-identity-remediation-plan.md) | [安全、查询与身份服务重构修复计划](../security-query-identity-remediation-plan.md) | Remediation plan and evidence |

Supporting READMEs in `migrations/`, `tests/fixtures/`, and `infra/deployment/`
have paired Chinese and English pages. Vendor files retain their upstream English
source and have separate local Chinese notes, leaving audited upstream resources
unmodified.
