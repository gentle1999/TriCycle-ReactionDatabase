# Implementation Backlog

[中文](../implementation-backlog.md) | [Documentation index](README.md)

> Historical phased backlog. Status and test counts are valid only at their
> recorded date; consult current code and tests before planning new work.

> Follow-up recorded 2026-09-10: the [database architecture refactor plan](database-architecture-refactor-plan.md)
> owns DB-00 through DB-09 priorities, dependencies, migrations, and acceptance.
> Earlier completion and benchmark records do not establish acceptance of the new
> source-isolation, effective-revision, or concurrency requirements.

## Recorded Workstreams

The backlog grouped work into read-side APIs, upload/ingestion closure, domain
filters, query cost and rate limiting, authorization, frontend delivery, test
restoration, deployment preparation, and milestone validation. Each item records
acceptance expectations and command-level evidence rather than treating an issue
title as completion proof.

## Current Reading Rule

Use this document to understand why an interface or test exists. Use the current
[development guide](development.md), [data model](data-model.md), migrations,
and test suite to determine present behavior. In particular, local import now
shares the upload service, uses a streaming candidate window, and exposes
per-artifact parsing states including `filtered`.
