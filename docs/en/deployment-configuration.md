# Deployment and Configuration

[中文](../deployment-configuration.md) | [Documentation index](README.md)

## Deployment Boundary

The application supports single-host and multi-host production deployments.
PostgreSQL/RDKit, RustFS/S3, Redis, OIDC, SMTP, and API processes can run on
different hosts behind stable endpoints.

```text
Browser -> EDGE (TLS, frontend, API upstream) -> API nodes
       -> PostgreSQL/RDKit writer, RustFS/S3 HTTPS, Redis TLS, OIDC, SMTP

Scheduler and local import CLI -> the same database, object-store, and Redis endpoints
```

The root `compose.yaml` is a single-host development/acceptance stack. Do not
run it unchanged on a compute host that should use remote data services: it
would create a competing local PostgreSQL/RustFS pair. Use `compose.data.yaml`
for the data host and the `compose.compute.yaml` overlay for a compute/API host,
or equivalent separate production stacks.

Expose only a same-origin HTTPS edge. Do not publish PostgreSQL, RustFS Console,
Keycloak administration, or internal API ports. HTTP only redirects to HTTPS;
Caddy proxies API, health, OpenAPI, GraphQL, MCP, and NexusX routes.

## Required Configuration

Prepare stable endpoints and credentials for public DNS/TLS, PostgreSQL with
RDKit and `sslmode=verify-full`, private HTTPS RustFS/S3, writable TLS Redis,
OIDC issuer/audience/JWKS/client, SMTP STARTTLS, bootstrap administrator, and
backup/monitoring/timer infrastructure. Inject secrets through the deployment
platform or a password manager, never through Git.

Production starts fail closed. It requires OIDC authentication, a non-default
session secret, secure cookies, verified database and object-store TLS, a
positive bounded MolOP worker count, and production bootstrap values. Use the
matching `TRICYCLE_*_CA_BUNDLE` settings when internal services use a private CA.

`TRICYCLE_OIDC_ISSUER` must exactly equal the issuer in OIDC discovery, including
scheme, host, path, and slash convention. The error `OIDC discovery issuer does
not match configuration` is fixed by aligning the configured issuer and identity
provider, not by proxy rewriting. The bundled Keycloak service is `start-dev`
only; production needs a supported, TLS-protected OIDC provider with independent
database, signing-key, and backup management.

## Startup

Host development uses `make dev`. For the packaged local Compose stack:

```bash
cp .env.example .env
make stack-up
curl --insecure https://localhost/health/ready
```

For a compute host attached to remote data services:

```bash
docker compose -f compose.yaml -f compose.compute.yaml config --quiet
docker compose -f compose.yaml -f compose.compute.yaml up -d --build --wait
```

`VITE_*` values are frontend build inputs. Rebuild after changing them and keep
`VITE_API_BASE_URL` empty for ordinary same-origin production operation. The host
local-import CLI uses the same verified PostgreSQL/RustFS endpoints as the API;
it does not start a HTTP uploader.

### Build mirrors

The images default to digest-pinned official Python, Node, and Nginx base images,
plus the official PyPI, npm, and Debian repositories. Build-only variables can
replace any of these sources without changing runtime API, RustFS, or PostgreSQL
endpoints. For example, to use ZJU's PyPI and Debian main mirrors:

```bash
TRICYCLE_PYPI_INDEX_URL=https://mirrors.zju.edu.cn/pypi/web/simple \
TRICYCLE_DEBIAN_MIRROR=https://mirrors.zju.edu.cn/debian \
docker compose -f compose.yaml -f compose.compute.yaml build --pull=false api frontend
```

The same variables can be stored in `.env` and then used by the normal Compose
or `make stack-build` commands:

| Variable | Default | Use |
| --- | --- | --- |
| `TRICYCLE_PYPI_INDEX_URL` | `https://pypi.org/simple` | uv and Python dependencies in the API builder |
| `TRICYCLE_DEBIAN_MIRROR` | `https://deb.debian.org/debian` | Debian main repository in the API runtime |
| `TRICYCLE_DEBIAN_SECURITY_MIRROR` | `https://security.debian.org/debian-security` | Security repository; official by default |
| `TRICYCLE_NPM_REGISTRY` | `https://registry.npmjs.org` | npm dependencies in the frontend builder |
| `TRICYCLE_PYTHON_BASE_IMAGE` | pinned official Python image | Complete API builder/runtime base-image reference |
| `TRICYCLE_NODE_BASE_IMAGE` | pinned official Node image | Complete frontend builder base-image reference |
| `TRICYCLE_NGINX_BASE_IMAGE` | pinned official Nginx image | Complete frontend runtime base-image reference |

Base-image overrides should remain compatible, complete references with a digest,
such as an internally cached `python:3.12-slim-bookworm@sha256:...`. Keep Debian
security updates on the official source unless the mirror's freshness is known;
the npm registry is configured independently rather than assuming that ZJU hosts
one.

## Capacity and Parsing

`TRICYCLE_MOLOP_BATCH_N_JOBS` is the shared worker parser-pool admission limit.
Bound OpenMP/BLAS pools separately with `OMP_NUM_THREADS`,
`OPENBLAS_NUM_THREADS`, and `MKL_NUM_THREADS`. The local import candidate
window only bounds RustFS staging and fingerprint buffering. The parse timeout
is 60 seconds for a 10 MiB input and scales proportionally; timeout advances
only that worker to the next queued file. Never use
`TRICYCLE_MOLOP_BATCH_N_JOBS=-1` in production.

After RustFS staging, browser, remote, and legacy pending-ingestion imports use
the single `ArtifactUploadService.reparse_batch` path. The worker claims a
64-file window and `reparse_batch` only reads/verifies existing RustFS objects before
delegating to the shared MolOP process pool and single persistence consumer. It
does not upload the object again or add a parser per request. `TRICYCLE_UPLOAD_MAX_CONCURRENCY` limits RustFS reads,
`TRICYCLE_UPLOAD_WORKER_CONCURRENCY` is retained for pending-ingestion
recovery, and `TRICYCLE_MOLOP_BATCH_N_JOBS` is the shared parser-pool admission
limit. Inside `upload_batch`, every eight completed files (or a temporarily
empty result queue) are handed to the single persistence consumer. The
persistence transaction is also capped at eight completed files or 128 parsed
frames, whichever limit is reached first. The client
`UploadBatch` is only a queue/progress boundary, not a persistence boundary:
one-file submissions for the same project/user are merged into one persistence
microbatch, while different project/user microbatches are committed
sequentially, with each persistence microbatch committed at the bounded
eight-file/128-frame boundary.
Neither boundary changes the configured parser-pool admission target, and these
controls must not be multiplied.
The bulk path keeps the previous legacy hot-path behavior; per-file
concrete/logical/reverse reconciliation must not be inserted there without a
same-fixture throughput regression check.

This model requires exactly one `upload-worker` instance in production: it is
the boundary for the shared MolOP process pool and the single active persistence
consumer. API nodes may scale horizontally, but upload-worker should not be
scaled horizontally. Multiple worker replicas create independent parser pools
and persistence consumers, changing the serial project/user-group and resource
limit semantics described here.

Scale API capacity through separate nodes behind Caddy and shared Redis rate
limiting. Do not use multiple Uvicorn workers on one metrics listener without a
Prometheus multiprocess design.

## Release Order

1. Verify PostgreSQL/RDKit and the private RustFS bucket.
2. Set production secrets plus TLS/CA configuration.
3. Run `uv run alembic upgrade head`.
4. Run `uv run tricycle-bootstrap --mode production`.
5. Start API, the durable upload worker, frontend/edge, and schedulers as separate processes.
6. Exercise real OIDC login/logout, invitation, artifact upload/download, and recovery.

```bash
curl -fsS https://<app-host>/health/live
curl -fsS https://<app-host>/health/ready
uv run alembic current
uv run alembic check
caddy validate --config infra/caddy/Caddyfile --adapter caddyfile
```

Run `tricycle-deployment-smoke` from every API node and archive its redacted JSON
with the acceptance record. It complements, but does not replace, real user-flow
and failover checks.
