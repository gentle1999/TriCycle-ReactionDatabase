# 高性能导入配置指南

> English edition: [High-performance import configuration](en/performance-tuning.md).

本文给出计算文件导入的性能配置和调优方法。目标是让 RustFS 暂存、MolOP 解析和 PostgreSQL
持久化形成连续流水线，同时保持 source evidence、逐文件失败隔离和重解析替换语义。

## 1. 性能边界

当前导入链路如下：

```text
本地 / 浏览器 / 远程 / MCP
        |
        v
RustFS 暂存 + UploadBatch 状态 = staged
        |
        v
唯一 upload-worker
        |
        +--> 共享 MolOP spawn 进程池（连续补充任务）
        |
        +--> 解析结果队列
                    |
                    v
            project/user 持久化消费者
                    |
                    v
            微批写入 PostgreSQL
                    |
                    v
            队列排空：profile 刷新 + 项目级 ANALYZE
```

`UploadBatch` 只是客户端的上传和进度边界，不是解析或数据库事务边界。单文件上传也必须先
进入 RustFS，再由 worker 领取；不要在 API、MCP 或本地导入器中直接启动 MolOP。

生产环境建议只运行一个 `upload-worker` 副本。每个副本都会创建自己的 MolOP 进程池、领取器
和持久化消费者；横向增加副本不会自动合并进程池，反而会增加 CPU、数据库会话和锁竞争。
API 可以横向扩展，但解析 worker 应作为独立的专用算力服务运行。

## 2. 已验证的高吞吐起始配置

下面是当前 24 个可见 CPU 核、远程 PostgreSQL/RustFS、单个 upload-worker 的实测起始配置。
它不是所有主机的固定最优值，但应作为专用算力主机的第一组参数：

```dotenv
# MolOP：一个进程处理一个文件；每个进程内部禁止额外的 BLAS/OpenMP 线程。
TRICYCLE_MOLOP_BATCH_N_JOBS=-1
OMP_NUM_THREADS=1
OPENBLAS_NUM_THREADS=1
MKL_NUM_THREADS=1

# 连续 dispatcher：24 个 MolOP worker 前面保持约 4 倍的有界预取。
TRICYCLE_UPLOAD_WORKER_PREFETCH_FILES=96
TRICYCLE_UPLOAD_WORKER_POLL_INTERVAL_SECONDS=0.1

# 同一 project/user 的持久化消费者按较大的微批提交。
TRICYCLE_UPLOAD_WORKER_PERSISTENCE_BATCH_FILES=64
TRICYCLE_UPLOAD_WORKER_PERSISTENCE_FRAME_LIMIT=2048

# 数据库连接池不是 MolOP 进程数。
TRICYCLE_DATABASE_POOL_SIZE=24
TRICYCLE_DATABASE_MAX_OVERFLOW=24
TRICYCLE_DATABASE_POOL_TIMEOUT_SECONDS=60

# RustFS 暂存并发和解析并发是两套独立的限制。
TRICYCLE_UPLOAD_MAX_CONCURRENCY=8

# 解析完整性开关必须保持开启。
TRICYCLE_MOLOP_CAPTURE_SOURCE_EVIDENCE=true
TRICYCLE_MOLOP_PARALLEL_FRAME_PERSISTENCE=true
```

此配置在 256 个真实 Gaussian/ORCA 文件、539,273,842 bytes 的测试中得到：

| 范围 | 结果 |
| --- | ---: |
| 文件完成数 | 256 / 256 |
| 首批 UploadBatch 创建到最后文件终态 | 约 85.46 s |
| worker 持久化吞吐 | 约 6.02 MiB/s |
| 包含客户端 RustFS 暂存启动的端到端吞吐 | 约 5.90 MiB/s |

吞吐定义为：

```text
MiB/s = 文件总字节数 / 1,048,576 / 端到端耗时（秒）
```

应使用冷的、批内 SHA-256 唯一的真实文件进行基准测试。合成 fixture、重复内容、已经存在
于数据库或 RustFS 的对象都不能代表导入吞吐。

## 3. 参数调整

