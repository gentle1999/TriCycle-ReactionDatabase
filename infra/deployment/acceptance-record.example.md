# Production deployment acceptance record

[English](acceptance-record.example.md) | [简体中文](acceptance-record.example.zh-CN.md)

This is a blank evidence template. Replace every `PENDING` value from the target deployment; an
unfilled copy is not release approval.

After filling the evidence, create the machine-readable `deployment-acceptance-v1` JSON record and
run `make validate-deployment-acceptance ACCEPTANCE_RECORD=/path/to/record.json`. The validator
requires two API-node smoke attachments, all six dependency failovers, six workflow checks, five
monitoring trigger/recovery pairs, 1/8/32 capacity results, stable 413 upload-limit evidence,
indexed query-plan evidence, matching source/restore manifests, and measured non-placeholder RTO/RPO.
The Markdown table is for review;
the JSON record and hashed attachments are the acceptance evidence.

Generate the capacity attachments on the target dataset and API node:

```bash
uv run --frozen python scripts/benchmark_upload_resources.py \
  --output evidence/capacity/upload-benchmark.json
make probe-upload-limit \
  UPLOAD_API_URL=https://api-01.example.test \
  UPLOAD_PROJECT_ID=<authorized-project-uuid> \
  UPLOAD_MAX_BYTES=67108864 \
  ACCEPTANCE_BEARER_TOKEN="$TRICYCLE_ACCEPTANCE_BEARER_TOKEN" \
  UPLOAD_LIMIT_OUTPUT=evidence/capacity/upload-limit.json
make probe-shared-rate-limit \
  RATE_LIMIT_MODE=shared \
  API_URLS="https://api-01.example.test https://api-02.example.test" \
  ACCEPTANCE_BEARER_TOKEN="$TRICYCLE_ACCEPTANCE_BEARER_TOKEN" \
  RATE_LIMIT_OUTPUT=evidence/capacity/shared-rate-limit.json
make probe-shared-rate-limit \
  RATE_LIMIT_MODE=fail-closed \
  API_URLS="https://api-01.example.test https://api-02.example.test" \
  ACCEPTANCE_BEARER_TOKEN="$TRICYCLE_ACCEPTANCE_BEARER_TOKEN" \
  RATE_LIMIT_OUTPUT=evidence/capacity/rate-limit-fail-closed.json
make capture-query-plan-evidence \
  DATASET_SCALE=<immutable-snapshot-id-and-row-scale> \
  QUERY_PLAN_OUTPUT=evidence/capacity/query-plans.json
```

## Release identity

| Field | Evidence |
| --- | --- |
| Deployment / change ID | PENDING |
| Git revision and immutable artifact digest | PENDING |
| Public origin | PENDING |
| UTC start / finish | PENDING |
| Operator / reviewer | PENDING |

## Node and dependency smoke

| Node | Before-change JSON | After-change JSON | Result |
| --- | --- | --- | --- |
| API-01 | PENDING | PENDING | PENDING |
| API-02 | PENDING | PENDING | PENDING |

Run `tricycle-deployment-smoke` on every API node. Attach the JSON files; do not paste secrets or
unredacted connection URLs. A failed check is a deployment failure even when another API node is
healthy.

## Planned failover

| Dependency | Stable endpoint | Injection / switch | Start UTC | Recovered UTC | Client errors | Result |
| --- | --- | --- | --- | --- | --- | --- |
| PostgreSQL writer | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| RustFS/S3 entrypoint | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Redis writer | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| OIDC node | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| SMTP relay | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| EDGE/API upstream | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |

Record whether the logical DNS/issuer remained stable. For PostgreSQL, attach proof that the new
endpoint is writable and has the expected RDKit extension. For Redis, attach the shared limiter
result from at least two API processes.

## User workflow

| Check | Evidence | Result |
| --- | --- | --- |
| OIDC authorization-code + PKCE login/logout | PENDING | PENDING |
| Same-origin Session and CSRF state change | PENDING | PENDING |
| Project invitation sent, received, and accepted | PENDING | PENDING |
| Artifact upload and exact-version download hash | PENDING | PENDING |
| MCP streaming request held through switch | PENDING | PENDING |
| Cross-project private object remains 404 | PENDING | PENDING |

## Backup and restore

| Field | Evidence |
| --- | --- |
| Backup ID / database snapshot / object replication watermarks | PENDING |
| OIDC realm, user store, signing key and secret backup references | PENDING |
| Failure injection UTC | PENDING |
| Database restore complete UTC | PENDING |
| Object restore complete UTC | PENDING |
| Application acceptance complete UTC | PENDING |
| Source `source-manifest.json` + restore `restore-validation.json` (`schema_version`, digest, mismatches) | PENDING |
| Measured RTO | PENDING |
| Latest recoverable transaction/object UTC | PENDING |
| Measured RPO | PENDING |
| Mismatches and disposition | PENDING |

## Monitoring and approval

| Check | Trigger evidence | Recovery evidence | Result |
| --- | --- | --- | --- |
| Public live/ready | PENDING | PENDING | PENDING |
| PostgreSQL / RustFS / Redis | PENDING | PENDING | PENDING |
| OIDC / SMTP | PENDING | PENDING | PENDING |
| Upload pending/failure and object integrity | PENDING | PENDING | PENDING |
| Maintenance unit failure | PENDING | PENDING | PENDING |

Release decision: `PENDING`

Approver and UTC timestamp: `PENDING`
