# Development Environment

[中文](../development.md) | [Documentation index](README.md)

## Prerequisites

- Python 3.12 and `uv 0.9` or later
- Node.js 20 or later and npm
- Docker Engine and Docker Compose
- Linux/amd64 for the supported local baseline

`.python-version` and `uv.lock` pin the interpreter and Python dependencies.
The repository-local `.uv-cache/` keeps `uv run` usable in shells where the
user cache is not writable; it is disposable and ignored by Git.

## Host Development

Install dependencies once:

```bash
uv sync --python 3.12
npm --prefix frontend ci
```

Use host processes for development. `make dev` starts the API and Vite with hot
reload on loopback and deliberately overrides remote data-service endpoints
that may be present in `.env`:

```bash
make dev
```

Open <http://127.0.0.1:5173/>. `Ctrl-C` stops the API and Vite only. Start the
development PostgreSQL/RDKit, RustFS, and Keycloak services first when needed:

```bash
make infra-up
make migrate
make bootstrap-development
make dev
```

`make dev-stack` performs the same local infrastructure/bootstrap setup and
starts the host development services. Use `make infra-down` to stop containers
without deleting named volumes.

## Database and Object Storage

All schema changes go through Alembic. Do not call
`SQLModel.metadata.create_all()` from application startup or production code.

```bash
docker compose up -d --wait postgres
uv run alembic upgrade head
make bootstrap-development
uv run alembic current
uv run alembic check
```

RustFS stores original calculation files. `make storage-up` starts it; the local
S3 API and console default to `http://127.0.0.1:19000` and
<http://127.0.0.1:19001>. `make test-storage` verifies put/head/get/hash/delete
against the running service. The development credentials are local-only values
from `.env.example`.

## Authentication and API

Development defaults to `TRICYCLE_AUTH_MODE=development` and requires
`make bootstrap-development` to create the fixed user, organization, and
project. A migration creates schema only, never application users or roles.

Production requires `TRICYCLE_ENVIRONMENT=production`,
`TRICYCLE_AUTH_MODE=oidc`, `TRICYCLE_OIDC_ISSUER`,
`TRICYCLE_OIDC_AUDIENCE`, and `TRICYCLE_OIDC_JWKS_URL`. The application validates
external JWTs and stores an `issuer + subject` mapping; it has no local password
database. Browser login uses authorization-code flow, state/nonce, and an
HttpOnly session cookie. Set `TRICYCLE_SESSION_COOKIE_SECURE=true` for HTTPS.

The local frontend proxies `/api`, `/health`, `/docs`, `/graphql`, and
`/nexusx/*` to the host API. API schemas expose stable business data, not RustFS
credentials, raw binary Mols, internal JSON, or `ScientificArray.data`.

### MCP organization, project, and calculation-log operations

An MCP token is a user-level credential. It has no fixed organization, project,
or static scope. Each call resolves the current organization and project
memberships for the token's user, so membership changes take effect without
issuing a new token. The service layer still enforces authorization for every
operation.

| Scope | MCP tools | Permission boundary |
| --- | --- | --- |
| Organization | `list_organizations`, `create_organization` | Lists visible organizations; the creator becomes owner |
| Organization members | `list_organization_members`, `upsert_organization_member`, `remove_organization_member` | Members can list; owners/admins can manage; the last owner cannot be removed or demoted |
| Project | `create_project`, `list_projects`, `get_project`, `update_project` | Creation requires organization owner/admin; updates require project manager or organization admin |
| Project members | `list_project_members`, `upsert_project_member`, `remove_project_member` | Project manager or organization admin; the last project manager is preserved |
| Project invitations | `list_project_invitations`, `create_project_invitation`, `revoke_project_invitation`, `resend_project_invitation`, `accept_project_invitation` | Project manager or organization admin; acceptance still checks the authenticated email |
| Audit | `list_project_audit` | Project manager or organization admin |
| Calculation logs | `upload_calculation_log` | Requires project `artifact:upload`; the request only performs size, authorization, and RustFS staging; MolOP/persistence run asynchronously in the worker |

