# Refactor and Release Plan

[中文](../refactor-plan.md) | [Documentation index](README.md)

> Historical plan and acceptance record. Its dated commands, dependency versions,
> and pass/fail counts are preserved as evidence, not current release approval.

## Recorded Phases

The plan records baseline freezing, visibility-contract repair, OIDC/session/CSRF
work, production configuration, query/upload capacity, frontend test gates,
backup/recovery/monitoring, and migration/bootstrap closure. It also records
review snapshots and external evidence still required for production acceptance.

## Current Release Rule

Run the current checks and deployment exercises for the target release. A past
green test count, an empty acceptance template, or a local Compose result does
not substitute for target-environment OIDC, SMTP, TLS, failover, backup, RTO, or
RPO evidence. Use the current deployment guide and operations runbook.
