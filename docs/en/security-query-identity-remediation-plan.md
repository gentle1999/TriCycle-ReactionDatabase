# Security, Query, and Identity Remediation Plan

[中文](../security-query-identity-remediation-plan.md) | [Documentation index](README.md)

> Dated remediation plan and evidence record. Do not interpret recorded status
> as a replacement for a target-release verification run.

## Recorded Remediation Areas

The plan covers topology visibility, representation-cache correctness, upload
resource limits, production authentication, database-side project authorization,
login-event separation, artifact paging, dependency/supply-chain controls,
ChemDoodle isolation, OIDC provider evaluation, release observability, and
rollback rules.

## Current Safeguards

Trusted MolGR graphs retain source atom order and explicit electronic markings;
the application does not sanitize or add hidden hydrogens during normal
normalization. Local and browser imports share the upload service, a per-file
timeout releases only its worker, and parsing state remains observable. OIDC
discovery issuer equality, private buckets, authorization-filtered queries,
shared production rate limiting, and indexed query paths remain mandatory.

Review the current [deployment guide](deployment-configuration.md),
[data model](data-model.md), and tests when changing any of these boundaries.
