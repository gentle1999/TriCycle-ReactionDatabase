# Production Operations and Recovery Runbook

[中文](../operations-runbook.md) | [Documentation index](README.md)

## Scope

This runbook covers multi-host backup, recovery, monitoring, and scheduled
maintenance. Perform recovery procedures in an isolated environment first.
The paired Chinese page is the detailed command and evidence reference.

## Backup and Recovery

Back up PostgreSQL including Alembic state, RustFS/S3 objects with version and
lifecycle information, OIDC realm/user/signing-key material, and production
secrets through the approved secret-management system. Store backups in an
independent failure domain and record the source snapshot manifest.

An isolated recovery must restore database and objects, run migrations only as
the recovery plan permits, validate table counts and exact object bytes/metadata,
and compare the source and restored manifests. Do not accept a successful API
startup as proof that raw artifacts, authorization data, and provenance agree.

Record measured RTO and RPO from the recovery exercise. A blank template or
estimated value is not acceptance evidence.

## Scheduled Work

Run these as separate scheduled processes, never inside every API worker:

```bash
uv run tricycle-auth-session-cleanup
uv run tricycle-rustfs-gc
```

Session cleanup removes expired/revoked sessions according to retention policy.
RustFS GC is crash-recovery compensation: normal upload failure cleanup is
synchronous and targeted. GC uses a database watermark and advisory lock, scans
only managed prefixes after a grace period, and must leave its watermarks
unchanged after any list/delete/database error.

## Monitoring and Acceptance

Monitor public live/ready state, PostgreSQL/RustFS/Redis/OIDC/SMTP reachability,
upload pending/failure/object integrity, scheduled-unit failures, rate limits,
and query latency/timeouts. `/health/live` alone is insufficient; `/health/ready`
must include PostgreSQL and RDKit readiness.

Before release, run `tricycle-deployment-smoke` on every API node and retain its
redacted JSON output. Then exercise OIDC login/logout, invitation sending and
acceptance, artifact upload plus exact download hash, MCP streaming, private
cross-project denial, planned dependency failover, and isolated restore. Record
the results in `infra/deployment/acceptance-record.example.md` and validate the
machine-readable acceptance record.

## Incident Rules

Preserve original artifacts and ParseRevision evidence. A parser or query issue
must not be "fixed" by deleting scientific records or weakening authorization.
Quarantine the affected endpoint, collect redacted diagnostics, correct the
configuration or code, re-run focused checks, and document the recovery.
