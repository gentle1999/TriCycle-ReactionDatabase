# 技术方案与实施路线图

> 状态：已接受  
> 生效日期：2026-07-12  
> 当前阶段：M1-M3 已完成 / M6 只读查询原型实现 / M4-M5 待继续  
> 文档职责：本文件是 v1 技术范围、架构边界和实施顺序的权威来源。

具体未完成任务、依赖和阻断验收维护在[实施目标清单](implementation-backlog.md)。
前端专项的路由、用户/组织/项目上下文、上传队列和发布门禁维护在[前端重构计划](frontend-refactor-plan.md)。

## 1. 目标与范围

本项目建设一个专用于环加成反应路径节点计算数据的数据库。数据流程为：

- MolOP probe 负责原始量化文件格式识别、解析和计算语义提取。
- Geometry 表示软件和势能面无关的核坐标；Protocol/Frame 表示具体计算描述。
- 原始文件可直接批量上传；workflow manifest 仅作为可选的人工策展入口。
- RustFS 负责原始 artifact 的不可变对象存储。
- PostgreSQL/RDKit 负责持久化、结构索引和领域查询。
- FastAPI/NexusX 负责 REST、GraphQL 和 MCP 查询接口。

v1 不建设以下能力：

- 实验条件、产率、ee/dr、文献和通用实验反应数据库。
- 根据文件名或目录结构创建反应，或把虚频正负号解释为绝对反应方向。
- HPC 任务提交、量化计算调度和工作流编排。
- 通用自动原子映射、批量自动反应发现和机理生成；TS 上传只使用同一帧的
  source atom order。
- 公共写入式 MCP 和 GraphQL 自动 CRUD。
- Redis、Celery、Kafka、微服务、数据湖、向量数据库和机器学习功能。
- 在取得代表性规模基准前进行分区、分库或缓存优化。

## 2. 已接受的架构原则

### 2.1 逻辑反应与文件解耦

`LogicalReaction` 只由两侧 topology 和计量定义，可通过显式 API 或 manifest 导入创建。
Topology 优先使用 MolOP 导出的 MolGR 分子图。Manifest 只声明 artifact 与节点计算
绑定；SMILES 和文件名不能替代 topology 外键。统一计算文件上传流程可以使用 MolOP
`possible_pre_post_ts()` 产生的前后体 topology 建立或复用反应，但必须保存 artifact、
TS frame、imaginary mode、推断参数和最终反应 FK。显式创建与 TS 推断共享相同反应身份，
创建方式不进入 identity key。

### 2.2 Geometry 与计算后端解耦

Geometry 只由 topology 和 E(3) 等价的核坐标定义。任意量化软件的 Frame 都可
精确复用或通过唯一候选匹配复用 Geometry。匹配先做 proper rigid alignment，必须保存
帧原始坐标、坐标哈希、原子映射、刚体变换、RMSD、最大偏差、打印精度和策略版本；
候选不唯一时不得静默选择。

### 2.3 原始事实不可变，派生结果可重算

Gaussian 电子能、频率和热校正，以及 ORCA 单点能分别保存。复合能、相对能和
活化能必须记录输入记录、公式、单位、温压/标准态、计算协议和算法版本，不能
覆盖任何原始计算值。

例如，以下公式只能作为版本化派生结果：

```text
G_composite = E_ORCA_SP + (G_Gaussian - E_Gaussian)
```

### 2.4 结构身份、几何和路径节点分离

化学数据使用 `MolecularFormula -> MolecularTopology -> Geometry` 三级存储。
Formula 只表示元素/同位素组成；Topology 是带 `mol: Chem.Mol` ORM 属性的权威化学
图实体；Geometry 保存按 Topology 原子顺序的 MolOP InternalCoords 和规范 RDKit Mol，
CalculationFrame 保存日志原始 Cartesian 坐标及 source→Geometry permutation。
相同 topology 可以有多个 Geometry，相同 Geometry 也可以被多个计算帧引用。
`MolecularTopologyDerivation` 独立保存 topology 的重建方法、版本和 provenance；
CalculationFrame 同时引用 Geometry 与其实际 derivation，避免同图复用时丢失重建历史。

