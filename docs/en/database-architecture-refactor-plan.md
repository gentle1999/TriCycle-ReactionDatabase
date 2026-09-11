# Database Architecture Refactor Plan

[中文](../database-architecture-refactor-plan.md) | [Documentation index](README.md)

> Created: 2026-09-10
>
> Status: Batch A minimum fixes, project-scoped query boundaries, and the historical derived-data
> cleanup are implemented in source/remote operations. Source and deployed migrations now reach
> `0038_geometry_match_index`. The two-project full RustFS re-import is running from checkpoints;
> final two-project acceptance remains in progress.
>
> Review baseline: Git `522405a` plus the existing working-tree changes; 63 ORM tables.
>
> Latest migration in source: `0038_geometry_match_index`; it adds the project-local Geometry
> candidate index and has been verified in the deployed database with `alembic current`.
>
> Scope: scientific facts, source authorization, thermodynamics, versions and integrity,
> derived refreshes, geometry matching, and array storage.

This is the execution plan for this database refactor, not a claim that its target
models are complete. The [data model](data-model.md), code, and tests describe current
behavior; update current contracts as stages ship. The [technical roadmap](technical-roadmap.md),
[previous backlog](implementation-backlog.md), and [release plan](refactor-plan.md)
retain their dated evidence. New work in this round uses `DB-*` identifiers.
Recording the plan does not by itself constitute a production release, cleanup, or
fact rewrite; the deployment record for this round is listed separately.

## Implementation Record (Batch A Minimum Fixes)

- DB-01: Successful `ParseRevision`/`ArtifactIngestion` lifecycle and source-Frame
  visibility now flow through the shared read paths for profile loading, details,
  lists, analytics, and CSV. Newly written profiles retain electronic-energy and
  thermochemistry source Frame IDs; legacy profiles use a visible-Geometry compatibility
  fallback. A durable profile-source relation, backfill, and two-project acceptance remain.
- DB-02: Equally scored distinct protocols, and conflicting observations under one
  protocol, are no longer resolved by earliest Frame. Deterministic selection is allowed
  only for complete protocols with equivalent observations within the declared tolerance.
  Full protocol policy, QC, and effective-revision integration remain.
- DB-03: Lists, details, filters, analytics, and export share an exact `EXISTS` predicate
  over one visible profile; legacy `MappedReaction` min/max fields remain for compatibility
  but are no longer final restricted-user predicates.
- Historical cross-project isolation: source migrations `0035` through `0037` scope Formula,
  Topology, Geometry, LogicalReaction, MappedReaction, TS inference, CalculationProtocol, and
  their relations to a project; database triggers reject cross-project writes. The agreed reset
  has since cleared all derived tables while preserving `ArtifactFile`, user/organization/project
  identity, and RustFS raw objects. Old backfill/quarantine rows are not the current data source;
  each ArtifactFile must be re-materialized into its owning project after the final migration.
- Query boundary: every derived query requires an explicit `project_id` and the current
  authenticated user's project permission. Missing or unauthorized scope fails closed. Only
  immutable `ArtifactFile` is an intentional raw-object cache boundary; it is still project-filtered
  and does not expose parse status or derived metadata.
- Query performance: project permission is checked with one indexed target `EXISTS`; project-scoped
  derived queries use direct `project_id = :project_id` predicates instead of walking the provenance
  graph per row. Geometry candidate matching has a project-leading composite index, and actual
  `EXPLAIN` output observed the scope predicate as an `Index Cond`.
- Import recovery: migration `0034_ingestion_recovery_lease` adds recovery leases. The worker
  claims stale pending ingestions without active `UploadBatchItem` ownership, verifies the RustFS
  object, and reparses it; lease fencing prevents late results from overwriting newer attempts.
  The earlier convergence of 16 orphaned tasks is pre-reset evidence only; after the reset, a fresh
  checkpointed RustFS import is required and the old checkpoint cannot prove completion.
- Verification: migration `0038` is deployed, readiness is healthy, and code-level unit, Ruff,
  compilation, and query-plan checks pass. The two-project re-import, final isolation audit, and
  complete post-reset counts remain in progress. Pre-reset counts such as `artifact_file=125355`
  and AutoDE active files `36342` are historical baselines.

## 1. Evidence and Findings

The initial review covered ORM models, migrations, core writes, and queries, with
small in-memory probes; the configured database connection was unavailable then.
Implementation has since checked the deployed database, applied the migration, and
observed the recovery worker. Isolated fixtures, full query-plan measurements, and a
concurrency baseline remain outstanding. A code-level risk is not an observed
production incident; evidence limits below reflect the current state.

