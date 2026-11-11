# Environment Variable Reference

Browse the project's environment variables by function. Each page lists code defaults, validation bounds, and examples for variables outside Python settings.

Application settings use separate Pydantic models for runtime queries, MCP exports, ingestion workers, identity and email, and storage connections, assembled by `ApplicationSettings`. RustFS, storage GC, frontend build, and Compose have separate models in [core/config.py](../../src/tricycle_reaction_db/core/config.py). `load_configuration()` loads backend settings; `load_project_configuration()` assembles all models and checks cross-domain consistency.

Python settings read the process environment and the current working directory's `.env`; Compose interpolates `.env` and passes it to services. Restart affected backend services after changes; rebuild the frontend after changing `VITE_*`.

Run `make validate-config` to preflight the root `.env`; `make frontend-build` preflights `frontend/.env`.

See [deployment configuration](deployment-configuration.md) for production cross-field checks. Run `make env-docs` after changing declarations to regenerate both languages.

| Function | Contents | Variables |
| --- | --- | ---: |
| [Runtime and query](environment/runtime-query.md) | Service identity, database, GraphQL, query budgets, and statistics. | 37 |
| [MCP and data export](environment/mcp-export.md) | MCP response budgets, TS geometry, and UniTS exports. | 19 |
| [Ingestion and workers](environment/ingestion-workers.md) | Uploads, parsing, persistence, maintenance commands, and workers. | 56 |
| [Identity and email](environment/identity-email.md) | OIDC, sessions, bootstrap, email, and CORS. | 45 |
| [Object storage and cleanup](environment/storage.md) | RustFS/S3 connections and garbage collection. | 22 |
| [Frontend build and pagination](environment/frontend.md) | Vite build variables, pagination, and prefetch. | 19 |
| [Compose and infrastructure](environment/deployment.md) | Container images, PostgreSQL, Caddy, Keycloak, and build proxies. | 41 |