物理文件主轴为 `RustFS Object -> ArtifactFile -> ParseRevision ->
CalculationSegment -> CalculationFrame -> Geometry`。Frame 是文件中的物理出现，
不能与可复用的 Geometry 合并。所有优化帧均逐帧重建并连接实际 topology；反应节点只关联
Geometry，中间帧也保留在 Geometry 的事实链并在反应坐标详情中展开；
连通性变化时允许连接到不同 topology，不能无条件继承优化目标 topology。

反应主轴为 `LogicalReaction -> MappedReaction(mapped rxn_smiles) -> Node ->
NodeGeometry -> Geometry <- CalculationFrame`。Node 可以绑定多个 component 和多个候选坐标，
同一坐标可以关联不同计算级别；rxn atom map 与几何原子顺序必须通过版本化转换记录连接。
能量选择是 GeometryEnergyView 的请求时行为，不是 reaction table。完整概念模型见
[数据模型与存储边界](data-model.md)，字段级候选设计见
[环加成反应路径数据库业务模型](business-model.md)。

Python `Chem.Mol` 到 PostgreSQL `mol` 的真实往返已确认可保持原子顺序、化学图、
立体化学、同位素、电荷、自由基、atom map 和项目所需键类型。它不保证自定义
property，conformer 坐标还会发生精度量化。Topology.mol 只承担 graph；Geometry.mol
保留规范 atom order 单 conformer，内坐标是几何身份权威，原始 Cartesian 坐标只属于
CalculationFrame。完整边界见
[RDKit Mol 对象数据库往返契约](rdkit-mol-roundtrip.md)。

### 2.5 MolOP 通过稳定公共模型集成

MolOP PyPI 发布包提供带 `schema_version` 的 ChemFile/ChemFileFrame Pydantic 模型。
数据库直接消费 `model_dump(mode="python", exclude_none=False)`，从同一 dump 投影
segment/frame source span、显式单位、typed energy observation 和数值数组；只对
Quantity 归一化、ndarray sidecar、关系型字段和数据库 identity 做必要转换。数据库不维护
平行 parser DTO，也不访问 `_frames_` 等私有 parser 状态。

### 2.6 原始 artifact 外置

Gaussian/ORCA 原始输入和日志以原始逻辑字节保存在私有 RustFS bucket；固定的 RustFS
`1.0.0-beta.8` 通过 `RUSTFS_COMPRESSION_ENABLED=true` 在磁盘层透明压缩可压缩对象。
PostgreSQL 保存 bucket、object key、version ID、SHA-256、大小、软件版本、解析器版本和
导入 revision，不将数据库作为大日志 BLOB 仓库。S3 GET/HEAD 仍返回原始逻辑字节，
Artifact 的 SHA-256 和大小不以内层压缩表示为准；S3 ETag 不能替代内容哈希。

RustFS 与 PostgreSQL 不共享事务。上传先提交 `ArtifactFile(pending)`，再写 RustFS，校验
成功后更新为 `available`；状态必须区分 `pending`、`available`、`missing` 和 `corrupt`。
上传生命周期 Hook 在失败出口通过 identity lock 定点清理本次对象；可选的
`tricycle-rustfs-gc` 再通过 PostgreSQL 水位和 RustFS UTC 小时分区处理进程崩溃、外部写入
等未经过 Hook 的孤立对象。失败 Hook 和 GC 会删除从未成功发布的 pending 预约行；
`missing` 只保留曾经 available、后来对象不可用的历史事实。宽限期、pending 补偿和
advisory lock 共同处理跨后端失败与并发。解析或 QC 隔离状态属于
ParseRevision/导入记录，不与对象可用性混用。

### 2.7 数值数组直接映射 ORM

Geometry 坐标及解析后的梯度、Hessian、振型和轨道矩阵不使用 JSONB。它们以 NPY
编码写入 PostgreSQL `BYTEA`，通过自定义 SQLAlchemy `NumpyArray` 类型直接返回
`np.ndarray`。除 Geometry 坐标外的矩阵共用 `ScientificArray` 实体，保存 owner、
kind、unit、dtype、shape 和 SHA-256。