| Item | Priority | Evidence and problem | Limits of evidence |
| --- | --- | --- | --- |
| DB-00 | Prerequisite | Freeze data, migration, and performance baselines | Deployed revision, core counts, and isolation audits recorded; isolated fixtures, full baseline, and planned measurements remain |
| DB-01 | P0 | [Profile loading](../../src/tricycle_reaction_db/application/services/mapped_reaction_thermodynamics_persistence.py), details, lists, analytics, and CSV now share successful ingestion/revision, retirement, and source-Frame visibility filters; historical derived roots are project-scoped | Minimum fix and historical containment shipped; durable profile-source relation, profile backfill, and two-project acceptance remain |
| DB-02 | P1 | [Energy selection](../../src/tricycle_reaction_db/application/services/geometry_energy.py) returns ambiguity for tied distinct protocols or conflicting same-protocol values; only equivalent observations may be selected deterministically | Unit counterexamples covered; complete protocol policy, QC, and effective-revision integration remain |
| DB-03 | P1 | [Range queries](../../src/tricycle_reaction_db/application/services/queries.py), analytics, and export now use one exact visible-profile `EXISTS` predicate rather than global min/max envelopes | SQL compilation regression passed; isolated EXPLAIN and scale baseline remain |
| DB-04 | P1 | [Visibility](../../src/tricycle_reaction_db/application/services/query_visibility.py) includes all parse revisions; [membership](../../src/tricycle_reaction_db/application/services/reaction_topology_membership.py) evidence is overwritten; profiles are deleted and rebuilt | History exists, but current selection and historical analysis reproduction lack separate models |
| DB-05 | P1 | [Reaction relations](../../src/tricycle_reaction_db/db/models/reactions.py), [TS links](../../src/tricycle_reaction_db/db/models/uploads.py), and [array owners](../../src/tricycle_reaction_db/db/models/calculations.py) now carry project ownership and historical quarantine state; write paths no longer reuse derived identities across projects | Ownership/source-chain audits are 0; composite parent FKs, database rejection of invalid writes, and full relationship constraints remain |
| DB-06 | P1 | [Ingestion finalization](../../src/tricycle_reaction_db/application/services/molop_artifact_ingestion.py) synchronously expands links and thermodynamics; [project counts](../../migrations/versions/0012_project_geometry_catalog_listing_summary.py) update a shared counter | Contention and write amplification are structural risks, not measured bottlenecks |
| DB-07 | P1/P2 | [Sequential internal coordinates](../../src/tricycle_reaction_db/domain/internal_coordinates.py) have collinear degeneracies; [matching](../../src/tricycle_reaction_db/application/services/molecular_geometry.py) depends on the lowest observed printing precision | A probe produced identical internal hashes for different coordinates; [normalization](../../src/tricycle_reaction_db/ingestion/normalization.py) rejects lossy reconstruction, so erroneous persistence was not demonstrated |
| DB-08 | P2 | Inference is unique per revision/frame and endpoint per frame/direction | Multiple modes, settings, or algorithms cannot naturally coexist on one Frame |
| DB-09 | P2 | [ScientificArray](../../src/tricycle_reaction_db/db/models/calculations.py) is fully inline; [NPY](../../src/tricycle_reaction_db/db/types/numpy_array.py) has a default 64 MiB payload cap | Storage tiering needs real array-size and access distributions |

## 2. Target Boundaries and Decisions

Keep PostgreSQL/RDKit, RustFS/S3, and the shared application-service layer. Preserve
Formula, Topology, Geometry, LogicalReaction, and MappedReaction boundaries, source
coordinates and atom order, explicit hydrogens, electronic labels, and stereo evidence.
Add relations and projections rather than rewriting existing scientific identities.

