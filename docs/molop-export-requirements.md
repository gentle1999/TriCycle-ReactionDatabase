# MolOP 计算结果导出需求

> 状态：MolOP 契约已实现，数据库 schema 已对齐
> 契约版本：`molop-calculation-export-v1`
> 使用方：Example Chemistry Database（部署显示名可由环境变量覆盖）
> 最低兼容基线：PyPI `molop>=0.2.11`、`molgr>=0.1.8`
> 复核日期：2026-08-09

## 1. 目标

MolOP 通过现有 `ChemFile` 与 `ChemFileFrame` 模型提供稳定、版本化、可验证的计算结果
序列化视图。数据库导入 Gaussian、ORCA 等计算结果时，不得依赖 MolOP 私有 parser 类型、
缓存、完整原文或格式专用 semantic model。

正式导入必须保留 artifact、segment、frame、几何、拓扑、能量、数组和热化学之间的物理
关系，并能把每条 source evidence 追溯到原始 artifact 的确定字节区间。

本文使用以下约束词：

- **必须**：正式数据入库的阻断要求。
- **应**：建议在 v1 完成；缺失时必须提供明确诊断。
- **不得**：会破坏科学语义、可追溯性或确定性的行为。

## 2. 责任边界

MolOP 负责解析原始计算文件，输出程序无关的计算事实、portable topology、原文位置和解析
诊断。数据库负责内容寻址、接收模型、持久化、数组 sidecar、admission/QC、数据库 identity
以及 Reaction、ReactionPath、Node 和 Edge 的创建。

MolOP 不负责：

- 根据文件名、能量顺序或 TS 位移自动决定反应方向；
- 上传对象存储、生成数据库 UUID 或决定表结构；
- 决定任意计算 observation 是否可匹配某个已有 Geometry；
- 为源文件中不存在的 forces、Hessian 或其他数组补零；
- 导出私有 `_rdmol` cache、`Chem.Mol`、完整原文或 parser component tree。

数据库不得要求 MolOP 生成数据库专用的第二套 DTO。数据库的 Pydantic 接收模型应只声明
当前导入流程需要的字段，并对格式专用扩展使用 `extra="ignore"`。接收模型负责约束数据库
所需子集，不反向扩大 MolOP 公共模型。

## 3. 导出形状

文件和帧是两个独立序列化视图，不构造嵌套全部帧的巨大 JSON。

```text
ChemFile dump
  schema_version
  artifact_sha256
  artifact_size_bytes
  source_format
  source_encoding
  parser_provenance
  source_complete
  source_segments[]
    segment_index
    source_span
    source_block_sha256
    qm_software
    qm_software_version
    protocol
    task_requests[]
    task_types[]
    termination_status
    scf_status
    parse_presence
    parse_completeness
    diagnostics[]

ChemFileFrame dump
  frame_id
  segment_index
  segment_frame_index
  file_frame_index
  frame_role
  source_span
  source_block_sha256
  atoms
  coords
  charge
  multiplicity
  topology fields
  calculation fields
  parse_presence
  parse_completeness
  parse_diagnostics[]
```

`ChemFile.model_dump()` 不含私有 `_frames_`。数据库先写入一个 file view，再以 iterator、
批次或分页方式消费 frame views，并用 artifact identity 建立关联。传输层可以临时包装一个
file payload 和一页 frame payload，但该包装不是 MolOP DTO。

审计/可复现导入必须使用：

```python
chem_file = AutoParser(
    path,
    capture_source_evidence=True,
    release_file_content=True,
)[0]
file_payload = chem_file.model_dump(mode="python", exclude_none=False)
frame_payloads = (
    frame.model_dump(mode="python", exclude_none=False)
    for frame in chem_file
)
```

## 4. P0 阻断需求

### 4.1 版本、确定性与 portable DTO

| ID | 需求 | 验收标准 |
| --- | --- | --- |
| `MOLREQ-001` | file view 必须包含非空 `schema_version` | 固定为 `molop-calculation-export-v1`，字段语义不得随 patch 静默改变 |
| `MOLREQ-002` | 必须输出 parser、MolOP、MolGR、RDKit 版本及有效配置快照和 hash | ParseRevision 可保存完整 provenance，canonical JSON hash 可复算 |
| `MOLREQ-003` | 相同 bytes 和配置必须生成确定性 dump | 黄金样本连续导出两次相等 |
| `MOLREQ-004` | 公共 dump 不得携带 `Chem.Mol` 或私有 cache | topology 仅使用 SMILES、V3000 MolBlock、bonds、charges、radicals 和 provenance |
| `MOLREQ-005` | `array_mode="ndarray"` 返回的数值数组不得别名模型内部数组 | 修改调用方副本不改变 MolOP frame |

