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

## Capacity and Parsing

`TRICYCLE_MOLOP_BATCH_N_JOBS` is the concurrent file-process count. Bound
OpenMP/BLAS pools separately with `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, and
`MKL_NUM_THREADS`. Keep the import candidate window above worker count and use a
small completed-result commit microbatch. The parse timeout is 60 seconds for a
10 MiB input and scales proportionally; timeout advances only that worker to the
next queued file. Never use `TRICYCLE_MOLOP_BATCH_N_JOBS=-1` in production.

Scale API capacity through separate nodes behind Caddy and shared Redis rate
limiting. Do not use multiple Uvicorn workers on one metrics listener without a
Prometheus multiprocess design.

## Release Order

1. Verify PostgreSQL/RDKit and the private RustFS bucket.
2. Set production secrets plus TLS/CA configuration.
3. Run `uv run alembic upgrade head`.
4. Run `uv run tricycle-bootstrap --mode production`.
5. Start API/frontend/edge and schedulers as separate processes.
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