| Responsibility | Target direction; new names are design candidates | Rule |
| --- | --- | --- |
| Original facts | ArtifactFile, ParseRevision, CalculationFrame, raw results | Append facts; do not replace original bytes or parsed results |
| Effective parsing | `ArtifactRevisionSelection` or equivalent explicit record | Separate current selection from history; record policy, reason, and time |
| Source authorization | Actual Frame/Revision relations such as `ThermodynamicProfileSource` | Shared identity does not grant private calculation access; filtering, ordering, and statistics must also respect sources |
| Reproducible analysis | `AnalysisSnapshot`, versioned profiles and membership evidence | Pin actual inputs, settings, and algorithms; historical reads still require current authorization |
| Independent inference | `InferenceRun`, run-owned Endpoint | Identify inference by Frame, mode, version, and settings without reparsing bytes |
| Current read models | Scoped thermodynamic projection, Geometry catalogue | Rebuildable, with input watermarks, policy, and refresh status; never final authorization authority |
| Derived tasks | Transactional `ProjectionRefreshTask` or outbox | Deduplication, leases, retries, and catch-up through a database worker; no new broker required |
| Array content | Inline small payloads and referenced large content | Relational metadata and explicit authorized downloads; no implicit ORM network IO |

Fixed rules:

1. Do not delay the P0 repair for a complete schema redesign. Recompute from visible
   sources or strictly verify provenance in existing read paths first. Unverifiable
   values have an explicit unavailable state, never a global-summary fallback.
2. Project/public projections optimize reads; they do not define permissions.
   Multi-project queries use the caller's actual visible source set. Combining
   per-project best values is not a substitute for source selection over the union.
3. Retirement, membership revocation, and visibility changes take effect on subsequent
   requests immediately. Authorization/source watermarks govern caches, and cache hits
   cannot bypass authorization. Snapshots do not preserve revoked access rights.
4. Reparse success, selection changes, and cache refreshes are distinct operations.
   Do not automatically select failed, filtered, or pending revisions. Partial revisions
   require explicit QC selection and do not replace a previous valid success by default.
5. Method scores are explicit analysis policies, not proof of protocol equivalence.
   Preserve method, basis, electronic state, solvent, temperature/pressure, and relevant
   standard-state provenance. Unknown or equally scored methods are not interchangeable.
6. Separate exact Geometry identity from approximate assignment. Retain source coordinates,
   permutations, rigid transforms, and evidence versions. Do not relax stereo, electronic
   state, or atom correspondence to increase reuse.
7. Clearing the database, reimporting all files, and deleting revisions are not default
   migrations. Completion requires code, migration/interface, tests, and documentation
   evidence. Skips and unavailable environments are not passes.
8. The only cross-user/project sharing boundary is immutable `ArtifactFile`. `ParseRevision`,
   `CalculationFrame`, Formula, Topology, Geometry, Reaction, and every derived edge must be
   accessed through the same ArtifactFile project ownership; equal content, graph, or reaction
   hashes must never reuse one derived row across projects.

## 3. Work Items and Acceptance

### DB-00: Baseline and Regression Fixtures

- Status: `todo`. Dependencies: none.
- Record Git revision and local differences, actual Alembic current/head, PG/RDKit
  versions, table/index sizes, row counts, array distributions, and available plans.
  Record existing user changes separately from this implementation.
- Build isolated public, private A, and private B sources with shared reactions/Geometry,
  reparses under one protocol, equally scored distinct protocols, retired sources,
  unmatched profile ranges, degenerate coordinates, and repeated refreshes.
- Record a reproducible failure or structural risk for each item, with environment and
  command evidence. DB-01 need not wait for a full-scale benchmark. Explicitly separate
  the normal runtime from the test database.
- Acceptance: reusable fixture hashes and records; production data unchanged;
  measurements clearly distinguished from pending work.

### DB-01: Source Authorization and Lifecycle Isolation

- Status: `partial` (Batch A minimum fix). Depends on the isolated authorization
  fixture from DB-00; durable profile-source relations and full two-project acceptance remain.
- Cover thermodynamic details, reaction summaries, range/existence filters, ordering,
  pagination totals, statistics, and CSV. REST/GraphQL/MCP share the same service;
  hiding values only while constructing DTOs is insufficient.
- Filter visible Frame/Revision inputs before selection and exclude retired sources.
  Returned source IDs, candidate counts, runtime, and missing/ambiguous states must not
  reveal hidden inputs. Runtime uses actually selected artifacts/revisions, deduplicated
  by file, not every file associated with a Geometry.
- Add profile-source relations for role, component, Frame, Revision, Protocol, and
  purpose such as electronic energy or thermal correction, protected by FKs. Mark
  cached legacy profiles with unrecoverable provenance as requiring recomputation.
- Stop using global MappedReaction bounds as final restricted-user results or predicates.
  Preserve DTO compatibility while providing values from authorized source projections
  and explicit states when unavailable.
