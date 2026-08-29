# 环加成反应计算数据库业务模型

> 当前业务边界。英文版见 [English](en/business-model.md)。标题沿用历史项目名称；系统不会
> 自动把任何推断反应分类为环加成。

## 用户面对的对象

| 对象 | 含义 | 不变性/身份 |
| --- | --- | --- |
| ArtifactFile | 一个项目拥有的原始输入、输出或辅助文件 | 内容哈希和对象位置不可覆盖；可退役 |
| ParseRevision | 对 Artifact 的一次可审计解析 | 追加，不覆盖旧 revision |
| CalculationFrame | 解析出的一个计算帧 | 保留源坐标、方法、频率和数组溯源 |
| MolecularTopology | MolGR 重建的分子图 | 显式氢、键、电荷、自由基和立体信息参与身份 |
| Geometry | topology、坐标、电荷和多重度组成的几何事实 | 同坐标但电子状态不同即不同 Geometry |
| TransitionStateInference | 一个 TS 帧沿虚频方向的推断证据 | 可成功、拒绝或失败；不篡改 Frame |
| LogicalReaction | 两侧 topology 的净反应 | 与文件、坐标和反应名称无关 |
| MappedReaction | 一组确定的原子映射和路径节点 | 同一净反应可有多套 mapping |

## 导入业务流程

上传者选择项目并提交文件。浏览器、批量 API 和本地 CLI 进入同一上传服务：原始字节先完成
对象存储核验，再建立解析 revision。计算输出由 MolOP 解析；每个可恢复 frame 独立持久化，
所以一个坏 frame 只会使该 frame 或文件状态变为 `partial`，不会吞掉已成功 frame。没有
recoverable frame 的文件仍可下载和审计，状态为 `filtered`。前端必须把 `pending` 显示为
“正在解析”，不要误报为失败。

项目 manager 可重命名文件、调整可见性、请求 reparse 或退役文件；内容、哈希和已经记录的
计算事实不在原记录上修改。reparse 读取同一原始 bytes 并产生新 revision，方便比较不同
MolOP/MolGR 版本或解析配置。

## 化学事实和显示

MolGR 输出是分子图权威来源。服务不再对可信图做 RDKit chemical sanitization、补隐式氢或
canonical atom reorder；只建立所需 ring metadata。显式氢 SMILES 是展示和精确字符串检索的
共同表示，字段名 `canonical_isomeric_smiles` 为兼容遗留 API 保留。ChemDoodle 渲染关闭隐式氢
推断，避免在前端展示并不存在的氢。

Geometry 详情显示总电荷、总自旋多重度和坐标，并可下载 XYZ/SDF。XYZ 的 comment 行必须含
charge/multiplicity。一个 Frame 的源电荷和多重度用于创建 Geometry，并且所有同一 TS 推断的
端点继承 TS 的整体电子状态；不能因为分片而丢失自由基电子。

## TS 与反应路径

对于 TS 和 `suspicious_fallback` Frame，系统按原始原子顺序将虚频模式正、负位移交给 MolOP。
它保留两个端点和拒绝/错误证据。端点任一为 `suspicious_fallback` 时不能成为前后体；Frame
自身为该状态并不跳过证据采集。原子顺序直接对应，因此不做昂贵且无意义的图同构匹配。

成功端点形成的反应以 topology multiset 去重，拓扑发生变化是成功推断的正常结果。系统不做
“前后体必须相同”的验证，也不自动写 reaction class。用户可以在反应和 TS 查询中筛选前后体
topology 是否发生变化，并按可用字段排序。

## 权限和公开范围

Organization 负责项目集合，Project 负责数据授权。成员权限决定上传、读取、下载、重解析、
删除和项目管理。`public` Artifact 可被匿名读取；默认的 `project` 数据只能由对应项目成员
通过 API 访问。生产身份由 OIDC 提供；开发固定身份仅用于本地开发和测试。MCP token 只授权
MCP 入口，不能扩大 REST 或 GraphQL 权限。

## 非目标

本项目不调度 HPC 作业，不把计算结果改写成预设反应类型，不作为通用电子实验记录本，也不
存储实验产率、条件或文献主张。它保存的是可追溯的计算文件、帧、分子图、几何和推断证据。