RustFS 在 v1 只保存原始 artifact。超大矩阵外置必须等待真实规模基准，并通过显式
loader use case 实现，禁止让普通 ORM 属性访问隐式触发网络请求。

### 2.8 API 依赖 use case，不依赖 ORM 暴露

SQLModel table entity、Pydantic API DTO 和 NexusX schema 分离。FastAPI 是稳定
HTTP 边界；NexusX 是可替换的 GraphQL/MCP adapter。化学搜索和 mutation 通过
显式 use case 暴露；只启用白名单 `create_reaction` mutation，不启用通用 entity CRUD。

### 2.9 模块化单体优先

v1 使用一个代码库、一个 PostgreSQL 数据库和一个 RustFS artifact store，不拆应用
微服务。运行边界为：

| 进程 | 职责 |
| --- | --- |
| `api` | FastAPI、NexusX 和 application services；处理查询与轻量写入 |
| `ingest-worker` | MolOP/MolGR/RDKit 解析、规范化、QC、幂等导入和派生计算 |
| `migrate` | 以一次性任务运行 Alembic；禁止生产环境 `create_all` 或启动时隐式改表 |
| `postgres` | PostgreSQL/RDKit；保存领域事实、结构索引、导入状态和审计信息 |
| `rustfs` | 保存不可变原始 artifact；不执行领域逻辑或解析任务 |

首个导入 MVP 先提供复用同一 application service 的批处理 CLI。需要 API 异步提交
导入时，再使用 PostgreSQL 任务表和 `FOR UPDATE SKIP LOCKED` 驱动 worker；v1
不为此引入 Redis/Celery。

## 3. 目标数据流

```text
workflow manifest
  -> PostgreSQL 提交 pending ArtifactFile
  -> 上传并校验 RustFS object
  -> ArtifactFile 更新 available
  -> 校验 SHA-256、对象可用性与 manifest 完整性
  -> 创建 ParseRevision 与 CalculationSegment
  -> MolOP probe 自动识别并解析 Gaussian/ORCA/QM 输出
  -> 逐帧建立实际 formula/topology、geometry 和 ScientificArray
  -> 保存全部有效优化帧并执行 Gaussian QC
  -> MolOP 解析 ORCA single-point
  -> 通过 topology + InternalCoords 校验并复用 Geometry
  -> 保存不可变计算事实与解析 provenance
  -> 执行版本化 QC 和派生能量计算
  -> 发布可查询 MappedReaction
  -> REST / GraphQL / MCP 查询或显式创建
```

导入状态必须区分：

1. `parsed`：日志已成功转为结构化事实。
2. `validated`：计算和几何通过版本化 QC policy。
3. `published`：记录可组成可信路径并对查询接口可见。
4. `quarantined`：解析或 QC 证据不足，保留原始事实但不进入正式路径。

重新解析创建新 revision，不覆盖历史结果。重复的 artifact + parser/config hash
必须幂等。

## 4. 初始技术基线

以下版本用于首轮兼容性验证。实际依赖必须提交 `uv.lock`；升级需要重新通过
真实 PostgreSQL/RDKit、Gaussian/ORCA 黄金样本和 API 契约测试。