- Acceptance: A's answers do not change due solely to B-private calculations, reparses,
  or changes confined to B-private visibility. Anonymous/public reads are equally isolated.
  Unpublishing, revocation, and retirement remove access on the next request. Authorized
  multi-project users get correct combined-source results across all transports.

### DB-02: Explicit Calculation Selection

- Status: `partial` (Batch A tie/conflict handling). Depends on DB-01 source-set rules;
  complete protocol policy and DB-04 effective-revision selection remain.
- Preserve candidates by complete protocol and physical context instead of permanently
  reducing each Geometry to one highest-ranked result. Preserve software, version, and
  normalized settings; cross-software compatibility requires an explicit policy.
- Return distinct candidates or `ambiguous` for equally scored different protocols.
  Conflicting results under the same protocol also cannot be resolved by insertion time.
  Use a deterministic representative only after units, semantics, and values are
  equivalent within declared tolerances.
- Restrict effective revisions first, then apply calculation status/QC and the declared
  selection policy. Persist policy version, candidates, rejection reasons, and exact
  sources. Composite electronic energies and thermal corrections require compatible contexts.
- Acceptance: cover unknown tied functionals, tied distinct bases, equivalent duplicates,
  conflicts, missing protocols, cross-software cases, and incompatible solvents/conditions.
  Import order does not change scientific selection; corrected reparses can become effective.

### DB-03: Actual Profile Range Matches

- Status: `partial` (Batch A exact predicate). Depends on DB-01 authorization;
  isolated EXPLAIN and scale acceptance remain, and this fix can proceed before DB-02/DB-04.
- Keep bounds only as a prefilter and use an actual visible-profile `EXISTS` predicate.
  Activation/reaction energy, conditions, and protocol filters must hold on the same
  compatible profile rather than different profiles independently.
- Share final predicates across lists, totals, statistics, and export. Sort values must
  also come from matching visible sources.
- Acceptance: 5 and 25 do not match 10–20; boundary behavior at 10 and 20 is explicit;
  NULL is not zero; conditions cannot be assembled from separate profiles. EXPLAIN
  confirms there is no unbounded repeated scan in the prefilter/exact-filter combination.

### DB-04: Effective Revisions, Evidence Versions, and Snapshots

- Status: `todo`. Depends on DB-00; coordinate interfaces with DB-02 without blocking DB-01.
- Add explicit Artifact revision selection and its history. Each Artifact/selection policy
  has at most one current selection pointing to its own revision. Use version checks or
  row locks for concurrent promotion.
- Backfill the highest successful revision that meets QC; leave no selection when none
  qualifies. Partial and exceptional cases require review. Automatic promotion of a success
  follows a fixed policy, not a guess based on creation time.
- Default scientific queries use effective inputs; historical or explicitly pinned revision
  queries are separate. Align project Geometry catalogues, frame_count, and energy sources
  with this rule; label historical counts explicitly if retained.
- Separate membership identity from append-only match evidence. Pin actual revisions,
  Frames, inferences, protocols, formulas, units/standard states, selection policy, and
  input hashes in analysis snapshots. Current projections can be rebuilt; published
  snapshots are not deleted and regenerated. Mark missing legacy evidence rather than
  inventing historical provenance.
- Acceptance: corrected successes, failed/partial reparses, concurrent promotion, and
  historical selection have deterministic behavior. Snapshots reproduce with pinned inputs;
  policy upgrades preserve old results; revocation still prevents historical disclosure.

### DB-05: Shared-Ancestry and Scientific Constraints

- Status: `partial` (project ownership backfill, historical containment, and write-side reuse
  boundary shipped). Depends on DB-00; design effective-revision constraints with DB-04.
- Migration `0035_project_owned_derived_data` adds `project_id` to derived roots and records
  historically ambiguous roots in `derived_data_isolation_quarantine`; source files and parsed
  facts remain intact, while ordinary queries exclude quarantined rows.
- Audit and enforce shared logical reaction for mapped/logical participants; shared mapped
  reaction for nodes and bound participants; consistent inference ingestion/revision/frame
  and logical/mapped reaction links; and the same Artifact for reparse_of.
- Array and result owner must share a Frame. A Frame's Geometry, topology derivation, and
  electronic state must agree. Prefer parent composite unique keys and composite FKs;
  use controlled constraint triggers only for relations that require cross-table checks.
- Backfill and validate concrete_topology_id before adding NOT NULL and removing the
  implicit logical-topology fallback. Keep unresolved rows and their original values in an
  auditable remediation list; do not choose arbitrary topologies to satisfy constraints.
