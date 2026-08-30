# TriCycle Reaction Database

[English](README.md) | [简体中文](README.zh-CN.md) | [Documentation / 文档](docs/README.md)

TriCycle Reaction Database is a topology-first database and web application for
quantum-chemistry reaction-path calculations. It ingests calculation artifacts,
preserves their raw provenance, reconstructs molecular graphs, stores reusable
geometries, and exposes reaction, structure, and calculation data through a web
UI, REST, GraphQL, and MCP.

The project is in active pre-1.0 development. Database migrations, API behavior,
and ingestion policies are versioned and tested, but downstream integrations
should pin a release rather than assume an unversioned main branch is stable.

## Highlights

- Ingest Gaussian, ORCA, and other calculation outputs supported by
  [MolOP](https://pypi.org/project/molop/), while keeping the original bytes in
  RustFS/S3-compatible object storage.
- Persist every recoverable segment and calculation frame with parser,
  software, method, coordinate, frequency, energy, and array provenance.
- Reconstruct per-frame molecular graphs through MolGR; topology data is taken
  from the reconstruction result rather than inferred from filenames or
  directory layout.
- Deduplicate formula, topology, and geometry facts. Geometry identity includes
  topology, coordinates, total charge, and spin multiplicity; source atom
  correspondence remains preserved within a calculation frame.
- Infer eligible transition-state endpoints from the imaginary mode, retain
  inference evidence, and connect successful endpoints to logical and mapped
  reaction paths without automatically assigning a reaction class.
- Search and browse reactions, mapped reactions, topologies, geometries,
  calculation frames, and artifacts. Structure queries are protected by input,
  candidate, timeout, and rate-limit budgets.
- Inspect 3D geometries in the browser; download geometry-specific XYZ and SDF
  files with charge and multiplicity metadata.
- Support project-scoped authorization, development identities, production OIDC
  login, invitation-based membership, and separate MCP access tokens.

## Scope

TriCycle stores and serves computational chemistry records. It is not an HPC
scheduler, a general reaction-discovery engine, an electronic laboratory
notebook, or a literature/yield database. It does not alter a calculation's
chemical facts to force a desired reaction label.

## Architecture

```text
Calculation file
  -> ArtifactFile (PostgreSQL) + content-addressed raw object (RustFS)
  -> ParseRevision -> CalculationSegment -> CalculationFrame
  -> MolecularFormula -> MolecularTopology -> Geometry
  -> TS endpoint inference -> LogicalReaction -> MappedReaction -> Path nodes

Vue 3 application <-> FastAPI / NexusX <-> PostgreSQL + RDKit cartridge
                                      \-> RustFS (raw artifacts)
```

The chemistry identity axis is `MolecularFormula -> MolecularTopology ->
Geometry`. The provenance axis is `ArtifactFile -> ParseRevision ->
CalculationSegment -> CalculationFrame`. These axes meet at the geometry
assignment, preserving both reusable molecular facts and the calculation frame
that observed them.

## Technology

| Layer | Components |
| --- | --- |
| Backend | Python 3.12, FastAPI, SQLModel, SQLAlchemy, Pydantic, Alembic |
| Chemistry | MolOP, MolGR, RDKit, RDKit PostgreSQL cartridge, MolAlchemy |
| Storage | PostgreSQL 18 with RDKit, RustFS S3-compatible object storage |
| Frontend | Vue 3, Vite, Vue Router, TanStack Vue Query, ChemDoodle |
| Query transports | REST/OpenAPI, NexusX GraphQL, MCP Streamable HTTP, Voyager |
| Operations | Docker Compose, Caddy, Prometheus metrics, GitHub Actions |

## Quick Start

### Prerequisites

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Node.js 20 or later and npm
- Docker Engine with Docker Compose, for the local PostgreSQL/RDKit, RustFS, and
  development Keycloak services

Clone the repository and install the locked dependencies:

```bash
uv sync --python 3.12
npm --prefix frontend ci
```

Start the local infrastructure, apply migrations, create the development
organization/project/user, and run the host API with Vite hot reload:

```bash
make dev-stack
```

Open <http://127.0.0.1:5173/>. The Vite development server proxies `/api`,
`/health`, `/docs`, `/graphql`, and `/nexusx/*` to the host API on port `8000`.
Press `Ctrl-C` to stop the API and frontend; the infrastructure containers and
their data volumes remain available.

If infrastructure is already running, use `make dev`. The main lifecycle
commands are:

```bash
make infra-up                 # PostgreSQL/RDKit, RustFS, development Keycloak
make migrate                  # Alembic upgrade head
make bootstrap-development    # development organization, project, and user
make dev                      # host API + Vite with hot reload
make infra-down               # stop local infrastructure
```

### Full Compose Stack

For the packaged API, frontend, and Caddy deployment instead of host-based
development:

```bash
cp .env.example .env
make stack-up
curl --insecure https://localhost/health/ready
```

`localhost` uses Caddy's internal development CA. Production requires an
external OIDC provider, TLS-verified PostgreSQL/RustFS endpoints, secret
management, and deployment-specific branding. Follow the
[deployment guide](docs/deployment-configuration.md) rather than promoting the
development `.env` values.

## Using the Application

The browser UI provides project switching, artifact upload and reparse status,
reaction and geometry catalogs, structured filters, sorting, cached pagination,
calculation-frame details, 3D geometry inspection, and source file previews.

After signing in, the following endpoints are available through the same origin
in local development:

| Interface | Local path | Intended use |
| --- | --- | --- |
| Web application | `/` | Interactive catalog, upload, and inspection workflows |
| OpenAPI | `/docs` | REST API discovery and request testing |
| Direct-list GraphQL | `/nexusx/graphql` | Small read-only exploratory queries |
| Paginated GraphQL | `/nexusx/paginated-graphql` | Filtered and paginated data access |
| MCP | `/nexusx/mcp/` | MCP client access with a generated MCP token |
| Voyager | `/nexusx/voyager/` | Data-model exploration |

Public artifacts can be explicitly marked public. Project data and write
operations require the corresponding project permission. In production, use
OIDC; the development identity mode exists only to make local development and
automated tests reproducible.

## Importing Existing Files

The browser upload queue and the local importer use the same application upload
service. The CLI differs only in its source: it reads existing paths directly
instead of receiving browser-spooled files.

```bash
IMPORT_MODE=development \
IMPORT_PROJECT_ID=<project-uuid> \
IMPORT_ROOTS='/data/calculations /data/supplemental' \
IMPORT_STATE_FILE=.tmp/artifact-import.jsonl \
IMPORT_PIPELINE_WINDOW_FILES=128 \
IMPORT_COMMIT_BATCH_FILES=16 \
IMPORT_STREAM_QUEUE_SIZE=128 \
make import-artifacts
```

The checkpoint is append-only and makes the import resumable using source path,
size, mtime, and SHA-256. Files that contain no recoverable calculation frames
are retained as filtered artifacts; a failure in one file does not discard
successfully parsed frames or unrelated files.

The importer deliberately separates three controls:

| Control | Default | Purpose |
| --- | --- | --- |
| `TRICYCLE_MOLOP_BATCH_N_JOBS` | `2` | Number of concurrent file-level MolOP workers |
| `IMPORT_PIPELINE_WINDOW_FILES` | `128` | Candidate files available to the parser queue |
| `IMPORT_COMMIT_BATCH_FILES` | `16` | Completed files per persistence/checkpoint microbatch |

Set the pipeline window appreciably above the worker count so a finished worker
can immediately take the next queued file. Do not use the persistence microbatch
size to limit parser concurrency. Keep `OMP_NUM_THREADS`,
`OPENBLAS_NUM_THREADS`, and `MKL_NUM_THREADS` bounded (the supplied development
configuration uses `1`) to avoid nested native-thread oversubscription.

The baseline file timeout is `TRICYCLE_MOLOP_FILE_PARSE_TIMEOUT_SECONDS` for a
10 MiB source. Larger files receive a proportional allowance; a timed-out file
fails independently and releases its worker slot.

For all importer flags, reparse behavior, and production import configuration,
see [Development: direct artifact import](docs/development.md#直接批量导入存量文件).

## Configuration

Copy `.env.example` only when you need to override development defaults or run
the Compose stack. Frontend build-time settings are documented in
`frontend/.env.example`. Never commit real database passwords, S3 keys, SMTP
credentials, OIDC client secrets, session secrets, or MCP tokens.

Frequently changed settings include:

| Setting | Purpose |
| --- | --- |
| `TRICYCLE_DATABASE_URL` | Host-process PostgreSQL connection URL |
| `TRICYCLE_RUSTFS_ENDPOINT_URL` | Host-process S3/RustFS endpoint |
| `TRICYCLE_AUTH_MODE` | `development` locally, `oidc` in production |
| `TRICYCLE_OIDC_*` | OIDC issuer, audience, JWKS, and browser client settings |
| `TRICYCLE_MOLOP_BATCH_N_JOBS` | Bounded file parser worker count |
| `TRICYCLE_MOLOP_FILE_PARSE_TIMEOUT_SECONDS` | Per-file parsing baseline timeout |
| `TRICYCLE_QUERY_STATEMENT_TIMEOUT_MS` | PostgreSQL statement timeout for query traffic |
| `TRICYCLE_STRUCTURE_CANDIDATE_LIMIT` | Upper bound before expensive structure post-processing |
| `VITE_API_PROXY_TARGET` | Vite development API upstream |

For a two-host or production deployment, use the
[deployment and configuration guide](docs/deployment-configuration.md). It
covers network boundaries, private CA bundles, OIDC, Redis-backed rate limits,
Caddy, backups, and service separation.

## Maintenance

Repair historical reaction-participant Geometry links that may have been
missed by an older batch-ingestion ordering:

```bash
# Inspect the expected work without committing it.
uv run tricycle-reconcile-reaction-geometries --dry-run

# Reconcile only missing-binding candidates in batches of 100.
uv run tricycle-reconcile-reaction-geometries --batch-size 100

# Check one mapped reaction directly.
uv run tricycle-reconcile-reaction-geometries \
  --mapped-reaction-id 00000000-0000-7000-8000-000000000000
```

The command is idempotent, fetches candidates in bounded pages, and commits
each reaction independently. Chemical reconciliation runs in a replaceable
worker process, so a native-library crash or `--reaction-timeout-seconds`
expiry is recorded without terminating the remaining scan. Use
`--start-after UUID` to resume a keyset scan and `--scan-all` to check
reactions already excluded by the missing-binding candidate query. The equivalent Make target is
`make reconcile-reaction-geometries`; its optional variables use the
`RECONCILE_*` prefix shown in the Makefile.

## Development and Testing

| Command | What it checks |
| --- | --- |
| `make format` | Format Python source, tests, migrations, and scripts |
| `make lint` | Ruff lint and formatting check |
| `make type` | mypy and pyright |
| `make test` | Default Python test suite |
| `make test-db` | PostgreSQL/RDKit integration tests |
| `make test-storage` | RustFS integration tests |
| `make test-redis` | Redis rate-limit tests |
| `make test-infra` | PostgreSQL/RDKit and RustFS integration tests |
| `make frontend-build` | Vue type-check and production build |
| `make frontend-test-e2e` | Playwright browser tests |
| `make check` | Frontend build, lint, types, and default Python tests |
| `make audit` | Python, npm, and vendored-browser dependency audits |

Install the local browser dependencies before running Playwright on a new host:

```bash
npx --prefix frontend playwright install --with-deps chromium
```

Continuous integration runs linting, type checks, unit tests, Alembic fresh
database checks, PostgreSQL/RDKit and RustFS integration suites, frontend build,
browser tests, deployment configuration validation, and supply-chain audits.

## Repository Layout

```text
src/tricycle_reaction_db/  FastAPI adapters, application services, domain, DB models, ingestion
frontend/                  Vue 3 application and Playwright tests
migrations/                Alembic schema migrations
tests/                     Unit, integration, storage, and contract tests
docs/                      Architecture, development, deployment, and operations guides
infra/                     Caddy, systemd, monitoring, Keycloak, and deployment assets
scripts/                   Validation, benchmark, audit, and operational helpers
```

## Documentation

The documentation index includes paired Chinese and English editions. It also
separates current operating contracts from dated planning and acceptance records.

- [Documentation index / 文档索引](docs/README.md)
- [Development environment and local importer](docs/en/development.md) / [开发环境与本地导入](docs/development.md)
- [Deployment and configuration](docs/en/deployment-configuration.md) / [部署与配置指南](docs/deployment-configuration.md)
- [Data model and storage boundaries](docs/en/data-model.md) / [数据模型与存储边界](docs/data-model.md)
- [Business model](docs/en/business-model.md) / [业务模型](docs/business-model.md)
- [Operations and recovery runbook](docs/en/operations-runbook.md) / [生产运维与恢复 Runbook](docs/operations-runbook.md)
- [Full document map / 完整文档映射](docs/README.md)

## Security

Artifact files can contain proprietary calculation details. Keep RustFS buckets
private, terminate TLS at a trusted edge, and configure OIDC plus Redis-backed
rate limiting before exposing a production deployment. Report a vulnerability
privately to the maintainers; do not place credentials, access tokens, or raw
restricted calculation files in a public issue.

## Contributing

Keep changes scoped and include tests appropriate to their risk. Schema changes
require an Alembic migration; backend changes should preserve the application
service boundary; frontend changes should pass the Vue build and relevant
Playwright coverage. Read [the development guide](docs/development.md) before
changing ingestion, storage, authentication, or deployment behavior.

## License

TriCycle Reaction Database is released under the [MIT License](LICENSE).
ChemDoodle Web Components in `frontend/public/vendor/chemdoodle` are distributed
under their upstream GPLv3 or commercial license; see
[`COPYING.txt`](frontend/public/vendor/chemdoodle/COPYING.txt).