`upload_calculation_log` accepts standard Base64 in `content_base64` (without a
Data URL prefix). The per-file limit is `TRICYCLE_MAX_UPLOAD_BYTES` (64 MiB by
default). The response contains the durable `UploadBatch` and item in `staged`/
`pending` state. `success=true` means that the raw object reached RustFS and the
parse queue, not that MolOP has completed; read the batch/item later for
`ingestion_status`, `parse_revision_id`, frame counts, and TS inference results.

For an explicit artifact reparse, a normal duplicate upload returns the same
revision, while reparse first deletes every old `ParseRevision`, segment,
frame, TS inference, and affected derived binding, then rebuilds revision 1
from the RustFS source. If parsing or persistence fails, ingestion is marked
`failed`; no obsolete parse result is restored.

To repair historical duplicate materialization, first inspect the candidate
set and then run the resumable shared RustFS/MolOP/persistence path:

```bash
uv run python scripts/reparse_overlapping_artifacts.py --dry-run
uv run python scripts/reparse_overlapping_artifacts.py \
  --batch-size 32 \
  --state-file .tmp/reparse-overlapping-artifacts-clean-first.jsonl
```

The command selects calculation artifacts with more than one `ParseRevision`,
including historical `quarantined` rows. It completes the `clear` phase for
all candidates before beginning `reparse`, and its JSONL manifest/checkpoints
support resume. Failed or partial files remain retryable.

For a known set of files, use the unified ID-based cleanup command. It performs
set-based deletion of every old `ParseRevision` and its revision-owned results
in one authorized database transaction, keeps the ArtifactFile/RustFS source,
and resets the corresponding ingestions to `pending` for automatic pickup by
the upload worker:

```bash
uv run python scripts/clear_artifact_parse_results.py \
  --artifact-id '<artifact-uuid-1>' \
  --artifact-id '<artifact-uuid-2>'

uv run python scripts/clear_artifact_parse_results.py \
  --artifact-id-file .tmp/artifact-ids.txt
```

The ID file accepts whitespace-, comma-, or newline-separated UUIDs and `#`
comments. Duplicate IDs are removed. The command does not delete RustFS
objects or Artifact catalogue rows.

## Query and Parsing Budgets

The default limits are documented in `.env.example`. In particular:

| Setting | Default | Meaning |
| --- | ---: | --- |
| `TRICYCLE_QUERY_STATEMENT_TIMEOUT_MS` | `15000` | PostgreSQL statement budget per connection |
| `TRICYCLE_SLOW_QUERY_THRESHOLD_MS` | `500` | Slow-query log threshold; parameters are redacted |
| `TRICYCLE_UPLOAD_MAX_CONCURRENCY` | `8` | Concurrent HTTP upload requests per API process |
| `TRICYCLE_UPLOAD_WORKER_CONCURRENCY` | `2` | Maximum files in one durable queue claim |
| `TRICYCLE_UPLOAD_WORKER_LEASE_SECONDS` | `3600` | Worker processing lease; heartbeats extend it and expiry permits recovery |
| `TRICYCLE_UPLOAD_CLIENT_LEASE_SECONDS` | `900` | Recovery threshold for an interrupted HTTP staging request |
| `TRICYCLE_UPLOAD_WORKER_POLL_INTERVAL_SECONDS` | `1` | Worker polling interval for staged items and expired leases |
| `TRICYCLE_UPLOAD_WORKER_STATEMENT_TIMEOUT_MS` | `120000` | Independent PostgreSQL statement budget for background parse/persistence; interactive API queries keep `TRICYCLE_QUERY_STATEMENT_TIMEOUT_MS` |
| `TRICYCLE_MOLOP_BATCH_N_JOBS` | `2` | Concurrent file-level MolOP workers |
| `TRICYCLE_MOLOP_FILE_PARSE_TIMEOUT_SECONDS` | `60` | Baseline parse budget for 10 MiB; larger files scale linearly |
| `TRICYCLE_STRUCTURE_CANDIDATE_LIMIT` | `50000` | Limit for paths requiring per-candidate post-processing |

