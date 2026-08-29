# 前端重构计划

> English edition: [Frontend refactor plan](en/frontend-refactor-plan.md). This is a dated planning record.

> 状态：已接受
>
> 生效日期：2026-08-14
>
> 适用范围：`frontend/` 以及为前端提供会话、项目上下文、资源查询和项目管理契约的 API
>
> 文档职责：本文件是前端重构的执行计划。通用领域边界、数据库不变量和 API 总体原则仍以
> [技术方案与实施路线图](technical-roadmap.md) 和 [数据模型与存储边界](data-model.md) 为准。

## 1. 目标

将当前以 `App.vue` 为中心的单页浏览器重构为资源路由、服务端状态缓存和项目访问上下文驱动的应用。重构后必须满足：

- 用户、组织、项目和权限是全局业务上下文，不是上传控件的局部状态；
- Geometry、CalculationFrame、Artifact、LogicalReaction 和 MappedReaction 按各自领域职责展示；
- 查询、详情、上传和下载都遵守后端返回的项目可见性和权限；
- 大数据量下使用服务端分页、过滤和定向缓存失效，不再启动时固定拉取 `limit=200` 的全部资源；
- 批量上传显示独立任务状态，单个文件失败不会触发全局重载或覆盖其他任务结果；
- 旧 URL、匿名公开 Artifact 浏览和现有 ChemDoodle 三维展示在迁移期间保持可用。

## 2. 当前基线与边界

### 2.1 已有能力

后端已有 `GET /api/auth/me`，返回当前用户、外部身份、可访问组织/项目、组织角色、项目角色和权限。当前权限为：

```text
artifact:read
artifact:download
artifact:upload
artifact:delete
project:manage
```

公开 Artifact 可匿名浏览；项目数据和上传由后端授权服务决定。前端已有 Vue 3、Vite、Lucide、ChemDoodle 组件，以及基本的 Reaction、Frame、Artifact 页面和 Playwright 检查。

### 2.2 明确缺口

后端已提供用户目录/启停、项目 CRUD、归档恢复、已有本地用户的成员管理，以及 Artifact
重命名、可见性修改和退役接口。当前仍缺少邀请状态机、组织成员管理和管理操作审计表，
因此以下功能继续阻断：

- 按邮箱邀请尚未首次 OIDC 登录的用户；
- 查看和处理 pending/accepted/expired 邀请；
- 组织成员和组织级管理。

前端不得用本地假数据模拟这些写操作。后端授权始终是最终裁决，前端权限判断只用于路由和交互体验。

## 3. 目标架构

```text
AppShell
  ├── Session / Access context
  │     ├── current user
  │     ├── organizations
  │     ├── projects
  │     └── capabilities
  ├── Project context
  │     ├── selected project (URL first)
  │     └── project-scoped query keys
  ├── Vue Router
  ├── TanStack Vue Query server state
  ├── Resource modules
  │     ├── reactions
  │     ├── mapped reactions
  │     ├── geometries / energy
  │     ├── calculations
  │     └── artifacts
  └── Upload queue
```

引入 Vue Router 管理 URL、返回、深链接和路由守卫；引入 TanStack Vue Query 管理缓存、取消、分页、重新验证和定向失效。短暂表单状态、抽屉状态和筛选输入保留在组件内；只有上传队列允许有一个小型领域 store。禁止重建一个新的全局单体 store。

类型优先从 OpenAPI 生成。迁移完成后删除手写 DTO 中与后端重复或冲突的定义；化学画布继续使用后端 Geometry SDF/MOL，不在浏览器从 SMILES 重建坐标。

## 4. 路由和页面职责

| 路由 | 职责 | 访问要求 |
| --- | --- | --- |
| `/reactions` | 逻辑反应分页、过滤和列表 | 登录用户；匿名仅显示公开结果（以后端结果为准） |
| `/reactions/:logicalReactionId` | 逻辑反应参与物、映射方案和路径关系 | `artifact:read` 或公开结果 |
| `/mapped-reactions/:mappedReactionId` | 映射节点、边、Geometry 绑定和 TS 关系 | `artifact:read` 或公开结果 |
| `/geometries` | Geometry 搜索、Formula/Topology 预筛和能量摘要 | `artifact:read` 或公开结果 |
| `/geometries/:geometryId` | 三维结构、聚合能量、来源 Frame/Protocol 和几何证据 | `artifact:read` 或公开结果 |
| `/calculations` | CalculationFrame 分页、协议/状态/频率过滤 | `artifact:read` 或公开结果 |
| `/calculations/:frameId` | 单帧完整结果、source span、数组元数据和下载入口 | `artifact:read`；下载另需 `artifact:download` |
| `/artifacts` | 原始文件列表、预览、下载和 ingestion 状态 | 公开或项目权限 |
| `/uploads` | 批量上传队列、失败重试和导入结果 | `artifact:upload` |
| `/search` | 跨资源查询入口，结果链接到资源详情 | 登录用户 |
| `/account` | 用户身份、组织和项目成员关系 | 已认证 |
| `/projects` | 可访问项目按组织分组的概览 | 已认证 |
| `/projects/:projectId` | 项目元数据、角色、权限和项目统计 | 该项目可见 |
| `/projects/:projectId/members` | 成员和角色管理 | `project:manage`，且后端提供管理 API |