### 3.1 MolOP 进程数和 native threads

`TRICYCLE_MOLOP_BATCH_N_JOBS=-1` 使用 worker 容器可见的全部 CPU 核。专用算力主机可以使用
该值；API、PostgreSQL 或其他任务与 worker 共机时，改用正整数为它们预留 CPU。

每个 MolOP 进程都应保持：

```dotenv
OMP_NUM_THREADS=1
OPENBLAS_NUM_THREADS=1
MKL_NUM_THREADS=1
```

不要通过把 native thread 设为 4、8 或更高来代替增加文件级进程。否则多个 MolOP 进程会
过度订阅，通常表现为 CPU 利用率下降、上下文切换增加和解析时间变长。

先在容器内确认可见核数：

```bash
docker exec reaction-database-compute-upload-worker-1 nproc
docker exec reaction-database-compute-upload-worker-1 python -c \
  'import os; print(os.cpu_count())'
```

容器不得被意外设置 CPU 配额：

```bash
docker inspect --format \
  '{{.Name}} quota={{.HostConfig.CpuQuota}} period={{.HostConfig.CpuPeriod}} nano={{.HostConfig.NanoCpus}} cpuset={{.HostConfig.CpusetCpus}}' \
  reaction-database-compute-upload-worker-1
```

如果这些值限制了 worker，先修正 Docker Compose、Docker Desktop VM 或编排平台的资源限制，
再调应用参数。增加 MolOP worker 数不能突破容器配额。

### 3.2 预取窗口

`TRICYCLE_UPLOAD_WORKER_PREFETCH_FILES` 只控制 staged 文件提前进入共享 dispatcher 的数量，
不创建更多进程，也不决定数据库事务大小。

推荐公式：

```text
prefetch_files = 2～4 × molop_process_count
```

24 核主机可从 48、72 或 96 开始。大文件、内存紧张时使用 2 倍；解析速度波动明显、数据库
写入较慢时使用 4 倍。超过 4 倍通常只增加内存和旧任务占用，不会自动提升吞吐。

### 3.3 持久化微批

两个参数共同决定提交边界，任一条件先达到就提交：

```dotenv
TRICYCLE_UPLOAD_WORKER_PERSISTENCE_BATCH_FILES=64
TRICYCLE_UPLOAD_WORKER_PERSISTENCE_FRAME_LIMIT=2048
```

调参建议：

| 数据库/文件特征 | 文件上限 | 帧上限 |
| --- | ---: | ---: |
| 开发机或低内存数据库 | 8～16 | 128～256 |
| 普通专用算力主机 | 32 | 512～1024 |
| 当前 24 核高吞吐配置 | 64 | 2048 |
| 帧非常密集或锁/内存有压力 | 16～32 | 512～1024 |

微批太小会重复进行几何、拓扑、反应绑定和事务提交；微批太大则会增加事务锁持有时间、
PostgreSQL 内存、失败重试范围和查询等待时间。当前 64/2048 在 256 个真实文件上成功完成，
但更小内存的数据库应从 32/1024 开始。

每次只改一个边界，观察 `persist_write_ms`、`flush_ms`、事务耗时、锁等待和失败率。

### 3.4 PostgreSQL 连接池

连接池大小不应简单等于 MolOP 进程数。MolOP 是多进程解析资源，而 project/user 写入由消费者
串行执行；额外连接主要用于领取、租约、状态完成、profile 刷新、项目统计和 API 查询。

连接数预算应满足：

```text
所有 API/worker pool 总和 + migration/maintenance/admin 保留连接
  < PostgreSQL max_connections
```

24 核单 worker 的起始值可以是 `24 + 24`，但如果 API 与 worker 共用数据库，必须把 API、
监控和管理员连接一起算入。不要为了填满连接池而无限增大 `max_connections`；连接数过多会
增加内存和上下文切换，不能替代索引或批量写入。

数据库主机应使用本地 SSD/NVMe，并尽量让算力主机与 PostgreSQL 处于低延迟内网。跨公网、
普通 NFS/SMB 或高延迟 VPN 会把每个批量 SQL 的网络等待放大。

