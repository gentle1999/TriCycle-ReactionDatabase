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

```bash
IMPORT_MODE=development \
IMPORT_PROJECT_ID=00000000-0000-7000-8000-000000000201 \
IMPORT_ROOTS='/data/archive/reactions /data/archive/supplemental' \
IMPORT_STATE_FILE=.tmp/artifact-import.jsonl \
IMPORT_PIPELINE_WINDOW_FILES=128 \
IMPORT_COMMIT_BATCH_FILES=16 \
IMPORT_STREAM_QUEUE_SIZE=128 \
make import-artifacts
```

The append-only JSONL state file makes import resumable by path, size, mtime,
and SHA-256. Source changes are re-imported. Use `--dry-run` to scan only.
Production import requires `--user-id` or `IMPORT_USER_ID` belonging to a user
with `artifact:upload` permission on the project.

`--pipeline-window-files` is the candidate pool (default `128`),
`--commit-batch-files` is only the completed-result persistence microbatch
(default `16`), and `--stream-queue-size` bounds discovery/fingerprinting
buffering (default `128`). Keep the candidate pool larger than active workers.
A parse timeout fails only the affected file; good frames in a partially parsed
file are persisted and a no-frame file becomes `filtered`, not successful.

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