Set `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, and `MKL_NUM_THREADS` to bound
native pools within each file worker. Do not reduce file-worker concurrency just
to control nested native threads. Production must give
`TRICYCLE_MOLOP_BATCH_N_JOBS` an explicit positive bound.

Geometry lists narrow candidates through project catalog and elemental filters
before expensive structure conditions. REST returns `413 query_budget_exceeded`
for a query budget violation, `429 query_rate_limit_exceeded` for rate limiting,
and `503 query_timeout` for a database timeout. GraphQL and MCP use the same
codes in their error envelopes.

## Testing

```bash
make lint
make type
make test
make frontend-check
make frontend-build
```

With infrastructure running, also execute:

```bash
make test-db
make test-storage
make test-infra
```

The database cost gate checks RDKit indexes, Geometry/Frame B-tree plans,
timeouts, and safe connection reuse. Run it only against a disposable or
dedicated development database:

```bash
TRICYCLE_RUN_DATABASE_TESTS=1 uv run pytest -q \
  tests/integration/test_query_cost_database.py \
  tests/integration/test_topology_search.py \
  tests/integration/test_reaction_search.py --no-cov
```

## Local Import of Existing Files

`tricycle-import-artifacts` is the registered project CLI for existing local
files. It uses the same upload service as the browser and remote batch API;
only its byte source differs. It recursively discovers and fingerprints files,
creates an `UploadBatch`, and stages verified raw objects in RustFS. The CLI
does not run MolOP; the independent `upload-worker` claims the staged items and
uses the same shared parser and persistence path as remote uploads.

### Unified file-upload sequence and data flow

The sequence below covers remote single-file uploads, remote batches, the local
`tricycle-import-artifacts` CLI, and MCP calculation-log uploads. Time and data
flow move from top to bottom; the only source difference is where the raw bytes
come from. The service first creates a recoverable `pending` reservation and
batch item in PostgreSQL, then writes and verifies RustFS. Only an available
object whose item is marked `staged` enters the parse queue. When the worker
claims that object and starts MolOP/frame processing, the file-level ingestion
becomes `processing`; an expired lease returns it to `pending`. A
[standalone zoomable version](diagrams/upload-processing-sequence.html) is also
available.

```mermaid
sequenceDiagram
    autonumber
    participant R as Remote client
    participant L as Local import CLI
    participant S as Upload staging service
    participant O as RustFS
    participant D as PostgreSQL
    participant W as upload-worker
    participant M as MolOP process pool
    participant P as Persistence consumer

    Note over R,P: Time and data flow move downward; production keeps one upload-worker instance
    alt Remote single-file or batch upload
        R->>S: POST /api/artifacts, /batch, or MCP calculation log
    else Local existing-file import
        L->>S: tricycle-import-artifacts
    end
    S->>D: Create pending Artifact + UploadBatch Item
    D-->>S: Return recoverable batch/file identity
    S->>O: Write raw bytes and verify SHA-256
    O-->>S: Object available
    S->>D: Mark Item = staged; enter durable parse queue
    S-->>R: 202 + batch/item identity
    S-->>L: Return staging result

    W->>D: Claim staged items and acquire leases
    D-->>W: PROCESSING claim window (up to 64 files)
    loop Each project/user persistence group (groups are serial)
        loop Each file in the group
            W->>O: Read and verify staged raw object
            O-->>W: Return file bytes
            W->>M: Submit MolOP parse task
            M-->>W: Return frames, topology, and reaction evidence
            W->>P: Enqueue result in bounded persistence queue
            alt Result queue is temporarily empty
                P->>D: Persist preload results only; do not commit
            else 32 completed results accumulated
                P->>D: Commit one 32-result persistence microbatch
            end
        end
    end
    P->>D: Claim window ends; commit remaining results
    W->>D: Finalize each UploadBatchItem state
    R->>S: GET batch status / parse result
    S->>D: Read batch, ingestion, and frame state
    D-->>S: SUCCEEDED / PARTIAL / FAILED
    S-->>R: Return final status and result identities