### 3.5 RustFS 与本地导入器

RustFS 并发只负责把 bytes 暂存到对象存储：

```dotenv
TRICYCLE_UPLOAD_MAX_CONCURRENCY=8
IMPORT_PIPELINE_WINDOW_FILES=64
IMPORT_STREAM_QUEUE_SIZE=64
```

本地导入器的 `IMPORT_PIPELINE_WINDOW_FILES` 和 `IMPORT_STREAM_QUEUE_SIZE` 是暂存/发现阶段
的背压，不是 MolOP 并发，也不是 PostgreSQL 微批。`IMPORT_COMMIT_BATCH_FILES` 仅为旧 CLI
调用保留，当前不控制 worker 的解析或提交。

当 RustFS 暂存阶段占据端到端时间时，先检查算力主机到 RustFS 的网络、对象存储磁盘和单对象
吞吐，再把 `TRICYCLE_UPLOAD_MAX_CONCURRENCY` 从 8 调到 12 或 16。不要同时无限增大 RustFS
上传并发和 MolOP 进程数；它们会争用网络、内存和文件描述符。

## 4. PostgreSQL 写入和维护

### 4.1 不要关闭完整溯源

```dotenv
TRICYCLE_MOLOP_CAPTURE_SOURCE_EVIDENCE=true
```

segment 边界、frame role、source locator、source span 和 block hash 是查询、部分成功保留、
重解析替换以及错误诊断所需的证据。关闭它可能看起来更快，但会产生不可恢复的数据缺口，
不属于合法性能优化。

### 4.2 延迟 profile 可见性物化

迁移 `0055_defer_profile_visibility` 为批量持久化提供事务级延迟开关：

```sql
SET LOCAL tricycle.defer_profile_source_visibility = 'on';
```

这样不会对每个 frame、ingestion 或 profile source 立即重算完整 profile 图；事务结束后由统一
worker 在队列排空时刷新 dirty thermodynamic profiles，并执行项目级 `ANALYZE`。不要直接删除
触发器或永久关闭可见性更新，否则会留下旧 profile 状态。

### 4.3 统计信息

批量删除、批量导入、全量重解析或大规模替换结束后，应在任务边界触发一次 `ANALYZE`，而不是
每个文件完成后执行。worker 队列排空会执行该维护；手工维护时可使用：

```sql
ANALYZE public.artifact_file;
ANALYZE public.artifact_ingestion;
ANALYZE public.parse_revision;
ANALYZE public.calculation_segment;
ANALYZE public.calculation_frame;
ANALYZE public.geometry;
```

如果查询仍然在批量变更后变慢，先确认统计信息更新时间和表膨胀，再检查执行计划；不要先把
API 查询超时无限放大。保持 `synchronous_commit=on` 作为默认正确性配置；关闭它可能降低提交
等待，但会扩大故障时丢失最近事务的范围。

## 5. 部署和验证

在专用算力主机上把配置写入 `.env`，然后重新创建 API 和 worker，使环境变量真正生效：

```bash
docker compose build --pull=false api
docker compose up -d --no-deps --force-recreate api upload-worker
docker compose ps api upload-worker
```

`--no-deps` 适用于 PostgreSQL/RustFS 已由数据服务器或另一个 Compose project 提供的部署，
避免重新启动不存在的本地依赖。代码或 migration 更新后执行：

```bash
uv run --frozen alembic upgrade head
uv run --frozen alembic current
```

验证容器内实际生效值，而不是只看宿主机 `.env`：

```bash
docker exec reaction-database-compute-upload-worker-1 python -c \
  'import os; from tricycle_reaction_db.application.services.artifact_uploads import molop_process_worker_count; from tricycle_reaction_db.core.config import get_settings; s=get_settings(); print({"cpu": os.cpu_count(), "molop": molop_process_worker_count(), "prefetch": s.upload_worker_prefetch_files, "batch_files": s.upload_worker_persistence_batch_files, "frame_limit": s.upload_worker_persistence_frame_limit, "poll": s.upload_worker_poll_interval_seconds})'

docker compose logs --no-color --since 5m upload-worker \
  | rg 'persistence microbatch|cycle parsed|slow database query|targeted database statistics'
```