- Acceptance: direct invalid SQL inserts/updates fail for every relationship, while valid
  batch writes, deletions, and rollbacks pass. No dangling references; all new FK/CHECK
  constraints validated; compatibility writers have an explicit removal milestone.

### DB-06: Decouple Derived Refreshes from Fact Transactions

- Status: `todo`. Depends on DB-01, DB-04, DB-05, and the DB-00 write baseline.
- Write deduplicated refresh tasks in the fact transaction. Cover new Frames, revision
  selection, link changes, retirement, and policy changes. Use bounded batches,
  SKIP LOCKED, leases, retries, and observable errors.
- Key refreshes by target, scope, and policy. Generations/watermarks preserve events
  arriving while a worker runs; expired leases or old inputs cannot overwrite newer
  projections. Commit result publication and task progress transactionally.
- Expose pending/ready/failed and calculation versions where useful, never an unsafe
  global fallback. Authorization and source revocation remain immediate. Strongly
  consistent requests use bounded synchronous computation when needed.
- Keep exact identity uniqueness. Move costly graph expansion, thermodynamics, and
  counters incrementally; measure before removing triggers. Provide full rebuild,
  per-target retry, and reconciliation tools.
- Acceptance: concurrent imports, duplicate tasks, worker crashes, stale publication,
  and mid-refresh arrivals converge. Full rebuild equals incremental output. Report lock
  waits, transaction time, throughput, and lag without hiding weaker consistency.

### DB-07: Stable Geometry Matching and Degenerate Coordinates

- Status: `todo`. Depends on DB-00 coordinate fixtures and DB-04 evidence versioning.
- Add property tests for translations/rotations, valid atom permutations, collinear and
  near-collinear coordinates, fragments, mirrors/stereo, printing precision, and import order
  under fixed topology/electronic state.
- Separate exact representation from tolerance assignment. Low-precision observations
  cannot change published identities or expand historical clusters without bounds. Record
  ambiguity, permutations, rigid transforms, and policy evidence.
- Design reference-indexed internal coordinates or another faithful representation that
  handles degeneracy. Keep reconstruction-error checks and validate final Cartesian
  RMSD/maximum deviation. Canonical query projections cannot rewrite MolGR factual graphs.
- Introduce a new representation/policy version with old-to-new identity mappings or
  assignment versions. Do not replace old hashes or Frame sources in place. Compare
  migrations before switching the default matching policy.
- Acceptance: no precision regression; degenerate inputs are preserved or explicitly
  isolated; order-independent policy results; distinct electronic/stereo facts remain
  separate; old data remains interpretable using its original version.

### DB-08: Independent Inference Runs and Endpoints

- Status: `todo`. Depends on DB-04 and DB-05.
- Add a run identity covering Frame, imaginary mode, algorithm/version, and configuration
  hash. Separate retry attempts from scientific run identity and retain rejected/failed
  runs. Endpoints belong to runs and are unique by run/direction.
- Reaction/mapping links record the originating run and derivation type. Distinguish direct
  TS evidence from template-transferred mappings; reuse is not a new calculation fact.
  Explicitly select the currently adopted inference.
- Migrate old inference/endpoints into legacy runs using known provenance. Unknown historical
  modes/settings remain incomplete; current defaults cannot stand in for missing evidence.
- Acceptance: multiple modes, displacement ratios, and algorithms coexist on one Frame;
  identical retries do not duplicate scientific results; selection does not create a new
  ParseRevision; legacy reactions and source atom order remain traceable.

### DB-09: Array Tiering and Scale Validation

- Status: `todo`. Depends on DB-00 capacity measurements and DB-05 ownership rules for migration.
- Choose an externalization threshold from array-size quantiles, access frequency,
  duplication, and WAL/backup costs. Keep small arrays inline and large arrays behind
  verified immutable content references with relational Frame/owner metadata.
- Deduplicate using an explicit encoding schema, hash, and length. Verify object writes
  before publishing database references; reclaim crash orphans through grace-period GC.
  Derive permissions from owners, not content hashes.
- Support both old and new payload reads during migration, with explicit downloads and
  hash/dtype/shape/unit validation, never implicit ORM network IO. Preserve inline data
  until content checks, restore drills, and the rollback window are complete.
- Measure deep pagination, totals, and frequent filters too. Evidence determines keyset
  pagination, index consolidation/additions, and count strategy; do not assume partitioning,
  sharding, or caching is necessary. Assess concurrent index creation and lock budgets.