```

Read the boundaries in the diagram as follows:

- The fingerprint pool only discovers files and reads SHA-256. Its internal
  cap is `32`; it is not the MolOP parser pool. `IMPORT_STREAM_QUEUE_SIZE`
  bounds the buffer from fingerprinting into the candidate window.
- `TRICYCLE_MOLOP_BATCH_N_JOBS` is the file-level admission limit for the
  worker's shared MolOP process pool. API, MCP, local CLI, and remote batch
  entry points only stage files in RustFS and the durable queue; they do not
  create parser pools in individual upload sessions.
- `pending` is the recoverable reservation and waiting-queue state. An item
  becomes a parser task only after RustFS write and SHA-256 verification
  succeed and the item changes to `staged`; after the worker claims it, the
  file-level ingestion is reported as `processing`. No upload request calls
  MolOP directly.
- After a worker claims an item, parser and frame work is submitted to one
  reusable, `spawn`-based `ProcessPoolExecutor`. Thus `n_jobs=16` means at most
  16 file tasks execute in that service process; a new pool is not created for
  every artifact or upload session. Completed or failed work lets the queue
  refill. A cancelled or timed-out task releases its admission slot while
  already submitted shared-pool work is drained by the pool.
- `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, and `MKL_NUM_THREADS` bound native
  threads inside the child and should normally all be `1`. The candidate
  window and native-thread counts do not replace file-level slots.
- The staging window provides RustFS backpressure; the worker's claim window
  and bounded persistence batches provide database backpressure. The staging
  checkpoint records batch/item IDs, while final parse status comes from the
  UploadBatch API. One file failure does not roll back other staged or completed
  files.
- The single `upload-worker` instance is the boundary for the shared MolOP pool
  and the single active persistence consumer. API nodes may scale horizontally;
  do not scale upload-worker horizontally unless multiple parser pools and
  persistence consumers are intentionally desired.

Browser, MCP, and remote API uploads skip the CLI fingerprint pool and local
candidate queue: the entry point stores bytes in RustFS and marks the item
`staged`, then the independent `upload-worker` claims a
`TRICYCLE_MAX_BATCH_FILES` (64) window and groups it by project/user for
`ArtifactUploadService.reparse_batch`. That wrapper only reads/verifies
existing objects and delegates to the shared MolOP pool and single persistence
consumer; it does not upload objects again or introduce a second parser.
`TRICYCLE_UPLOAD_MAX_CONCURRENCY` limits RustFS reads,
`TRICYCLE_UPLOAD_WORKER_CONCURRENCY` is retained for legacy pending-ingestion
recovery, and `TRICYCLE_MOLOP_BATCH_N_JOBS` limits shared-pool admission; these
controls must not simply be multiplied.

Keep the remote reparse boundaries separate from parser concurrency: the worker
claims at most 64 staged files, aggregates each project/user microbatch, and
passes the microbatch through `reparse_batch` to `upload_batch`. The client
`UploadBatch` is only a queue/progress boundary, not a persistence boundary:
one-file submissions for the same project/user are merged into one microbatch,
and different project/user microbatches are committed sequentially. Within a
microbatch, the same result queue and single consumer call `persist_parsed_files`
for every 32 parsed results (or when the queue is temporarily empty), and commit
at that bounded persistence boundary; persistence must not wait until all 64
claimed files have parsed. Thus `64` is the claim window and `32` is the commit
microbatch, while actual parser concurrency is controlled only by the shared MolOP pool's
`TRICYCLE_MOLOP_BATCH_N_JOBS` (normally `16` on a dedicated host). The durable
bulk/reparse transaction also uses the previous legacy bulk hot path: reaction-SMILES
topology caching and one set-based Geometry match remain enabled, while later
per-file concrete/logical/reverse reconciliation must not be inserted directly.
Project scope and ownership constraints still apply. Update the architecture guide
and remeasure byte throughput and failure isolation on the same real file set before
changing these boundaries.

### Recommended import settings

Choose a starting profile based on the host. The current deployment benchmark
uses 16 file-level MolOP workers and one native thread per worker. Treat that
as a validated starting point for a compute host, not as a universal optimum:
available CPU cores, memory, storage, and PostgreSQL latency all matter.

| Profile | `TRICYCLE_MOLOP_BATCH_N_JOBS` | `OMP_NUM_THREADS` / `OPENBLAS_NUM_THREADS` / `MKL_NUM_THREADS` | `IMPORT_PIPELINE_WINDOW_FILES` | `IMPORT_STREAM_QUEUE_SIZE` | `IMPORT_COMMIT_BATCH_FILES` |
| --- | ---: | --- | ---: | ---: | ---: |
| Local development or low-resource host | `2` | `1 / 1 / 1` | `16` | `16` | `8–16` |
| Dedicated compute host, throughput first | `16` | `1 / 1 / 1` | `64` | `64` | `16` |
| Memory- or database-constrained host | `4–8` | `1 / 1 / 1` | `32` | `32` | `8` |

