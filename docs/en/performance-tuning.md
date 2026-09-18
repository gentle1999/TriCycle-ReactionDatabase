# High-performance import configuration

> 中文版：[高性能导入配置指南](../performance-tuning.md)。

This guide describes settings and measurements for high-throughput calculation-file
ingestion. The goal is to keep RustFS staging, the shared MolOP pool, and PostgreSQL
persistence in one continuous pipeline without sacrificing source evidence,
per-file failure isolation, or replacement semantics during reparse.

## 1. Performance boundary

```text
local / browser / remote / MCP
        |
        v
RustFS staging + UploadBatch = staged
        |
        v
one upload-worker
        |
        +--> shared MolOP spawn pool (continuously refilled)
        |
        +--> parsed-result queue
                    |
                    v
            project/user persistence consumer
                    |
                    v
            PostgreSQL microbatch
                    |
                    v
            queue drain: profile refresh + project ANALYZE
```

`UploadBatch` is a client upload/progress boundary, not a parser or database
transaction boundary. A single-file upload is staged in RustFS and then claimed
by the worker; API, MCP, and the local importer must not start MolOP directly.

Run one production `upload-worker` replica. Each replica creates its own MolOP
pool, claim loop, and persistence consumer. Additional replicas do not merge
pools; they increase CPU use, database connections, and lock contention. The API
can scale horizontally while the parser worker remains a dedicated compute
service.

## 2. Validated high-throughput starting point

The following profile was measured on a deployment with 24 visible CPU cores,
remote PostgreSQL/RustFS, and one upload-worker. It is a starting point for a
dedicated compute host, not a universal optimum:

```dotenv
TRICYCLE_MOLOP_BATCH_N_JOBS=-1
OMP_NUM_THREADS=1
OPENBLAS_NUM_THREADS=1
MKL_NUM_THREADS=1

TRICYCLE_UPLOAD_WORKER_PREFETCH_FILES=96
TRICYCLE_UPLOAD_WORKER_POLL_INTERVAL_SECONDS=0.1
TRICYCLE_UPLOAD_WORKER_PERSISTENCE_BATCH_FILES=64
TRICYCLE_UPLOAD_WORKER_PERSISTENCE_FRAME_LIMIT=2048

TRICYCLE_DATABASE_POOL_SIZE=24
TRICYCLE_DATABASE_MAX_OVERFLOW=24
TRICYCLE_DATABASE_POOL_TIMEOUT_SECONDS=60
TRICYCLE_UPLOAD_MAX_CONCURRENCY=8

TRICYCLE_MOLOP_CAPTURE_SOURCE_EVIDENCE=true
TRICYCLE_MOLOP_PARALLEL_FRAME_PERSISTENCE=true
```

With 256 real Gaussian/ORCA files (539,273,842 bytes), this profile achieved:

| Scope | Result |
| --- | ---: |
| Files | 256 / 256 succeeded |
| First batch creation to last terminal item | about 85.46 s |
| Worker persistence throughput | about 6.02 MiB/s |
| End-to-end throughput including staging startup | about 5.90 MiB/s |

Calculate throughput as:

```text
MiB/s = total source bytes / 1,048,576 / elapsed seconds
```

Use 256 cold, content-unique real files. Synthetic fixtures, duplicate content,
or objects already present in PostgreSQL/RustFS do not measure ingestion capacity.

## 3. Tune by host size

### 3.1 MolOP processes and native threads

`TRICYCLE_MOLOP_BATCH_N_JOBS=-1` uses every CPU core visible to the worker
container. Use it on a dedicated compute host; use a positive value when the API,
PostgreSQL, or other services share the host.

Keep one native thread per MolOP process:

```dotenv
OMP_NUM_THREADS=1
OPENBLAS_NUM_THREADS=1
MKL_NUM_THREADS=1
```

Do not replace file-level concurrency with native thread multiplication. Setting
these values to 4 or 8 makes several MolOP processes oversubscribe the host and
often lowers throughput.

Verify the container's actual CPU view and quota:

```bash
docker exec reaction-database-compute-upload-worker-1 nproc
docker exec reaction-database-compute-upload-worker-1 python -c \
  'import os; print(os.cpu_count())'
docker inspect --format \
  '{{.Name}} quota={{.HostConfig.CpuQuota}} period={{.HostConfig.CpuPeriod}} nano={{.HostConfig.NanoCpus}} cpuset={{.HostConfig.CpusetCpus}}' \
  reaction-database-compute-upload-worker-1
```