| 组件 | 初始基线 | 策略 |
| --- | --- | --- |
| Python | `3.12.x` | 应用与 CI 的首个支持版本 |
| 包管理 | `uv` | 提交 `pyproject.toml` 与 `uv.lock` |
| PostgreSQL/RDKit | `antonsiomchen/cheminfo-db:postgres18-rdkit2025.09.6` | 开发与兼容性基线 |
| Linux/amd64 image | `antonsiomchen/cheminfo-db:postgres18-rdkit2025.09.6` | Compose 使用明确 tag；生产发布记录实际 digest |
| Python RDKit | `2025.09.6` | 与 cartridge 版本对齐并做 binary round-trip 测试 |
| RustFS | `rustfs/rustfs:1.0.0-beta.8` | 上游尚无 stable release；升级必须重跑对象往返，并在生产发布记录实际 digest |
| MolAlchemy | `0.0.7` | 仅作为 SQLAlchemy/RDKit cartridge 适配层 |
| SQLAlchemy | `2.0.51` 兼容基线 | 最终 patch 由 `uv.lock` 固定 |
| SQLModel | `0.0.39` | table entity 与 DTO 分离 |
| Pydantic | `2.13.4` | API 与 MolOP 导出契约基础 |
| FastAPI | `0.135.1` | 稳定 HTTP 边界 |
| NexusX | `6.1.2` | 精确锁定；使用 DTO-first Compose executor、严格 selection 校验、可组合 ErManager 与 Voyager service cluster 能力 |
| FastMCP | `3.1.x` | 保持 NexusX 的 `<3.2` 兼容约束 |
| MolOP / MolGR | PyPI `>=0.2.4` / `>=0.1.3` | 声明最低兼容版本；lock 升级后重跑真实样本与迁移门禁 |
| 数据库迁移 | Alembic | 所有 schema 变更必须通过 migration |

数据库连接优先使用 psycopg 3：API 使用 SQLAlchemy `AsyncSession`，导入 worker
可使用同步 Session。两者共享 entity/repository 代码，但不共享事务或 Session。

MolAlchemy 类型在 SQLModel 中显式绑定，ORM 属性直接返回 `Chem.Mol`。API DTO 仍
显式转换为稳定文本或结构 DTO：

```python
model_config = ConfigDict(arbitrary_types_allowed=True)

mol: Chem.Mol = Field(
    sa_type=RdkitMol(return_type="mol"),
    nullable=False,
)
```

矩阵 ORM 属性使用 `NumpyArray` 映射 NPY `BYTEA`；显式加载后返回 `np.ndarray`。
禁止 object dtype，序列化和反序列化均固定使用 `allow_pickle=False`。payload 默认
deferred，AsyncSession 不通过普通属性访问隐式加载矩阵。

M1 原型已验证 SQLModel 同一实体的 `Chem.Mol` 与 `np.ndarray` 可分别通过 PostgreSQL
`mol` 和 `BYTEA` 精确往返，并验证 query-level defer/undefer。`NumpyArray` 当前使用
64 MiB 原型上限，正式阈值仍需由 M0 黄金样本基准确定。

首次 Alembic migration 必须验证 RDKit extension 后再创建 `mol`/`reaction` 列。
生产 downgrade 不自动执行 `DROP EXTENSION rdkit`。

## 5. 科学与数据质量门

### 5.1 Gaussian opt/freq

- 保存所有有效优化帧；每帧连接逐帧重建的实际 topology 和不可变 Geometry。Frame
  同时保存原始坐标系，以保证振动模、梯度和 Hessian 的方向语义不被 Geometry 去重破坏。
- 区分 initial、intermediate 和 terminal candidate；`final_accepted` 仅由 Frame QC/manifest
  事实表达，异常终止的最后一帧不能标记为已收敛。反应只引用 Geometry。
- 验证正常终止、SCF 收敛、目标任务和真实优化收敛指标。
- 频率必须对应最终优化几何；分文件时显式关联并检查原子序列和坐标 RMSD。
- 极小值要求 0 个显著虚频；TS 要求恰好 1 个显著虚频。
- 显著虚频阈值、低负频告警、频率缩放和低频修正由版本化 QC policy 定义。
- TS 虚频模式应与 manifest 的预期 bond-change signature 一致；不一致时隔离，
  不修改路径拓扑。
- 保存温度、压力、标准态、全部频率/模式及原始优化阈值，不能只保存布尔结论。

### 5.2 跨后端单点与复算

- 任务语义来自 MolOP 的 segment protocol，不由文件扩展名决定。
- 验证终止、SCF、方法、基组、溶剂、状态、电荷和多重度等 Frame 事实。
- 元素、拓扑或原子映射不一致时阻断；坐标偏差超过版本化容差时隔离。
- 精确坐标身份直接复用 Geometry；打印误差匹配保存完整 assignment evidence。
- 缺少足够或唯一几何证据时不能声明结果已关联到节点 Geometry。