For a dedicated compute host, the following is a useful first run:

```bash
IMPORT_MODE=deployment \
OMP_NUM_THREADS=1 \
OPENBLAS_NUM_THREADS=1 \
MKL_NUM_THREADS=1 \
TRICYCLE_MOLOP_BATCH_N_JOBS=16 \
IMPORT_PIPELINE_WINDOW_FILES=64 \
IMPORT_STREAM_QUEUE_SIZE=64 \
IMPORT_COMMIT_BATCH_FILES=16 \
IMPORT_MAX_TRANSIENT_RETRIES=3 \
IMPORT_PROJECT_ID='<project-uuid>' \
IMPORT_USER_ID='<user-uuid>' \
IMPORT_ROOTS='/data/calculations /data/supplemental' \
IMPORT_STATE_FILE=.tmp/artifact-import.jsonl \
make import-artifacts
```

Tune in this order:

- Increase `TRICYCLE_MOLOP_BATCH_N_JOBS` in steps such as `2 → 4 → 8 → 16`,
  measuring the same real file set after each change. Approximate CPU pressure
  is file-worker count multiplied by native threads per worker. Keep all three
  OpenMP/BLAS variables at `1`; do not use nested native pools as a substitute
  for file-level concurrency. Production must use a positive bound, never `-1`.
- Set `IMPORT_PIPELINE_WINDOW_FILES` to roughly four times the parser-worker
  count, and keep it above that count. Use the same value for
  `IMPORT_STREAM_QUEUE_SIZE` as a starting point. These values control
  buffering and prefetch, not parser concurrency; lower them for large files
  or memory pressure.
- Fingerprinting uses a separate thread pool with an internal cap of `32`;
  there is currently no environment variable or CLI flag for it. If the
  fingerprint phase dominates the timings, inspect storage and SHA-256 read
  cost before increasing MolOP parser concurrency.
- `IMPORT_COMMIT_BATCH_FILES` controls persistence transaction/checkpoint
  frequency only. Keep `16` initially, reduce to `8` for lock contention,
  statement timeouts, or database memory pressure, and try `32` only when the
  database has clear headroom.
- Keep `IMPORT_MAX_TRANSIENT_RETRIES=3`. It covers transient deadlocks,
  serialization conflicts, and connection interruptions; raising it does not
  fix a persistent failure.
- `TRICYCLE_MOLOP_FILE_PARSE_TIMEOUT_SECONDS=60` is a 10 MiB baseline that
  scales with source size. It isolates outliers rather than increasing speed;
  raise it for slow storage or many large files, and lower it only after
  checking the resulting failure rate.
- Keep `TRICYCLE_MOLOP_CAPTURE_SOURCE_EVIDENCE=false` for the previous
  high-throughput bulk-import behavior. Set it to `true` for audit imports that
  require frame-role/source-locator, source-span, or block-hash evidence and
  accept the extra cost. Keep `TRICYCLE_MOLOP_PARALLEL_FRAME_PERSISTENCE=true`.

Browser and remote API uploads use the independent durable `upload-worker`, so
do not confuse its controls with the local `IMPORT_*` variables.
`TRICYCLE_UPLOAD_MAX_CONCURRENCY=8` limits RustFS reads and
`TRICYCLE_MAX_BATCH_FILES=64` is the worker claim window; persistence commits
are bounded to 32 results per microbatch;
`TRICYCLE_UPLOAD_WORKER_CONCURRENCY` is only for pending-ingestion recovery.
A dedicated compute host may use `TRICYCLE_MOLOP_BATCH_N_JOBS=16` for the
shared parser pool, subject to CPU, memory, and database write-latency checks.

The worker claim window does not define a database transaction: it does not
serialize 64 files or change the internal 32-result hand-off. Persistence commits
are bounded to that 32-result microbatch. Local CLI
`IMPORT_COMMIT_BATCH_FILES` is retained for compatibility and does not control
worker parsing or persistence. These controls belong to staging backpressure,
parser admission, and worker commit boundaries respectively and must not
substitute for one another.

