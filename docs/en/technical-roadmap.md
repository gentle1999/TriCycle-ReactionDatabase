# Technical Roadmap

[中文](../technical-roadmap.md) | [Documentation index](README.md)

> Historical architecture and planning record. Refer to the current data model,
> development, deployment, and operations documents for active instructions.

## Recorded Direction

The roadmap establishes a modular-monolith baseline: immutable raw artifacts in
external object storage; PostgreSQL/RDKit for relational and structure-query
projections; a chemical identity axis of Formula, Topology, and Geometry; a
separate calculation-provenance axis; and an explicit logical/mapped reaction
path model. FastAPI adapters call application services rather than exposing ORM
objects directly. MolOP is integrated through stable public result models and
MolGR provides graph reconstruction.

The staged goals cover golden fixtures, artifact ingestion, chemistry identity,
reaction paths, searchable APIs, authorization, operations, and release
validation. The document's dated milestones capture decisions at the time of
writing, not an assertion that every listed future item remains pending.

## Current Supersession

Current implementation preserves source atom order for trusted MolGR graphs,
uses explicit-H topology strings, includes charge/multiplicity in Geometry
identity, and persists TS inference evidence without automatic reaction-class
assignment. See [Data model](data-model.md) for the active contract.