### 5.3 能量与派生结果

- 分别保存 Gaussian/ORCA 语义明确的 total energy，不将 MolOP computed `total_energy`
  或未区分 correction/total 的旧字段直接当作发布值。
- 规范单位：坐标使用 angstrom、电子能使用 hartree、频率使用 `cm^-1`、时间使用 second。
- 持久化的 total/correction 标量 Hartree 能量统一量化到小数点后六位；原始日志仍由
  ArtifactFile 保存，优化收敛阈值不参与该量化。
- 派生结果必须记录公式、输入 FK/hash、协议、单位、温压/标准态和算法版本。
- 禁止跨电荷、多重度、电子态、溶剂、化学计量或不兼容协议静默比较。
- 相对能和势垒必须显式记录参考节点，并能够从原始输入独立复算。

### 5.4 结构与立体化学

- 保存 Formula、带 RDKit `mol` 的 Topology、内坐标 Geometry、CalculationFrame 原始坐标
  与 source→Geometry permutation，以及 isomeric mapped SMILES。
- 立体状态区分 `assigned`、`unassigned`、`unknown` 和 `conflict`。
- TS 新成键位置可以是 `unknown`，不得强行赋予 CIP 构型。
- RDKit/MolOP/MolGR 的结构重建和标准化版本必须进入 provenance。
- 序列化 round-trip 必须保持手性和原子映射。

### 5.4.1 Topology 结构检索实施计划

结构检索以无构象的 `MolecularTopology.mol` 为唯一候选集；Topology 不持久化 2D
conformer，展示坐标由 RDKit 在输出时生成。`Geometry`、`CalculationFrame` 和反应节点只在
Topology 命中后按需展开，避免同一图因构象或计算级别重复命中。

实施分期如下：

1. **已完成：Formula 预筛 + 图匹配。** 提供稳定 REST DTO，先通过
   `MolecularFormula` 的 `id`、composition hash 或 Hill formula 联合到 Topology，再执行
   标准化 canonical isomeric SMILES 的 B-tree 精确身份匹配，以及 RDKit GiST
   支持的 SMARTS
   子结构、手性匹配和子结构计数。查询必须至少带一个 Formula、图或关系型拓扑条件，并限制
   分页上限。即使 RDKit sanitize 失败，原始连接图仍保留并参与 SMARTS/子结构匹配；结果
   暴露 `sanitization_status`、`sanitization_error` 和 `morgan_bfp_available`。
2. **已完成：版本化相似度索引。** `MolecularTopology` 使用数据库生成的
   `morgan-bfp-r2-v1` `bfp` 投影和 GiST 索引，查询端提供 Tanimoto/Dice 阈值、实际分数和
   Top-K KNN 排序；查询分子按图身份补齐普通氢后生成同一规范指纹。禁止对全表临时计算
   fingerprint 或用 pgvector 代替 RDKit 指纹索引。失败 sanitize 的拓扑不生成 Morgan
   指纹，因此不会进入相似度排序或阈值匹配。
3. **已完成查询，按需投影：描述符检索。** 当前使用 cartridge 的 MW、LogP、TPSA、
   HBA/HBD、环数和 Murcko scaffold 函数，并支持范围筛选；Formula 条件应作为大表预筛。
   对 sanitize 失败的拓扑返回 `NULL`，不调用可能抛错的 cartridge 描述符函数。高频描述符
   只有在基准证明需要时才增加版本化标量投影和 B-tree 索引。MCS 仅用于两个分子或
   已缩小候选集的分析，不能作为无界列表查询。
4. **已完成：反应结构检索。** `MappedReaction` 使用数据库生成的 cartridge `reaction`
   投影和 `reaction-structural-bfp-r5-v1` structural fingerprint；reaction SMARTS、
   Tanimoto/Dice 阈值和 Top-K KNN 分别使用 reaction/bfp GiST 索引，不从普通 reaction
   SMILES 文本逐行重建检索对象。