- Acceptance: payload fidelity and authorization preserved; write/publication crashes
  recover; GC retains referenced content. Record measured storage, WAL, restore, and query
  effects, the chosen threshold, and reasons for retaining any inline workloads.

## 4. Delivery Batches and Dependencies

| Batch | Items | Merge/cutover gate |
| --- | --- | --- |
| A: Results and permissions | Minimal DB-00 fixtures, DB-01, DB-03, DB-02 tie handling | Authorization matrix and incorrect-result counterexamples pass without waiting for full schema migration |
| B: Versions and integrity | DB-04, DB-05, complete DB-02, DB-08 | Explicit current/history semantics, database ancestry enforcement, reproducible source snapshots |
| C: Writes and geometry | DB-06, DB-07 | Rebuild/incremental agreement, crash convergence, geometry properties and version migration |
| D: Capacity and operations | DB-09 | Representative measurements, content fidelity, restore and rollback verification |

Batches establish delivery order, not a requirement to merge all items together.
Each item supplies reviewable code, migrations, tests, and documentation. Record
owners and PRs when assigned; do not invent dates, ownership, or completion.

## 5. Migration and Rollback

1. Audit read-only and export exceptions; verify backup and restore evidence. Check the
   actual head and add forward migrations. Do not edit published migrations or rely on
   historical statements that only the baseline exists.
2. Expand with tables/columns, indexes, and compatible readers/writers. FK/CHECK constraints
   may start NOT VALID and later use VALIDATE CONSTRAINT. Unique and NOT NULL constraints
   require their own supported procedures; NOT VALID is not a generic mechanism.
3. Backfill in bounded stable-key batches with checkpoints, retry safety, and failure
   reasons. Execution is not correctness proof. Missing evidence stays explicit.
4. Verify counts, source FKs, payload hashes, effective revisions, authorization sets,
   and query differences. Explain intended changes such as removing private inputs
   instead of requiring unconditional equality with old results.
5. Switch gradually by project/feature and record schema, policy, watermarks, and monitoring.
   Never roll back an authorization repair to unsafe global summaries. Disable the
   derived feature with an explicit unavailable state when necessary.
6. Contract after the compatibility window and recovery validation. Removing old reads,
   triggers, or payloads needs an exact scope and recoverability record for data deletions.
   Destructive downgrades are not an automatic schema rollback strategy.

## 6. Verification and Completion

Run item-specific regressions before repository release checks. Database/object tests
must explicitly target isolated infrastructure. The commands below are the acceptance
set for the complete refactor; the subset executed in this round is recorded above:

```bash
uv run alembic heads
uv run alembic current
uv run alembic check
make lint
make type
make test
make test-db
make test-infra
```

`make test-infra` enables database and object tests; skipped Redis or other checks are
not passes. DTO/transport changes require REST/GraphQL/MCP contract checks. Frontend
consumer changes require `make frontend-check`, `make frontend-build`, and relevant E2E.
Migration PRs verify both empty-schema and previous-supported-version upgrades, including
ORM, generated columns, indexes, and triggers. Alembic check does not test trigger behavior.

Performance evidence records distributions, concurrency, cold/warm cache, P50/P95/P99,
timeouts/errors, lock waits, transaction duration, throughput, WAL, and refresh lag.
Register budgets from DB-00 before performance changes rather than relaxing them after
seeing results. Follow the [operations runbook](operations-runbook.md) for restore checks;
RTO/RPO are measurements, not estimates.

Complete each record before marking an item done. Database unavailability does not
block recording this plan, but prevents acceptance of database-dependent implementation.

| Field | Current value / recording rule |
| --- | --- |
| Item / status | DB-00 `todo`; DB-01 through DB-03 `partial` (Batch A minimum fixes); DB-04 `todo`; DB-05 `partial` (historical project-ownership isolation); DB-06 through DB-09 `todo` |
| Owner / PR / Git revision | Record during implementation |
| Schema / policy / data snapshot | Record during validation |
| Commands and results | Separate passed, failed, skipped, unavailable, and evidence locations |
| Backfill and exceptions | Scanned/migrated counts, failures, checkpoints, validation |
| Security and correctness | Item-specific counterexamples, cross-project matrix, ancestry constraints |
| Performance and recovery | Applicable measurements, restore drill, rollback validation |
| Documentation | Update both current-contract editions and ERD as models change; retain this plan's historical evidence |
