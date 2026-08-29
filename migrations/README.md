# Database migrations

[English](README.md) | [简体中文](README.zh-CN.md)

Alembic owns every database schema change. Run migrations with:

```bash
uv run alembic upgrade head
```

Do not use `SQLModel.metadata.create_all()` in application startup or
production code.

The mainline starts from one reviewed baseline revision:

- `0001_initial_schema`: current v1 PostgreSQL/RDKit schema, extensions, functions, indexes,
  constraints, and generated columns. It does not create users, organizations, projects, or roles.

The snapshot was captured from the validated development head `20260819_0053` on PostgreSQL
18.3 with RDKit 4.8.0 and pg_trgm 1.6. It is a schema snapshot, not a dump of the development
database contents.

The development-only migration chain that produced this baseline is intentionally not part of
the Alembic script directory. It is not needed to deploy an empty database and should not be
reintroduced as the production migration history.

The baseline SQL is generated from a schema-only snapshot and deliberately contains no artifact,
calculation, or fixture rows. Re-ingest those records with the normal upload/seed workflows after
initialization.

Run bootstrap explicitly after migrating a new database:

```bash
# Local development only: fixed test identity and configurable placeholder containers.
uv run tricycle-bootstrap --mode development

# Production: requires production OIDC plus every TRICYCLE_BOOTSTRAP_* value.
uv run tricycle-bootstrap --mode production
```

Both modes are idempotent and write a `deployment.bootstrap` audit event. Development bootstrap is
rejected when `TRICYCLE_ENVIRONMENT=production`; production bootstrap is rejected outside production.
The production command grants owner/manager only to the explicitly configured OIDC identity and does
not grant project access to the system service account.

`0001_initial_schema` is upgrade-only. To roll back a deployment, restore a database backup or
recreate the database; do not run a destructive downgrade against a production database.