`TRICYCLE_MAX_UPLOAD_BYTES=64 MiB` is the per-file cap and also applies to local
imports. `TRICYCLE_MAX_BATCH_FILES=64` and
`TRICYCLE_MAX_BATCH_BYTES=512 MiB` protect HTTP batch requests and do not limit
the local import batch. Raise the batch limits to values such as 1024 files /
1 GiB only for a dedicated trusted benchmark or internal bulk client after
validating the reverse-proxy body limit, RustFS, and PostgreSQL capacity. Keep the worker
lease/recovery defaults (`3600` seconds, `900` seconds, and a `1` second poll)
unless there is an operational reason to change them.

```bash
IMPORT_MODE=development \
IMPORT_PROJECT_ID=00000000-0000-7000-8000-000000000201 \
IMPORT_USER_ID=00000000-0000-0000-0000-000000000002 \
IMPORT_ROOTS='/data/archive/reactions /data/archive/supplemental' \
IMPORT_STATE_FILE=.tmp/artifact-import.jsonl \
IMPORT_PIPELINE_WINDOW_FILES=64 \
IMPORT_COMMIT_BATCH_FILES=16 \
IMPORT_STREAM_QUEUE_SIZE=64 \
make import-artifacts
```

For an extracted archive, use manifest mode. Generate the manifest with a
trusted extractor, configure `TRICYCLE_IMPORT_STAGING_ROOT`, and pass an
explicit importing user:

```bash
TRICYCLE_IMPORT_STAGING_ROOT=/data/staging \
uv run tricycle-import-artifacts \
  --project-id 00000000-0000-7000-8000-000000000201 \
  --user-id 00000000-0000-0000-0000-000000000002 \
  --manifest /data/staging/archive.manifest.json
```

Manifest mode registers a durable database ImportJob/ImportJobItem and imports
only entries marked `selected`. Every entry is re-hashed before registration
and startup. The configured staging root is mandatory; traversal, symlinks,
hard links, special files, out-of-root paths, changed bytes, and files not in
the manifest are rejected. Re-registering the same project manifest returns
the existing job. MCP exposes `get_import_status`, `list_import_failures`,
`retry_import_items`, `pause_import`, `resume_import`, and `cancel_import` for
control and audit; it never transfers archive bytes.

The append-only JSONL state file makes import resumable by path, size, mtime,
and SHA-256. Source changes are re-imported. Use `--dry-run` to scan only.
Every import requires `--user-id` or `IMPORT_USER_ID` belonging to a user with
`artifact:upload` permission on the project. There is no implicit development
user fallback.

For `calculation_output`, unambiguous JSON/CSV/TSV/YAML/TOML, structure, and
Markdown sidecars are skipped before MolOP. Unknown suffixes remain admissible
for vendor-specific quantum-chemistry formats. Use repeated `--include-suffix`
options for a local allowlist or `--exclude-suffix` for additional exclusions.

`--pipeline-window-files` is the RustFS staging window (default `64`),
`--commit-batch-files` is a retained compatibility option (default `16`), and
`--stream-queue-size` bounds discovery/fingerprinting buffering (default `64`).
Worker preparation and persistence transactions are bounded separately so a
large request cannot exhaust PostgreSQL advisory-lock shared memory. Deadlocks,
serialization conflicts, connection interruptions, statement timeouts, and
`max_locks_per_transaction` failures are retried with exponential backoff and
adaptive batch splitting; an exhausted file is recorded as failed while other
files continue. Re-running the same state file retries it.
A parse timeout fails only the affected file; good frames in a partially parsed
file are persisted and a no-frame file becomes `filtered`, not successful.
The Makefile equivalents are `IMPORT_INCLUDE_SUFFIXES`,
`IMPORT_EXCLUDE_SUFFIXES` (space-separated suffix lists), and
`IMPORT_MAX_TRANSIENT_RETRIES`.

## Dependency Upgrades

MolOP `>=0.2.12` and MolGR `>=0.1.8` are installed from PyPI. After changing
MolOP, MolGR, OpenBabel, or RDKit, run:

```bash
uv lock --python 3.12
uv sync --python 3.12 --frozen
uv pip check
uv run python -c "import molop, molgr, openbabel, rdkit"
make check
make test-db
```