### 4.2 Segment、Frame 与原文区间

`SourceSpan` 是必需的半开区间，并完整包含：

```text
start_byte
end_byte
start_char
end_char
start_line
end_line
```

| ID | 需求 | 验收标准 |
| --- | --- | --- |
| `MOLREQ-010` | Gaussian Link1 和 ORCA job 必须导出为有序 segment | 多任务黄金样本的 segment 数、边界和顺序与原文一致 |
| `MOLREQ-011` | 每个捕获的 segment/frame 必须有独立 span 和原文 SHA-256 | 用 byte span 截取 artifact 后可复算 hash |
| `MOLREQ-012` | frame 必须输出 `segment_frame_index` 和 `file_frame_index` | `file_frame_index` 是完整 locator 布局的稳定全局序号，`only_last_frame` 不重置它 |
| `MOLREQ-013` | `frame_id` 只表示当前 `ChemFile` 的追加序号 | 数据库不得用 `frame_id` 恢复原文件顺序 |
| `MOLREQ-014` | status 必须按真实证据作用域输出 | segment termination 不伪装成逐 frame termination 事实 |
| `MOLREQ-015` | span 必须在 locator 拆分原文时产生 | 不得通过重复 frame 文本反向搜索位置 |
| `MOLREQ-016` | 正式导入默认设置 `capture_source_evidence=True`；可显式关闭 | 开启时必须保留 artifact identity、segment evidence、frame span 和 MolOP frame role；关闭时保留 artifact SHA-256、解析器 provenance/configuration、帧顺序与诊断，但不能提供证据派生的 frame role，源跨度/块哈希为 `NULL` |
| `MOLREQ-017` | 证据收集与延迟拓扑重建、批量持久化相互独立 | `TRICYCLE_MOLOP_PARALLEL_FRAME_PERSISTENCE=true` 时，纯文本解析阶段关闭 MolOP prewarm；持久化微批次在 owning process 中展平所有 frame，通过 MolGR 原生 batch scheduler 并行重建后再转换 DTO；revision-local rows 使用 client-side UUID 和延迟 flush 批量写入；审计/重解析路径保持严格幂等写入 |

segment 的 `protocol` 必须是通用 `model_chemistry` 的纯 mapping 投影，
`task_requests` 必须是通用 `QMTaskRequest` 的纯 mapping 列表。不得泄漏
Gaussian/ORCA 专用 Pydantic 类型。协议从 segment metadata 生成，因此零帧或坐标解析
失败的 segment 仍必须保留请求证据。

### 4.3 几何与原子顺序

| ID | 需求 | 验收标准 |
| --- | --- | --- |
| `MOLREQ-020` | 每个 frame 必须输出实际计算几何、元素顺序、charge 和 multiplicity | atom count、元素和坐标维度相互一致 |
| `MOLREQ-021` | 必须提供 portable topology | 至少包含 canonical isomeric SMILES、map-free V3000 MolBlock 和重建 provenance |
| `MOLREQ-022` | 只对可信 topology 输出 source 到 topology 的 permutation | permutation 完整、无重复；失败或歧义时省略，并输出 `parse_failed` 和稳定 diagnostic |
| `MOLREQ-023` | ORCA 必须输出实际观察坐标与 `coordinate_decimal_places` | 数据库可据打印精度设置匹配容差并独立计算 RMSD |
| `MOLREQ-024` | 各后端的观测几何来自公开 `frame.coords` | 诊断坐标不创建第二个 Geometry；匹配由数据库执行 |
| `MOLREQ-025` | V3000 MolBlock 中的坐标仅是 topology carrier placeholder | 数据库不得把 MolBlock 坐标当作计算几何 |

### 4.4 Typed Energy Observation 与单位 wire format

能量 observation 位于 `energies.observations[]`，至少包含：

```text
method
quantity_semantics  # total_energy | correlation_correction | component
value (hartree)
source_label
```

MolOP 的 wire format 直接使用带单位 key，例如 `value (hartree)`、
`coords (angstrom)`，而不是额外的 `{value, unit}` 包装。数据库接收层必须解析并校验
该 key 语法；若持久化为独立 value/unit 列，转换属于数据库责任。

| ID | 需求 | 验收标准 |
| --- | --- | --- |
| `MOLREQ-030` | 能量必须区分 total、correction 和 component | 未知语义不能回退为 total energy |
| `MOLREQ-031` | CCSD 与 CCSD(T) 必须使用不同 observation | Gaussian CCSD(T) 不写入 `ccsd_energy` |
| `MOLREQ-032` | MP2 等相关能必须明确 total 或 correlation correction | Gaussian 与 ORCA 黄金样本均有人工核对值 |
| `MOLREQ-033` | 所有 quantity 必须通过规范 unit key 携带可转换单位 | 转换到目标单位后与原始输出在约定容差内一致 |
| `MOLREQ-034` | parser 无法判定语义时必须输出结构化诊断 | 调用方将记录送入 quarantine，不猜测字段含义 |