Fix Compose, Docker Desktop, or scheduler limits before changing application
settings. More MolOP processes cannot overcome a container CPU quota.

### 3.2 Prefetch window

`TRICYCLE_UPLOAD_WORKER_PREFETCH_FILES` controls how many staged files can be
admitted ahead of the shared dispatcher. It creates no processes and does not
set the database transaction size.

Start with:

```text
prefetch_files = 2 to 4 × MolOP process count
```

For 24 cores, use 48, 72, or 96. Use 2x for large files or tight memory, and
4x when parse time varies and PostgreSQL persistence is slower. More than 4x
usually adds memory pressure and stale work rather than useful throughput.

### 3.3 Persistence microbatches

The first limit reached commits the microbatch:

```dotenv
TRICYCLE_UPLOAD_WORKER_PERSISTENCE_BATCH_FILES=64
TRICYCLE_UPLOAD_WORKER_PERSISTENCE_FRAME_LIMIT=2048
```

Starting points:

| Database/file profile | Files | Frames |
| --- | ---: | ---: |
| Development or low memory | 8-16 | 128-256 |
| Ordinary dedicated compute host | 32 | 512-1024 |
| Current 24-core throughput profile | 64 | 2048 |
| Dense frames or lock/memory pressure | 16-32 | 512-1024 |

Small batches repeat geometry, topology, reaction-binding, and commit work.
Large batches increase transaction lock duration, PostgreSQL memory use, retry
blast radius, and query wait time. The 64/2048 profile was successful on the
measured host; smaller databases should start at 32/1024.

Change one limit at a time and watch `persist_write_ms`, `flush_ms`, transaction
duration, lock waits, and failure rate.

### 3.4 PostgreSQL pool

The pool size is not the MolOP process count. MolOP processes parse files, while
the project/user consumer serializes writes; additional connections cover claim,
lease, completion, profile refresh, statistics, and API traffic.

Budget connections as:

```text
all API/worker pools + maintenance/admin reserve < PostgreSQL max_connections
```

`24 + 24` is a starting point for a 24-core single-worker host. Include all API
nodes, monitoring, migrations, and administration before increasing it. More
connections do not replace indexes or bulk writes and can increase memory use.

Use local SSD/NVMe for PostgreSQL and a low-latency private network between the
compute host and PostgreSQL. Public networks, ordinary NFS/SMB, and high-latency
VPN links magnify every bulk SQL round trip.

### 3.5 RustFS and local staging

RustFS concurrency only stages bytes:

```dotenv
TRICYCLE_UPLOAD_MAX_CONCURRENCY=8
IMPORT_PIPELINE_WINDOW_FILES=64
IMPORT_STREAM_QUEUE_SIZE=64
```

The `IMPORT_*` window and queue provide staging/discovery backpressure. They do
not increase MolOP concurrency or define PostgreSQL transactions.
`IMPORT_COMMIT_BATCH_FILES` remains only for old CLI compatibility.

If staging dominates end-to-end time, inspect the network, object-store disk,
and PUT latency before raising `TRICYCLE_UPLOAD_MAX_CONCURRENCY` to 12 or 16.
Do not grow RustFS and MolOP concurrency without checking memory, network, and
file-descriptor headroom.

## 4. PostgreSQL writes and maintenance

### 4.1 Keep complete provenance enabled

```dotenv
TRICYCLE_MOLOP_CAPTURE_SOURCE_EVIDENCE=true
```

Segment boundaries, frame roles, source locators, spans, and block hashes are
required for queries, partial-success preservation, reparse replacement, and
diagnostics. Turning this off is data loss, not a performance optimization.

### 4.2 Defer profile visibility materialization

Migration `0055_defer_profile_visibility` provides a transaction-scoped switch:

```sql
SET LOCAL tricycle.defer_profile_source_visibility = 'on';
```

The worker avoids rebuilding the whole profile graph for every frame, ingestion,
or profile-source row. At queue drain it refreshes dirty thermodynamic profiles
and runs project-level `ANALYZE`. Do not drop the triggers or permanently disable
visibility updates.

### 4.3 Statistics

After bulk delete, bulk import, full reparse, or large replacement, refresh
statistics at the operation boundary, not once per file. The worker does this at
queue drain. Manual maintenance can use:

```sql
ANALYZE public.artifact_file;
ANALYZE public.artifact_ingestion;
ANALYZE public.parse_revision;
ANALYZE public.calculation_segment;
ANALYZE public.calculation_frame;
ANALYZE public.geometry;
```

If queries remain slow after a large mutation, check statistics freshness and
table bloat before raising API timeouts. Keep `synchronous_commit=on` by default;
disabling it may reduce commit latency but expands the amount of work lost during
a failure.

## 5. Deploy and verify

Put the profile in `.env`, then recreate API and worker so the environment is
actually loaded:

```bash
docker compose build --pull=false api
docker compose up -d --no-deps --force-recreate api upload-worker
docker compose ps api upload-worker
```

For a separate data-server deployment, `--no-deps` prevents Compose from trying
to start local PostgreSQL/RustFS services. Apply migrations first:

```bash
uv run --frozen alembic upgrade head
uv run --frozen alembic current
```

Verify the running values, not only the host `.env`:

```bash
docker exec reaction-database-compute-upload-worker-1 python -c \
  'import os; from tricycle_reaction_db.application.services.artifact_uploads import molop_process_worker_count; from tricycle_reaction_db.core.config import get_settings; s=get_settings(); print({"cpu": os.cpu_count(), "molop": molop_process_worker_count(), "prefetch": s.upload_worker_prefetch_files, "batch_files": s.upload_worker_persistence_batch_files, "frame_limit": s.upload_worker_persistence_frame_limit, "poll": s.upload_worker_poll_interval_seconds})'

docker compose logs --no-color --since 5m upload-worker \
  | rg 'persistence microbatch|cycle parsed|slow database query|targeted database statistics'
```

After changing `.env`, a container restart/recreate is required.

## 6. Benchmark 256 files

Use 256 real files that are absent from both the target database and RustFS.
Keep the source directory, state file, and logs separate:

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

Record CLI staging start, the first batch `created_at`, the last terminal item,
and profile/ANALYZE completion. Use first-batch creation to last terminal time
for worker throughput and CLI start to last terminal time for end-to-end
throughput. Confirm all 256 items are `succeeded`, rather than trusting a
`queued` CLI result:

```sql
SELECT status, count(*)
FROM upload_batch_item
WHERE batch_id = ANY(:batch_ids)
GROUP BY status
ORDER BY status;
```

Also record parse overlap, persistence `preload_ms`/`write_ms`/`flush_ms`,
PostgreSQL locks and connections, RustFS latency, worker RSS, child-process
count, and container CPU.

## 7. Tune from symptoms

| Symptom | Check first | Direction |
| --- | --- | --- |
| Low `parse_overlap` | prefetch, CPU quota, RustFS claim latency | Use 3-4x prefetch; remove quota |
| Low CPU and gaps between tasks | poll interval, claims, active worker replicas | Use `0.1`; verify the new image |
| `write_ms`/`flush_ms` dominates without locks | microbatch too small | 16/256 -> 32/1024 -> 64/2048 |
| Timeout or `max_locks_per_transaction` | microbatch too large/dense | Reduce to 32/1024 or 16/512 |
| Geometry equivalence queries are slow | indexes and statistics | Run ANALYZE, then inspect plans |
| RustFS dominates end-to-end time | network, disk, PUT concurrency | Fix object storage, then raise PUT slots |
| Profile refresh dominates the tail | dirty profile count | Keep refresh batched at queue drain |
| Failure rate rises | MolOP, memory, statement timeout | Keep evidence; reduce batch or raise file timeout |

Change one variable at a time and repeat with the same cold-file class. A faster
run with worse failure rate, query latency, or data completeness is not a better
configuration.

## 8. Release checklist

- [ ] One production `upload-worker` replica; API and worker use the same image digest.
- [ ] MolOP process count matches actual container CPUs; native threads are all `1`.
- [ ] `TRICYCLE_MOLOP_CAPTURE_SOURCE_EVIDENCE=true`.
- [ ] Prefetch and persistence limits are present in the container and verified.
- [ ] Total PostgreSQL pool budget is below `max_connections`, with maintenance reserve.
- [ ] PostgreSQL and RustFS use low-latency networking and reliable storage; the bucket is private.
- [ ] Migrations are at head, including `0055_defer_profile_visibility`.
- [ ] A 256-file real cold-data run has 256 `succeeded` items and recorded throughput.
- [ ] Profile refresh and project-level `ANALYZE` ran after the import/reparse/delete boundary.
- [ ] Partial-success, failure-isolation, and reparse-replacement tests still pass.