### 5.5 路径、溯源与审计

- manifest 显式给出 path/node/edge、节点角色、顺序、atom map 和预期 bond change。
- 校验边端点存在，且元素、总电荷、自旋和化学计量守恒；显式碎片/副产物除外。
- 每个 artifact 保存 RustFS bucket/key/version、SHA-256、大小、软件/版本、任务参数和
  scheduler ID；ETag 不作为内容身份。
- 每个正式 Frame 可追溯到 ArtifactFile、ParseRevision、CalculationSegment 和文件内
  frame index，并通过 Geometry 连接实际 Topology。
- 每个 QC 结果保存 `rule_id`、`policy_version`、测量值、`pass/warn/fail` 和证据引用。
- 人工修正必须保存操作者、原因、时间和被替代 revision。

### 5.6 API 与运维

- API、worker 和 migration 使用独立最小权限数据库账号。
- GraphQL/MCP 只暴露 allowlisted use case，默认 MCP 只读。
- 限制查询深度、复杂度、结果数、结构查询输入大小和数据库 statement timeout。
- 日志解析在独立 worker 中完成，禁止 API 接受任意服务器路径或执行命令。
- 每次 schema 发布验证 fresh database、上一版本升级、fixture backfill、约束和索引。
- 外部数据库镜像用于 M1-M6；M7 上线前必须完成 SBOM/签名与构建来源审计，或从
  固定源码自行构建并发布受控镜像。
- v1 发布前完成备份恢复/PITR 演练，并记录不可逆 migration。
- RustFS 与 PostgreSQL 执行协调备份、恢复和对象一致性演练。

## 6. 实施路线图

```text
M0 -> M1 ----\
              -> M3 -> M4 -> M5 -> M6 -> M7
M0 -> M2 ----/
```

| 阶段 | 目标与交付物 | 阻断验收条件 |
| --- | --- | --- |
| **M0 范围与样本冻结** | 领域词汇与不变量、manifest v1、人工标注 Gaussian/ORCA 黄金样本、核心 ADR | 每个样本的路径、节点角色、权威几何和预期结果均可人工核对；不依赖文件名隐含规则 |
| **M1 技术兼容性验证** | Docker Compose、`uv.lock`、Alembic、MolAlchemy `Chem.Mol`/GiST、NumPy/NPY `BYTEA`、RustFS 对象往返和 NexusX DTO 只读 POC | 空环境可启动；extension、migration、化学图与矩阵往返、已知损失、对象哈希、结构查询和 DTO schema 测试通过 |
| **M2 MolOP 导出契约** | source span、显式单位、typed energy observation、kind/dtype/shape 数组、结构化错误和通用几何关联规则 | 黄金样本确定性快照通过；energy 区分 total/correction 和 CCSD/CCSD(T)；错配记录进入 quarantine |
| **M3 表设计与持久化内核** | formula/topology/geometry、artifact/revision/protocol/segment/frame、array/thermochemistry、reaction/path、QC/derived energy 和 Alembic migration | 每帧连接实际 topology；节点显式绑定跨后端计算；ORM 直接返回 `Chem.Mol`/`np.ndarray` |
| **M4 数据入库 MVP** | 直接上传优先的 `validate/import/reparse` 流程、可选 manifest、事务、隔离区、导入报告和完整 provenance | 原始日志无需预整理即可全量导入；重复运行零新增；单文件失败不污染整批；任一记录可追溯到源 artifact |
| **M5 领域查询与 QC** | 路径重建、协议过滤、化学结构查询、版本化 QC、复合能与活化能 | 路径与人工标注一致；派生值可解释和独立复算；代表性规模查询命中索引，无意外全表扫描 |
| **M6 API MVP** | FastAPI REST、NexusX GraphQL、只读 MCP、统一 DTO、分页、过滤、错误与超时策略 | 三种接口调用同一 use case 且结果一致；ORM 类型不泄漏；schema 快照、安全和查询成本测试通过 |
| **M7 试运行与 v1** | 真实环加成路径试导入、生产部署、CI、监控、备份恢复、迁移和重解析手册 | 全新环境可重建；完成恢复演练；科学数据、路径、能量和 provenance 抽样复核通过 |