项目选择器位于应用 Shell，并按组织分组。当前项目优先从 URL 恢复，其次才使用本地偏好；上传组件不得拥有第二份项目选择状态。切换项目时清理或失效项目范围内的 Query cache，禁止短暂展示上一个项目的私有详情。

## 5. 用户、组织、项目和权限

### 5.1 会话

建立 `useSession()`，以 `/api/auth/me` 为唯一读取来源，提供：

- `user`、`identity`、`isAuthenticated`、`isLoading`；
- 组织和项目树；
- `projectAccess(projectId)`；
- `can(projectId, permission)`；
- 401/403 的统一处理。

匿名状态不能被当作请求异常。公开 Artifact 页面可以继续工作；受保护路由显示登录或无权状态，而不是空白列表。

### 5.2 项目上下文

建立 `useProjectContext()`，提供当前项目、组织、角色和权限。所有项目相关 Query key 必须包含项目上下文，例如：

```text
["artifacts", { projectId, filters, page }]
["geometries", { projectId, search, page }]
["uploads", { projectId }]
```

上传目标项目没有 `artifact:upload` 时，不显示可提交状态；服务端返回 403 时保留任务失败证据并允许用户切换项目后重试。

### 5.3 管理能力

注册和登录不属于本项目的本地业务能力：生产环境由外部 OIDC 身份提供方负责注册、邮箱
验证、MFA 和会话签发；服务端只验证 JWT，并在首次登录时创建本地授权主体和
`issuer + subject` 映射。开发环境继续使用固定 Development User，不保存本地密码。

项目管理按基础协作能力实现，不扩展为组织运营后台。前端消费组织访问和项目管理接口；
基础版管理需要后端至少提供：

```text
GET    /api/projects/{project_id}
GET    /api/projects/{project_id}/members
GET    /api/users?project_id={project_id}
POST   /api/projects
PATCH  /api/projects/{project_id}
POST   /api/projects/{project_id}/members
PATCH  /api/projects/{project_id}/members/{user_id}
DELETE /api/projects/{project_id}/members/{user_id}
PATCH  /api/artifacts/{artifact_id}
DELETE /api/artifacts/{artifact_id}
```

上述基础接口已返回稳定 DTO 和明确的 401/403/404/409 语义；成员数量增长前仍需补服务端
分页。邀请接口支持过期、接受、撤销和投递状态；受邀者在外部 OIDC 登录后绑定现有邀请，
邮件失败可由项目管理页重发。邀请与管理操作已接入审计。

当前范围不包括本地密码、找回密码、计费、复杂审批、组织运营后台、细粒度自定义权限或
审计报表。`Viewer`、`Contributor`、`Manager` 三档项目角色仍由后端最终裁决。

## 6. 领域模块拆分

- `session`：会话、权限和项目访问上下文。
- `catalog`：Reaction、MappedReaction、Geometry、CalculationFrame、Artifact 的列表 Query 和分页。
- `geometry`：三维展示、GeometryEnergyView、来源标记和能量组成；能量不是 Reaction 的局部字段。
- `reaction`：逻辑反应、映射方案、节点、边和反应关系；不重复承载 Geometry 所有事实。
- `upload`：文件选择、并行批次、单文件状态、重试、ingestion 结果和定向缓存失效。
- `account`：用户身份、组织和项目访问概览。
- `project`：项目概览和后续成员管理。

列表 Query 只返回轻量摘要；详情 Query 按需加载，Geometry/Frame 和 MappedReaction 的大型一对多集合必须分页或分段查询。所有下载链接由 API client 生成，组件不得拼接后端路径。

## 7. 分阶段执行计划

### F0：契约冻结与基线

**交付物：** OpenAPI DTO 快照、权限矩阵、资源分页/过滤字段表、错误 envelope、旧路由映射和页面截图基线。

**验收：** 明确每个资源的项目可见性来源；明确匿名、Viewer、Contributor、Manager 的页面和操作矩阵；没有未声明的全量查询。

### F1：Shell、Router 和会话上下文

**交付物：** `AppShell`、Vue Router、Query client、`useSession`、项目切换器、路由守卫、401/403 页面。

**验收：** 刷新和深链接可恢复；项目切换不会串显私有数据；匿名用户仍可浏览公开 Artifact；当前用户权限可以控制上传和下载入口。

### F2：Geometry 垂直切片

