# Data Model and Storage Boundaries

[中文](../data-model.md) | [Documentation index](README.md)

> Current implementation contract. Dated design and acceptance records are
> historical evidence, not a substitute for this document.

## Domain Axes

```text
Raw facts
RustFS object -> ArtifactFile -> ParseRevision -> CalculationSegment -> CalculationFrame
                                      |                                  |
                                      `-> ArtifactIngestion               `-> Geometry

Chemical identity
MolecularFormula -> MolecularTopology -> Geometry
                         |
                         `-> MolecularTopologyDerivation

Reaction path
TransitionStateInference -> LogicalReaction -> MappedReaction -> Node -> NodeGeometry -> Geometry
```

`ArtifactFile`, `ParseRevision`, and `CalculationFrame` provide file and parser
provenance, not chemical or reaction identity. `MolecularFormula ->
MolecularTopology -> Geometry` holds reusable chemical facts. The axes meet at
a frame's Geometry binding. Organizations, projects, memberships, and external
identities form a separate authorization axis.

## Formula, Topology, and Geometry

`MolecularFormula` records elements and isotopes only. Its authoritative range
query representation is `element_count_vector`, ordered by atomic number;
frequent exact compositions are additionally projected into GIN-indexed
`element_count_tokens`. Charge, radical electrons, bonds, and stereochemistry
belong to a topology.

`MolecularTopology` stores the MolGR-reconstructed graph, formal charge,
radical-electron count, fragment count, stereochemical state, and graph hash.
For a trusted MolGR graph, the database clears transient properties and builds
RDKit ring information only. It does **not** call `SanitizeMol`, add hydrogen,
infer radicals, or canonically reorder atoms. MolGR atom order and electronic
annotations therefore remain aligned with quantum-chemical coordinates.
`suspicious_fallback` graphs are persisted with provenance, but cannot be used
as TS endpoint reactants or products.

`canonical_isomeric_smiles` remains the public field name for compatibility;
its value is an explicit-hydrogen isomeric SMILES projection of the MolGR graph,
not a scaffold or implicit-hydrogen representation. A graph that cannot produce
SMILES is still stored and inspectable through its binary Mol and `graph_hash`.
The versioned graph serialization includes elements, isotopes, bonds, formal
charges, radical electrons, stereochemistry, and explicit hydrogens. A
`MolecularTopologyDerivation` records MolOP/MolGR versions, configuration, and
reconstruction evidence independently from graph identity.

`Geometry` is an E(3)-invariant coordinate equivalence class under one
topology. It stores one display RDKit 3D Mol, lossless internal coordinates, and
database query projections. Its identity includes topology, versioned internal
coordinates, **total charge**, and **spin multiplicity**: equal coordinates with
a different electronic state are distinct Geometry rows. A CalculationFrame
retains source Cartesian coordinates, print precision, source-to-geometry
permutation, and rigid transform. Vibration, gradient, and Hessian arrays are
always interpreted in source atom order. XYZ downloads write total charge and
multiplicity in the comment line.

## Ingestion and Parse States

Browser uploads, batch uploads, explicit reparse, and the local
`tricycle-import-artifacts` CLI use the same application upload service. The
CLI differs only by using local paths as its byte source. An upload creates a
pending `ArtifactFile`, writes and verifies the RustFS/S3 object, marks it
`available`, then parses a `calculation_output`. Object identity, content hash,
parse revisions, and scientific facts are never overwritten in place.

One parse creates one `ParseRevision` and persists every recoverable segment
and frame. A frame-level normalization or persistence error does not discard
other successful frames in the same file. A file with no recoverable calculation
frame remains an artifact, but is `filtered`, not `succeeded`. Visible
`ArtifactIngestion` states are `pending`, `succeeded`, `partial`, `filtered`,
and `failed`. Explicit reparse appends a new revision and preserves a previous
successful result.

Local import is a streaming candidate queue. `IMPORT_PIPELINE_WINDOW_FILES`
sets the prefetched candidate pool, `TRICYCLE_MOLOP_BATCH_N_JOBS` sets concurrent
file workers, and `IMPORT_COMMIT_BATCH_FILES` only sets the completed-result
persistence/checkpoint microbatch. The candidate pool should exceed the worker
count so a completed or timed-out task is replaced immediately. The per-file
budget uses `TRICYCLE_MOLOP_FILE_PARSE_TIMEOUT_SECONDS` for 10 MiB and scales
linearly with source size. A timeout stops only that file and frees its slot.
Bound `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, and `MKL_NUM_THREADS`; file
worker concurrency is not a native-thread limit.

## TS Endpoint Inference and Reactions

Every frame is persisted normally. TS and `suspicious_fallback` Frames request
MolOP evidence from positive and negative imaginary-mode displacements. A
fallback Frame itself is still examined; an inference is rejected only when an
endpoint is `suspicious_fallback`. Atom order is inherently one-to-one across
the reference coordinates, vibration mode, and displacements, so inference does
not use graph isomorphism or canonical atom reordering.

Endpoint total charge and multiplicity inherit from the TS. When MolGR returns
multiple fragments, each fragment's formal charge and radical electrons are
summed from atom annotations actually present in the graph. The service stores
topology, charge, multiplicity, displacement ratio, and source-coordinate hash
for each endpoint. Reactant and product graphs being different is the expected
inference outcome; explicit-H SMILES, radical-stripped SMILES, and graph hashes
are never required to match across the two sides.

`LogicalReaction` deduplicates a directed topology multiset with chemical
electron annotations. `MappedReaction` stores one exact atom mapping, and
Node/NodeGeometry binds reaction nodes to Geometry rows. The system never
auto-labels an inferred reaction as cycloaddition or any other class. Mode sign
does not itself define chemical direction; the direction policy and rejection
reasons are persisted as inference evidence. Reaction and TS queries can filter
whether the standard reactant and product topology multisets differ.

## Queries, Visibility, and Derived Values

All artifact, frame, geometry, topology, reaction, and inference reads enforce
project visibility. Large Geometry lists use the project geometry catalog and
inexpensive predicates such as elemental composition before structural,
frequency, or thermochemical conditions. Pagination is deterministic, supports
explicit sort fields and direction, and the UI prefetches adjacent pages while
showing an in-progress state.

RDKit binary Mol, reaction, and fingerprints are query projections, not the
authoritative graph or geometry identity. `GeometryEnergyView` and reaction
thermodynamic profiles are versioned derived read models that select a source
Frame/Protocol and report `selected`, `missing`, or `ambiguous`; they never
rewrite original observations.

## Storage and Deletion

Raw files live in RustFS/S3 and PostgreSQL records their locator, content hash,
authorization, and parse results. Buckets are private and all downloads pass
project authorization in the API. Upload compensation removes the current
pending object after failure; `tricycle-rustfs-gc` is an infrequent, auditable
crash-recovery measure. Artifact deletion creates a `retired` tombstone and
removes the managed object without erasing established audit facts.