修改 `.env` 后必须重启或重建容器。

## 6. 256 文件基准测试

基准测试必须使用目标数据库和 RustFS 中尚未出现的 256 个真实文件。源目录、state file 和
日志输出分开，避免把 benchmark 产生的 JSONL 当成计算文件：

```bash
uv run --frozen tricycle-import-artifacts \
  --project-id '<project-uuid>' \
  --user-id '<authorized-user-uuid>' \
  --state-file /tmp/artifact-import-256.state.jsonl \
  --pipeline-window-files 64 \
  --stream-queue-size 96 \
  /data/cold-calculations \
  > /tmp/artifact-import-256.stage.jsonl
```

记录 CLI 开始暂存、第一批 `created_at`、最后一个批次进入终态，以及 profile 刷新和 `ANALYZE`
完成时间。持久化吞吐使用第一批创建到最后终态；端到端吞吐使用 CLI 开始到最后终态。数据库
查询应确认 256 个 item 全部为 `succeeded`，而不是只看 CLI 的 `queued`：

```sql
SELECT status, count(*)
FROM upload_batch_item
WHERE batch_id = ANY(:batch_ids)
GROUP BY status
ORDER BY status;
```

同时记录 worker 的 `parse_wall_ms`、`parse_sum_ms`、`parse_overlap`，每个微批的 `preload_ms`、
`write_ms`、`flush_ms`、`reconcile_ms`，以及 PostgreSQL 锁/连接、RustFS 延迟、worker RSS、
MolOP 子进程数量和容器 CPU。

## 7. 按现象调参

| 现象 | 优先检查 | 调整方向 |
| --- | --- | --- |
| `parse_overlap` 明显低于 MolOP worker 数 | 预取、CPU 配额、RustFS 领取延迟 | 预取提高到 worker 数的 3～4 倍；确认无 quota |
| CPU 很低且解析任务有空档 | poll interval、claim 查询、worker 副本 | poll 调到 `0.1`；确认运行新镜像 |
| `write_ms`/`flush_ms` 占大头且无锁等待 | 微批太小 | 16/256 -> 32/1024 -> 64/2048 |
| 超时或 `max_locks_per_transaction` | 微批过大或帧过密 | 降到 32/1024 或 16/512 |
| geometry 等价匹配很慢 | 索引和统计信息 | 先 ANALYZE，再检查执行计划 |
| RustFS 占据端到端时间 | 网络、磁盘、PUT 并发 | 先修对象存储，再提高 PUT slots |
| profile 刷新占据队列尾部 | dirty profile 数量 | 保持排空时批量刷新 |
| 失败率升高 | MolOP、内存、statement timeout | 保留 evidence；降低微批或提高文件 timeout |

每次只改变一个变量，并用同一类冷文件重复测试。吞吐提高但失败率、查询延迟或数据完整性
恶化时，该配置不算更优。

## 8. 发布前检查清单

- [ ] 只有一个生产 `upload-worker` 副本，且 API/worker 使用同一镜像 digest。
- [ ] MolOP 进程数匹配容器实际 CPU；native threads 全为 `1`。
- [ ] `TRICYCLE_MOLOP_CAPTURE_SOURCE_EVIDENCE=true`。
- [ ] 预取窗口、微批文件上限和帧上限已写入运行环境并在容器内验证。
- [ ] PostgreSQL 连接池总预算低于 `max_connections`，并预留维护连接。
- [ ] PostgreSQL 和 RustFS 使用低延迟网络及可靠存储，RustFS bucket 保持私有。
- [ ] migration 已到 head，尤其是 `0055_defer_profile_visibility`。
- [ ] 256 个真实冷文件测试中所有 item 都是 `succeeded`，并记录持久化和端到端吞吐。
- [ ] 批量导入/重解析/删除结束后执行了 profile 刷新和项目级 `ANALYZE`。
- [ ] 失败隔离、部分成功和重解析替换测试仍然通过。