### 4.5 ORCA Gradient、Force 与任务状态

| ID | 需求 | 验收标准 |
| --- | --- | --- |
| `MOLREQ-040` | `CARTESIAN GRADIENT` 必须作为 gradient 输出，或取负后作为 force 输出 | 黄金样本逐元素验证 `force = -gradient` |
| `MOLREQ-041` | 字段名称、source field、transformation 和单位必须一致 | 不得将未取负 gradient 放入 `forces` |
| `MOLREQ-042` | 非优化任务没有 optimization result 时必须在 `parse_presence` 标记 `not_requested` | 不要求伪造 `optimization_status=not_requested` 对象 |
| `MOLREQ-043` | 单点任务中出现优化证据必须产生拒绝诊断 | diagnostic code 为稳定的 `MOL.CALC.UNEXPECTED_OPTIMIZATION` |

### 4.6 解析状态与诊断

可选结果不能只用 `None` 表达所有缺失原因。`parse_presence` 必须能够区分：

```text
not_requested
absent_in_source
parsed
parse_failed
unsupported
```

| ID | 需求 | 验收标准 |
| --- | --- | --- |
| `MOLREQ-050` | diagnostic 必须包含稳定 code、severity、scope 和 message | 调用方不解析日志文本判断错误类型 |
| `MOLREQ-051` | 部分解析必须显式标记 `parse_completeness` | Geometry 成功但其他请求字段失败时不得声明 complete |
| `MOLREQ-052` | 未识别 route/block 只保留小型结构化证据 | 不得把完整原文复制到 metadata |

## 5. P1 完整性需求

### 5.1 优化指标与热化学

| ID | 需求 | 验收标准 |
| --- | --- | --- |
| `MOLREQ-060` | energy change、RMS/max force、RMS/max displacement 必须带单位 | dump 中使用稳定 unit key，不导出无单位裸值 |
| `MOLREQ-061` | 每个 metric 应提供 threshold 和源程序 convergence 判断 | Gaussian 优化表与 dump 可逐项核对 |
| `MOLREQ-062` | metric 必须绑定产生它的物理 frame | 不复制到没有该证据的终态重打印 frame |
| `MOLREQ-070` | 必须区分 correction 与 total thermodynamic quantity | ZPE、thermal correction、`U/H/G` 语义固定 |
| `MOLREQ-071` | 必须明确 per-mole/per-particle 语义和单位 | 单位转换可复算 |
| `MOLREQ-072` | 必须输出 temperature 和 pressure | 热化学结果不脱离计算条件持久化 |
| `MOLREQ-073` | vibrational temperatures 必须关联 frequency mode index | 排除虚频后仍能恢复模式对应关系 |

### 5.2 数值数组

MolOP 输出命名字段，例如 `forces`、`hessian` 和 vibrations 中的 normal modes，并在模型
旁提供 `axis_order`、`atom_order`、orientation、normalization、mass weighting 等
convention metadata。MolOP 不负责把所有数组重写成通用 `arrays[]` DTO。

| ID | 需求 | 验收标准 |
| --- | --- | --- |
| `MOLREQ-080` | 默认 dump 将数组转换为 JSON-safe list | 小 payload 可直接 JSON 编码 |
| `MOLREQ-081` | `array_mode="ndarray"` 保留无 object dtype 的数值 ndarray 副本 | 数据库可用 `allow_pickle=False` 编码 sidecar |
| `MOLREQ-082` | forces/Hessian/normal modes 必须声明原子与轴顺序 | 可验证 `(N,3)`、`(3N,3N)`、`(M,N,3)` |
| `MOLREQ-083` | normal modes 应声明 normalization 和 mass-weighting convention | 源文件无法确定时显式标记 unknown |
| `MOLREQ-084` | 源文件缺失数组时不得补零或复制相邻帧 | `parse_presence` 与实际命名字段一致 |

数据库负责 sidecar 的 dtype、shape、hash、object key 和原子顺序冗余校验，并用引用替换
frame payload 中的大数组。sidecar envelope 不是 MolOP 导出契约的一部分。

## 6. P2 可延期规范化

MolOP 已提供 `method_family`、`method`、`reference_method`、`functional`、
`dispersion_correction` 和 `spin_treatment`，但跨程序、跨任务的规范化仍允许继续收敛。

