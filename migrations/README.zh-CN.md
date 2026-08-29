# 数据库迁移

[English](README.md) | [简体中文](README.zh-CN.md)

所有数据库 schema 变更均由 Alembic 管理。执行迁移：

```bash
uv run alembic upgrade head
```

不要在应用启动代码或生产代码中使用 `SQLModel.metadata.create_all()`。

主分支从一条经审查的 baseline revision 开始：

- `0001_initial_schema`：当前 v1 PostgreSQL/RDKit schema、extension、function、index、
  constraint 和 generated column；不创建用户、组织、项目或角色。

该 snapshot 从 PostgreSQL 18.3、RDKit 4.8.0、pg_trgm 1.6 上已经验证的开发 head
`20260819_0053` 获取。它是 schema snapshot，不是开发数据库内容 dump。

产生该 baseline 的开发期 migration chain 有意不纳入 Alembic script directory。部署空数据库
不需要它，也不应把它重新引入生产 migration history。

baseline SQL 从 schema-only snapshot 生成，刻意不含任何 artifact、calculation 或 fixture row。
初始化后通过普通 upload/seed 流程重新导入这些记录。

迁移新数据库后必须显式执行 bootstrap：

```bash
# 仅本地开发：固定 test identity 和可配置的占位容器。
uv run tricycle-bootstrap --mode development

# 生产：要求 production OIDC 和全部 TRICYCLE_BOOTSTRAP_* 值。
uv run tricycle-bootstrap --mode production
```

两种模式均幂等，并写入 `deployment.bootstrap` audit event。production 环境拒绝 development
bootstrap，非 production 环境拒绝 production bootstrap。生产命令只向明确配置的 OIDC identity
授予 owner/manager，不向 system service account 赋予项目访问权。

`0001_initial_schema` 仅支持 upgrade。回滚部署时恢复数据库 backup 或重建数据库；不要对生产
数据库执行 destructive downgrade。
