# Security and Query Baseline

[中文](../security-query-baseline.md) | [Documentation index](README.md)

> Dated security/performance baseline. Use it to understand the original review
> scope; use current configuration and tests for active limits.

## Baseline Themes

The record covers project authorization, query count/plan evidence, uploads,
shared Redis rate limiting, and Keycloak/OIDC boundaries. It requires structure
searches to be bounded by input size, index-supported candidates, timeout, and
rate limits, with slow-query logs redacting user parameters and secrets.

## Current Applicability

Current high-scale lists use project Geometry catalog and inexpensive elemental
filters before expensive structural conditions. Page lists use deterministic
sorting and caching. The current development guide documents active budgets and
error semantics; the database cost tests protect the key indexed paths.