| ID | 需求 | 验收标准 |
| --- | --- | --- |
| `MOLREQ-090` | `method_family` 应使用稳定的粗粒度方法族 | 不把不稳定字段作为数据库协议唯一键 |
| `MOLREQ-091` | `method` 应使用稳定的具体电子结构方法名 | 同一计算跨 segment 不产生无依据的别名漂移 |
| `MOLREQ-092` | functional 应排除 R/U/RO spin 前缀和 dispersion 后缀 | spin、functional、dispersion 可独立查询 |
| `MOLREQ-093` | Gaussian 与 ORCA 应逐步统一规范化规则 | 等价协议得到等价通用字段 |
| `MOLREQ-094` | 无法规范化时必须保留 raw token | 不得用错误规范值替代原始证据 |

数据库当前必须保存 raw/protocol evidence，不得把这些仍可演进的规范字段单独作为不可变
identity。

## 7. 数据库端设计指南

1. file 与 frame 分表或分阶段导入；以 artifact identity 和数据库 file id 关联，不持久化
   一个包含全部帧的 JSON blob。
2. 接收模型只声明业务所需字段，先校验 file view，再流式或分页校验 frame views。
3. admission 阶段复算 artifact、segment 和 frame hash，校验所有 span 边界、序号唯一性和
   `source_complete`。
4. 使用 `file_frame_index` 建立原文件顺序；
   `segment_index + segment_frame_index` 用于段内定位；`frame_id` 只作当前 MolOP
   对象的局部信息。
5. 对 unit-key wire format 做白名单解析和维度校验，再映射到数据库 value/unit 列。
6. topology 与 calculation geometry 分开存储。V3000 坐标不得覆盖 `frame.coords`。
7. 仅在 permutation 存在且 topology presence 为 `parsed` 时接受 atom-indexed
   topology 映射；否则 quarantine 或按无可信 topology 处理。
8. 大数组使用独立 sidecar；数据库记录 dtype、shape、hash、axis convention 和
   source field。
9. `parse_presence`、`parse_completeness` 和 diagnostics 是 admission 输入，不是可丢弃日志。
10. Reaction/ReactionPath 语义、软件无关的 Geometry 匹配和 observation 接纳策略
    保持在数据库层。

## 8. 当前实现状态与剩余验收

MolOP 当前已实现：

- 独立 file/frame dump，固定 schema version 和 parser provenance；
- exact byte/character/line spans、artifact/segment/frame hashes；
- `segment_index`、`segment_frame_index`、稳定 `file_frame_index`；
- segment-scoped portable `protocol` 与 `task_requests`，包括零帧 segment；
- typed energy observations 和稳定 unit-key dump；
- portable topology、可信 permutation 规则和 topology diagnostics；
- ORCA 打印精度、gradient 到 force 的符号语义；
- parse presence/completeness/diagnostics；
- 命名数组字段、array convention metadata 和 ndarray sidecar 模式。

数据库接入状态：

1. 依赖要求为 PyPI MolOP `>=0.2.4` 与 MolGR `>=0.1.3`。Gaussian 进程内导入直接消费 MolOP
   公共模型的 `model_dump(mode="python", exclude_none=False)` payload，不重复实现
   locator、状态判断、拓扑重建或模型校验。数据库侧只做字段裁剪、Quantity 单位归一化、
   ndarray sidecar 摘要/转换和数据库 identity 绑定。
2. 已用 DA minimum、TS 和多 Link1 fixture 验证 9 个 segment、45 个 frame、227 个数组、
   4 个热化学结果、49 条 typed energy observation、14 个 molecular-orbital result、
   14 个 population result（18 条 series）和 14 个 polarizability result。
3. Topology graph identity 与版本化 reconstruction derivation 已分表；每个 frame
   关联实际 derivation，并持久化可选 `coordinate_decimal_places`。该字段因 MolOP
   `exclude_if` 可能不出现在 `None` dump 中，接收层从同一公共 model field 显式补齐。
4. 仍需对异常 termination、相关方法，以及 ORCA 单点、相关
   方法、gradient、坐标异常建立数据库端黄金导入快照。
5. 验证 file/frame 分阶段导入在大优化轨迹下不会构造全帧 JSON，也不会把 ndarray 留在
   JSON 编码路径。
6. 完成 ORCA admission 和 quarantine policy 的数据库测试。
7. 将 model chemistry 进一步规范化视为 P2 演进，不阻塞当前 evidence-first 导入。

MolOP 当前少数 computed evidence 属性尚未进入 `model_dump()`（例如 file/frame
diagnostics 和部分收敛摘要）。适配器只对这些明确列出的字段做最小属性回退；字段一旦进入
MolOP dump，数据库适配器不再直接读取该属性。该回退不改变 MolOP 的事实来源，也不构造
第二套 parser DTO。