**交付物：** Geometry 列表/详情、三维画布、GeometryEnergyView、来源 Frame/Protocol 列表、后端分页查询接入。

**验收：** 同一 Geometry 的电子能、热化学校正和总视图来源可追溯；不依赖 Reaction 才能查看 Geometry；移动端和桌面端画布无布局溢出。

### F3：Reaction 和 MappedReaction

**交付物：** 逻辑反应详情、多个 mapped reaction 切换、节点/边关系、Geometry 入口和 TS 关联。

**验收：** 多个 mapping 不覆盖彼此；无 Geometry 的节点可明确显示缺失；未达到优化收敛条件的中间帧不会伪装成反应 Geometry。

### F4：Calculation、Artifact 和搜索

**交付物：** Frame 详情、Artifact 预览/下载、结构和 Formula 搜索、服务端分页与过滤。

**验收：** 详情只按需加载；私有对象的列表、详情、反向查询和下载均受同一权限约束；Geometry 查询可以处理只有 Formula、没有可靠拓扑的分子。

### F5：上传队列

**交付物：** 并行批次队列、每文件进度/成功/失败、重试、取消、批次汇总和定向 Query invalidation。

**验收：** 不再使用全局 `refreshAll()`；单文件失败不影响其他文件；同一项目的 Artifact/Frame/Reaction 列表在导入完成后只刷新必要 Query；上传项目始终来自全局项目上下文。

### F6：账户和项目页面

**交付物：** `/account`、`/projects`、`/projects/:projectId`，组织分组、成员角色和能力展示。

**验收：** 所有项目访问来源于 `/api/auth/me`；无权项目不可通过 URL 进入；账户页不会暴露内部 token 或敏感 claims。

### F7：基础项目和成员管理

**前置条件：** 后端项目/成员 CRUD、基础邀请、角色变更和权限集成测试完成。

**交付物：** 项目名称编辑、成员列表、按邮箱邀请、角色修改、移除成员和操作反馈。

**验收：** `Viewer`、`Contributor`、`Manager` 的边界与后端一致；过期或已接受邀请有明确
反馈；每次变更保留操作者和时间；前端权限检查不能绕过后端拒绝。复杂审计 UI、组织管理
和审批流不属于本阶段。

### F8：删除旧入口与发布验收

**交付物：** 删除 `App.vue` 的全局资源状态、AbortController 集合、手写 DTO 和全量刷新逻辑；更新 README、开发文档和 Playwright 测试。

**验收：** `npm run typecheck`、`npm run build`、Playwright 桌面/移动端、权限矩阵、分页、项目切换、批量上传部分失败和 ChemDoodle 画布检查全部通过。

## 8. 后端配套任务

前端不能通过组件层规避以下后端问题，需作为前后端联合任务跟踪：

1. 为 Geometry、CalculationFrame、MappedReaction detail 提供可分页的独立关系查询。
2. 提供轻量反应能量 profile，避免为一个摘要加载完整 MappedReaction detail。
3. 所有资源查询支持明确的 `project_id`、分页、排序和服务端过滤。
4. 增加项目详情、成员、邀请、角色变更和审计 API。
5. 统一 401、403、404、分页和批量上传状态 DTO。
6. 如需实时进度，增加可轮询的 ingestion batch status；在此之前前端只承诺请求级状态。

## 9. 测试与发布门禁

- TypeScript：严格 typecheck，生成客户端与后端 OpenAPI schema 一致。
- 组件/Composable：session、项目切换、权限矩阵、Query key 隔离、401/403、上传队列部分失败。
- API 集成：匿名、Viewer、Contributor、Manager、跨项目 ID 枚举和下载授权。
- Playwright：桌面/移动端、深链接刷新、项目切换、Geometry 详情、Reaction 多 mapping、上传重试和部分失败。
- 性能：首屏不拉取所有资源；列表分页；详情请求数可测；项目切换不产生旧项目请求；大型上传批次不触发 N 次全局刷新。
- 可视化：ChemDoodle 画布非空、三维结构正确、抽屉和表格不重叠、移动端无横向溢出。

完成 F8 前不删除旧接口和旧路由；使用兼容重定向或双入口迁移。发布前必须有新的前端构建产物、后端 schema 快照、权限回归结果和迁移说明。

## 10. 完成定义

前端重构只有在以下条件全部满足时才算完成：

- 用户、组织、项目和权限上下文贯穿所有查询和上传流程；
- Geometry 的事实和能量视图不再由 Reaction 页面拥有；
- 服务端分页、过滤和 Query cache 取代固定 `limit=200` 和全量刷新；
- 上传队列支持并行批次、单文件失败隔离和定向刷新；
- 项目/成员管理只使用正式后端 API，并通过权限和审计测试；
- 现有公开 Artifact、ChemDoodle 结构展示和核心 API 行为保持兼容；
- 所有代码、文档、类型检查、构建、端到端和权限门禁均通过。
