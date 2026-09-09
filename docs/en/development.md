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

## Query and Parsing Budgets

The default limits are documented in `.env.example`. In particular:

| Setting | Default | Meaning |
| --- | ---: | --- |
| `TRICYCLE_QUERY_STATEMENT_TIMEOUT_MS` | `15000` | PostgreSQL statement budget per connection |
| `TRICYCLE_SLOW_QUERY_THRESHOLD_MS` | `500` | Slow-query log threshold; parameters are redacted |
| `TRICYCLE_UPLOAD_MAX_CONCURRENCY` | `8` | Concurrent HTTP upload requests per API process |
| `TRICYCLE_UPLOAD_WORKER_CONCURRENCY` | `2` | Concurrent MolOP files handled by the durable upload worker |
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
only its byte source differs. It recursively reads files, writes verified raw
objects, and parses calculation outputs with the ordinary MolOP path.

### File flow and process-pool model

The sequence below shows the main path for local `tricycle-import-artifacts`.
File tasks within one pipeline window advance concurrently; the inner loop
represents each file in that window and is not a serial execution model.

```mermaid
sequenceDiagram
    autonumber
    participant I as Import CLI
    participant F as Fingerprint pool
    participant Q as Candidate queue
    participant U as ArtifactUploadService
    participant R as RustFS
    participant G as File-slot gate
    participant P as File-local parser
    participant W as Persistence consumer
    participant D as PostgreSQL
    participant C as JSONL checkpoint

    Note over F: ThreadPoolExecutor, internal cap 32
    Note over G,P: n_jobs file slots; one spawn ProcessPoolExecutor(max_workers=1) per file
    Note over P: OMP/OPENBLAS/MKL are normally set to 1 in the child

    I->>F: Discover paths and compute SHA-256
    F->>Q: Enqueue candidate
    loop Each pipeline window
        Q->>U: Provide candidate window
        Note right of Q: IMPORT_PIPELINE_WINDOW_FILES
        loop Each file in the window (concurrent)
            U->>R: Write and verify raw object
            R-->>U: Object ready
            U->>G: Wait for and acquire file slot
            G->>P: Start isolated child process
            P->>P: MolOP parse + frame post-processing
            alt Normal completion
                P-->>G: Return frames/diagnostics
                G-->>U: Release file slot
                P-->>W: Put bounded result
            else Timeout or parse failure
                P-->>G: Raise timeout/error
                G->>P: Terminate only this file child
                G-->>U: Release file slot
                P-->>W: Put file failure result
            end
        end
        W->>W: Accumulate completed results
        opt IMPORT_COMMIT_BATCH_FILES reached
            W->>D: Persist completed-result microbatch
            D-->>W: Commit
            W->>C: Append status and fsync
        end
        U-->>Q: Refill with the next candidate
    end
```

Read the boundaries in the diagram as follows:

- The fingerprint pool only discovers files and reads SHA-256. Its internal
  cap is `32`; it is not the MolOP parser pool. `IMPORT_STREAM_QUEUE_SIZE`
  bounds the buffer from fingerprinting into the candidate window.
- `TRICYCLE_MOLOP_BATCH_N_JOBS` is a file-level admission semaphore: it limits
  how many files may enter parsing at once. Waiting for a slot does not consume
  the per-file parse timeout.
- After a file acquires a slot, the production path creates a file-local,
  `spawn`-based `ProcessPoolExecutor(max_workers=1)`. Thus `n_jobs=16` means
  at most 16 file child processes are active; it does not create 16 children
  inside every file task. A timeout terminates only that file child and frees
  its slot, while other files continue.
- `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, and `MKL_NUM_THREADS` bound native
  threads inside the child and should normally all be `1`. The candidate
  window and native-thread counts do not replace file-level slots.
- Each import batch has one bounded persistence consumer. Its result queue and
  `IMPORT_COMMIT_BATCH_FILES` provide database backpressure. A checkpoint is
  appended and `fsync`ed after the microbatch commit, so one file failure does
  not roll back already committed files.

Browser and remote API uploads skip the CLI fingerprint pool and local
candidate queue: the API stores bytes in RustFS and marks the item `staged`,
then the independent `upload-worker` claims jobs using
`TRICYCLE_UPLOAD_WORKER_CONCURRENCY` and enters the same file-level parser
path. The worker-concurrency limit and `TRICYCLE_MOLOP_BATCH_N_JOBS` parser-slot
limit are different controls and must not simply be multiplied.

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
- Keep `TRICYCLE_MOLOP_CAPTURE_SOURCE_EVIDENCE=true` and
  `TRICYCLE_MOLOP_PARALLEL_FRAME_PERSISTENCE=true`. The first preserves frame
  role/source-locator evidence; the second enables batched persistence and
  throughput. Do not disable evidence just for a short-term speed gain.

Browser and remote API uploads use the independent durable `upload-worker`, so
do not confuse its controls with the local `IMPORT_*` variables. Keep
`TRICYCLE_UPLOAD_MAX_CONCURRENCY=8` for HTTP byte intake,
`TRICYCLE_UPLOAD_WORKER_CONCURRENCY=2` for active durable files, and
`TRICYCLE_UPLOAD_WORKER_STATEMENT_TIMEOUT_MS=120000` as the starting point. A
dedicated compute host may use `TRICYCLE_MOLOP_BATCH_N_JOBS=16`, but start the
durable worker at `2` and increase it only after checking CPU, memory, and
database write latency; the limits are separate and must not simply be
multiplied.

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
IMPORT_ROOTS='/data/archive/reactions /data/archive/supplemental' \
IMPORT_STATE_FILE=.tmp/artifact-import.jsonl \
IMPORT_PIPELINE_WINDOW_FILES=64 \
IMPORT_COMMIT_BATCH_FILES=16 \
IMPORT_STREAM_QUEUE_SIZE=64 \
make import-artifacts
```

The append-only JSONL state file makes import resumable by path, size, mtime,
and SHA-256. Source changes are re-imported. Use `--dry-run` to scan only.
Production import requires `--user-id` or `IMPORT_USER_ID` belonging to a user
with `artifact:upload` permission on the project.

For `calculation_output`, unambiguous JSON/CSV/TSV/YAML/TOML, structure, and
Markdown sidecars are skipped before MolOP. Unknown suffixes remain admissible
for vendor-specific quantum-chemistry formats. Use repeated `--include-suffix`
options for a local allowlist or `--exclude-suffix` for additional exclusions.

`--pipeline-window-files` is the candidate pool (default `64`),
`--commit-batch-files` is only the completed-result persistence microbatch
(default `16`), and `--stream-queue-size` bounds discovery/fingerprinting
buffering (default `64`). Preparation transactions are bounded separately so a
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
