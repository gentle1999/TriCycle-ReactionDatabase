# Database Entity Relationship Diagram

[中文](../database-erd.md) | [Documentation index](README.md)

> Current physical-schema reference. The Chinese counterpart contains the full
> ERD and table-by-table inventory; SQL identifiers are identical in both pages.
>
> Current schema: Alembic `0037_project_owned_calculation_protocol`. The ORM inventory contains
> 64 tables, 778 columns, 101 foreign keys, 77 UNIQUE constraints, 202 CHECK constraints,
> and 171 indexes.

## Storage Boundaries

PostgreSQL holds authorization, object locators, content hashes, parse revisions,
calculation facts, chemistry identity, reaction paths, and audit state. RustFS/S3
holds original bytes only. The database does not store a second mutable copy of
an artifact payload. Immutable `ArtifactFile` is the only cross-user/project
sharing boundary; parsing, frames, chemistry identities, reactions, and derived
edges follow its project ownership. Historical derived rows without one
unambiguous owner are kept in the quarantine ledger and excluded from ordinary
project queries. Every derived query also requires an explicit `project_id` and
the current authenticated user's permission on that project; only the raw
`ArtifactFile` object is an intentional cross-project cache boundary.

```text
ArtifactFile -> ParseRevision -> CalculationSegment -> CalculationFrame -> Geometry
                                      |                    |
                                      |                    `-> scientific result tables / ScientificArray
                                      `-> TransitionStateInference -> LogicalReaction -> MappedReaction

MolecularFormula -> MolecularTopology -> Geometry
                         |            `-> MolecularTopologyDerivation
                         `-> MolecularTopologyAbstraction (directed stereo DAG)

LogicalReactionParticipant -> LogicalParticipantConcreteTopology -> MolecularTopology
MappedReaction -> MappedReactionParticipant -> concrete MolecularTopology

Organization -> Project -> ArtifactFile and visibility scope
```

`Geometry` has a unique identity over topology, canonicalization version,
geometry hash, charge, and multiplicity. `MolecularTopology` uses a versioned
graph hash and has a distinct derivation provenance record. `ArtifactIngestion`
exposes `pending`, `succeeded`, `partial`, `filtered`, and `failed` states.

## Integrity and Query Rules

Foreign keys, unique constraints, checks, generated columns, RDKit cartridge
indexes, and purpose-specific B-tree/GIN indexes are schema responsibilities.
Migrations own all changes. Do not use ORM `create_all`, manual production DDL,
or destructive downgrade as a rollback strategy. Rebuild an empty database by
running `alembic upgrade head`, then run the correct bootstrap mode and ingest
artifacts through normal services.

The project geometry catalog and indexing migrations exist to keep visibility,
counts, paging, per-file frame listing, and Geometry matching bounded at scale.
The detailed Chinese ERD documents each physical relation and cross-backend
constraint; inspect it when modifying a table or a migration.