M3 revision `20260713_0002` 已实现 Formula、Topology、Geometry、Artifact 和
CalculationProtocol；`20260713_0003` 已实现 ParseRevision、Segment、Frame、
ScientificArray 和 ThermochemistryResult，并验证 SQLModel Relationship、复合 FK、
deferred NPY 与迁移往返；`20260713_0004` 已实现 manifest、artifact binding、净反应、
参与物、路径、节点、坐标绑定和边，并以真实 DA 子集验证定向路径往返。
`20260714_0005` 已采用固定 MolOP 提交的文件、segment、frame 和附加 dataclass 导出，
完成 46 帧、123 个数组及能量/优化/振动/热化学/状态结果的原型录入。M6 已按 NexusX
6.1.2 开发指南实现共用 `UseCaseService` 的只读 REST、Compose GraphQL、四层 MCP，并以
单数据库 member 的 `ComposedErManager` 为 Voyager 实体和 DTO 提供可配置 cluster/color；
身份、OIDC 映射、组织/项目成员授权及公开 Artifact 匿名读取已由 `20260809_0013`
实现；`20260809_0014` 已增加认证统一上传、MolOP 全帧持久化、TS 帧检测、虚频前后体
推断和共享反应的幂等关联；`20260810_0015` 至 `0017` 已增加 RustFS 增量 GC 水位、
运行审计、pending 补偿和索引。I3 领域过滤和 A2 查询成本门已完成：数据库 statement
timeout、结构候选预算、GraphQL 深度/复杂度、REST/MCP 限流、慢查询脱敏日志及代表性
索引计划均有自动化验收；M6 剩余阻断项是跨传输查询授权闭环和完整测试基线。

里程碑定义：

- M4 完成后达到“数据入库 MVP”。
- M6 完成后达到“可用 API MVP”。
- M7 验收通过后发布 v1。

## 7. M0 黄金样本最低矩阵

M0 至少冻结以下人工期望值和 QC 证据：

- Gaussian 正常极小值、正常 TS、两个显著虚频、低负频、未收敛和异常终止。
- Gaussian opt/freq 几何一致与不一致样本。
- ORCA 正确 single-point、坐标打印误差、原子顺序变化、元素/电荷/多重度错配和
  误跑 opt。
- 具有相同 achiral SMILES 的对映体/非对映体。
- 不同原始能量类型、复合能输入和跨协议拒绝案例。
- 重复 artifact、不同 parser/config hash 重解析和人工修订。
- 优化中 topology 改变、重复 Geometry 被多个 Frame 引用，以及 dtype/shape/NaN 数组
  往返。

每个样本提供固定 manifest、artifact hash、期望 ChemFile/ChemFileFrame dump、QC rule
evidence 和预期发布状态，作为 parser、adapter、migration、QC 与 API contract test
共享夹具。

## 8. 变更规则

以下变更必须先更新本文件和对应 ADR，再进入实现：

- 替换数据库、cartridge、ORM、解析器或 API 核心组件。
- 改变 Geometry 匹配策略或能量组合规则。
- 允许自动路径推断、写入式 MCP 或通用 entity CRUD。
- 引入新的外部队列、缓存、微服务或 artifact 存储系统。
- 修改 canonical units、身份规则、QC policy 或 provenance 语义。

依赖 patch 升级、镜像 digest 更新和 MolOP/MolGR revision 更新不改变总体架构，
但必须更新 lockfile、部署清单和 provenance，并重新通过对应质量门。

## 9. 上游组件

- [MolOP](https://github.com/gentle1999/MolOP)
- [MolGR](https://github.com/gentle1999/MolGR)
- [MolAlchemy](https://github.com/asiomchen/molalchemy)
- [NexusX](https://github.com/KLR-Pattern/nexusx)
- [SQLModel](https://github.com/fastapi/sqlmodel)
- [FastAPI](https://github.com/fastapi/fastapi)
