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
                         |                 `-> MolecularTopologyDerivation
                         `-> MolecularTopologyAbstraction
                              specific_topology -> general_topology

Reaction path
TransitionStateInference -> LogicalReaction -> MappedReaction -> Node -> NodeGeometry -> Geometry

Logical/concrete membership
LogicalReactionParticipant -> LogicalParticipantConcreteTopology -> concrete MolecularTopology
MappedReactionParticipant -> concrete MolecularTopology
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

Geometry matching first fixes topology, canonicalization version, total charge,
and spin multiplicity, then compares the versioned internal coordinates. If
several candidates satisfy the equivalence tolerance, a deterministic nearest
geometry is selected. This resolves Geometry-assignment ambiguity only; it does
not modify topology, E/Z, R/S, explicit hydrogens, or atom mapping.

### Stereo topology abstraction and concrete membership

The model has two topology semantics, both represented by independent
`MolecularTopology` rows:

- **Strict concrete topology:** reconstructed by MolGR from an actual
  calculation endpoint. It retains the atoms, bonds, explicit hydrogens,
  electronic annotations, and stereochemistry proven by that 3D endpoint. It
  is the fact source for Geometry and strict `MappedReaction` rows.
- **Logical/general topology:** materialized only when an abstraction is
  requested. It is copied from a strict topology while selectively clearing
  specified stereo features. It is a LogicalReaction query topology; it has no
  calculation coordinates and never overwrites a concrete topology.

`MolecularTopologyAbstraction` stores a directed edge:

```text
specific concrete topology -> general abstract topology
```

An edge requires the same elemental/isotopic composition, atom count, formal
charge, and connectivity. Every stereo constraint retained by the general
query must be satisfied by the concrete target, and the concrete target must
have at least one assigned atom- or bond-stereo feature omitted by the general
topology. Writes use stereo-aware graph matching and reject self-edges and
cycles. Each edge stores the abstraction policy, match schema, atom
correspondence, and abstracted features for audit and versioned recomputation.

`molecular_topology.is_stereo_abstraction_upstream` is an explicit capability
marker for a valid abstraction upstream. It is not inferred from `unknown`,
`unassigned`, `ambiguous`, or ordinary parse failure states. Only a reviewed
abstraction projection may set it. If no marked matching upstream exists, the
effective upstream is the topology itself; no self-edge is persisted.

The relation is a lazily materialized DAG, not a stereoisomer generator:

- strict topology creation searches existing marked upstreams with the same
  composition and registers matching edges;
- abstraction creation checks already materialized concrete downstreams and
  backfills the reverse side of the relation;
- concrete-member queries traverse existing edges from
  `general_topology -> specific_topology`;
- the system never expands the power set of all theoretical stereo features or
  creates an unrequested hypothetical configuration.

For example, two independent stereo centres may form a diamond: a two-centre
concrete topology connects to one-centre-A and one-centre-B topologies, which
both connect to a common no-centre-specific topology. A concrete topology can
have multiple abstraction paths, and one general topology can converge multiple
concrete descendants.

An abstraction clears only explicitly selected features. Clearing E/Z also
clears the adjacent single-bond direction flags used to serialize that E/Z;
unrelated E/Z, R/S, and other stereo information must remain. A global
`useChirality=False` match or a rule that clears all stereo merely because an
atom is nitrogen is not an acceptable substitute for selective projection.

Chemistry policies are code-owned and versioned in
`src/tricycle_reaction_db/core/chemistry_config.py`:

| Policy | Current value | Meaning |
| --- | --- | --- |
| `CALCULATION_PROTOCOL_VERSION` | `calculation-protocol-v1` | calculation-protocol identity |
| `FORMULA_COMPOSITION_VERSION` | `formula-composition-v1` | formula/isotopic-composition identity |
| `TOPOLOGY_IDENTITY_VERSION` | `topology-identity-v1` | strict molecular-graph identity |
| `TOPOLOGY_SOURCE_ORDER_STEREO_IDENTITY_VERSION` | `topology-source-order-stereo-identity-v1` | source-order and stereo identity |
| `TOPOLOGY_DERIVATION_VERSION` | `topology-derivation-v1` | topology reconstruction evidence |
| `GEOMETRY_CANONICALIZATION_VERSION` | `geometry-internal-coordinates-v1` | Geometry internal-coordinate identity |
| `STEREO_ABSTRACTION_POLICY_VERSION` | `topology-stereo-abstraction-v1` | DAG edge and projection policy |
| `STEREO_ABSTRACTION_MATCH_SCHEMA_VERSION` | `topology-stereo-abstraction-match-v1` | abstraction evidence format |
| `INVERSION_STEREO_PROJECTION_POLICY_VERSION` | `reaction-inversion-stereo-projection-v1` | reaction-level dual-sided inversion projection |
| `LOGICAL_PARTICIPANT_CONCRETE_MATCH_POLICY_VERSION` | `logical-participant-concrete-match-v1` | logical-to-concrete participant matching |
| `GEOMETRY_MATCH_POLICY_VERSION` | `geometry-internal-coordinate-match-v4` | Geometry matching policy |
| `REACTION_GEOMETRY_LINK_METHOD` | `topology-identity` | reaction/Geometry linking method |
| `REACTION_GEOMETRY_LINK_POLICY_VERSION` | `reaction-geometry-link-v1` | reaction/Geometry linking policy |
| `GEOMETRY_ENERGY_POLICY_VERSION` | `geometry-energy-view-v1` | Geometry energy read model |
| `MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION` | `mapped-reaction-thermodynamics-v1` | mapped-reaction thermodynamic projection |

The only currently registered inversion-labile rule is
`neutral-trivalent-nitrogen`, atom SMARTS `[N;X3;v3;+0]`. It is an atom rule,
not a bond rule. After matching the atom, the service examines adjacent bonds
and stereo reference atoms to find dependent C=N E/Z or other neighboring
stereo features. Adding another reversible centre requires a reviewed rule and
version; an environment variable must not silently change persisted chemical
identity.

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

`ParseRevision.running_time_seconds` stores the file-level calculation runtime
reported by MolOP. `CalculationFrame.running_time_seconds` stores per-frame
runtime; the file-level value has different semantics and must not be replaced
by a sum of frame runtimes.

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

### TS endpoints are the 3D fact source

Every frame is persisted normally. TS and `suspicious_fallback` Frames request
MolOP evidence from positive and negative imaginary-mode displacements. A
fallback Frame itself is still examined; an inference is rejected only when an
endpoint is `suspicious_fallback`. The reference coordinates, vibration mode,
and both displacements use one source atom order, so endpoint inference does
not recover correspondence through graph isomorphism or perform canonical atom
reordering.

Endpoint total charge and multiplicity inherit from the TS. For multiple
fragments, formal charge and radical electrons are summed from the actual atom
annotations. Strict reaction facts are formed in this order:

```text
TS frame + imaginary mode
  -> positive/negative displaced 3D endpoints
  -> MolGR endpoint graph with source atom order
  -> strict concrete MolecularTopology and Geometry
  -> exact atom maps from source correspondence
  -> strict MappedReaction and mapped_reaction_smiles
```

`mapped_reaction_smiles` is an explicit-H, isomeric, atom-mapped serialization
of the strict endpoint 3D graphs. It is a query/persistence projection, not the
source of topology or Geometry. A normal 2D SMILES must not be used to project
stereochemistry back onto the 3D facts. Endpoint topology, charge,
multiplicity, displacement ratio, and source-coordinate hash remain auditable
inference evidence. Different reactant and product graphs are expected; their
explicit-H SMILES, radical-stripped SMILES, and graph hashes need not match.

Reactant/product order is a storage convention, not the direction of an
inversion rule. The sign of the imaginary mode does not itself define chemical
direction; direction policy and all rejection reasons remain inference evidence.

### Strict reactions, logical reactions, and inversion-labile centres

`MappedReaction` is one strict concrete reaction: every participant has a
concrete topology, a complete atom mapping, and mapped reaction text serialized
from those strict topologies. `LogicalReaction` is the retrieval abstraction;
its participants use abstract topologies and it is not a Geometry or a concrete
configuration. One logical reaction may contain multiple configuration-specific
mapped reactions.

Logical projection runs only for a complete strict mapping and treats both
sides symmetrically:

```text
strict mapped endpoints
  -> inspect inversion-labile atoms on both sides
  -> collect authoritative atom-map numbers
  -> propagate the same map set to both endpoints
  -> clear only stereo features dependent on those maps
  -> persist/reuse logical topologies and abstraction DAG edges
  -> create/reuse LogicalReaction
```

The only registered rule is `[N;X3;v3;+0]`, neutral trivalent nitrogen. The
rule matches an **atom**, not a bond; after matching it, the service examines
adjacent bonds, neighboring atoms, and double-bond stereo reference atoms. Thus
an sp3 N in a product or TS can supply the labile atom map even when the
corresponding reactant N is sp2, allowing the dependent C=N E/Z to be cleared.
The rule is dual-sided and must not be applied only to a presumed reactant or
product.

Projection clears only the selected atom chirality, dependent double-bond E/Z,
and the adjacent single-bond directions used to serialize that E/Z. Unrelated
E/Z, R/S, connectivity, explicit hydrogens, formal charge, and radical
annotations remain. Without a complete mapping, the rule cannot be propagated
across endpoints and the service must not fabricate a strict `MappedReaction`.

### Bidirectional logical/concrete membership

`LogicalReactionParticipant.topology_id` stores the abstract query topology;
`MappedReactionParticipant.concrete_topology_id` stores the strict concrete
topology. They are connected independently through
`LogicalParticipantConcreteTopology`:

```text
LogicalReaction
  -> LogicalReactionParticipant (abstract topology)
      -> LogicalParticipantConcreteTopology
          -> concrete MolecularTopology

MappedReaction
  -> MappedReactionParticipant
      -> concrete MolecularTopology
```

Candidates are first narrowed by formula, atom count, and formal charge. The
abstract topology is then the query and the concrete topology is the target for
stereo-aware molecular-graph matching. Topology-hash equality is not the final
membership condition; a hash is only a candidate index. Matching must preserve
connectivity and every stereo constraint retained by the abstract query. It
must not equate different fragments, electronic states, or compositions.

Membership can exist without a complete reaction mapping. It records the match
policy, schema, candidate count, atom correspondence, and recomputable audit
metadata, but does not fabricate a `MappedReaction`. If multiple legal graph
matches exist, all candidates are retained and reaction-level mapping, side,
and occurrence constraints must resolve them; the first match is never chosen
arbitrarily.

Both creation orders are supported:

1. **Concrete topology before logical reaction:** when a logical participant is
   created, traverse already materialized DAG descendants and register every
   matching concrete member; do not generate hypothetical configurations.
2. **Logical reaction before concrete topology:** when a new topology is
   created, search existing logical participants with the same graph-match
   policy and register membership. If the LogicalReaction has a complete mapped
   reaction template, create a new strict mapped reaction; otherwise keep only
   the concrete membership.

### Mapping transfer for a new concrete configuration

A new concrete topology does not need to carry a new atom-map text. If it is a
member of a logical participant and a complete strict mapping template exists
under that LogicalReaction, mapping is transferred through two graph
correspondences:

```text
source concrete atom
  -> abstract topology temporary atom index
  -> target concrete atom
```

The existing mapped reaction is the only source of atom-map labels. Both source
and target concrete topologies must match the same abstract topology; composing
those correspondences yields complete target atom maps. The target SMILES is
serialized from the target 3D topology only after mapping is complete and is
never used as the mapping source.

If symmetry or stereo constraints produce multiple complete assignments, the
service records/raises mapping ambiguity and does not choose one arbitrarily.
After a successful transfer, `mapping_hash` is computed from the target strict
mapped SMILES. Within one LogicalReaction, `(logical_reaction_id,
mapping_hash)` is the strict text identity; the same canonical mapped text must
not create a second mapped-reaction row.

A template-derived mapped reaction shares the source mapped reaction's TS and
endpoint evidence and reusable Geometry links, while retaining its own mapped
reaction, participant, and node rows. It does not invent or duplicate new
calculation facts.

### Bidirectional Geometry/reaction binding

Geometry must bind a concrete topology:

```text
Geometry.topology_id == MappedReactionParticipant.concrete_topology_id
```

`MappedReactionNode` describes a path state, `MappedReactionNodeGeometry`
connects it to actual Geometry, and `MappedReactionNodeGeometryMapping` records
the Geometry atom order to mapped-atom correspondence and its verification
evidence. Mapped-reaction creation backtracks over existing concrete Geometry,
thermodynamic data, and no-imaginary-frequency frames; Geometry creation also
looks up existing mapped participants and fills the node/Geometry links. An
abstract logical topology has no 3D conformer and cannot serve as Geometry
evidence.

### Reaction identity and validation boundary

`LogicalReaction` deduplicates the two-sided abstract topology multiset,
side/participant order, and chemical electron annotations. It does not depend
on filenames, directories, coordinates, Geometry, or reaction names.
`MappedReaction` uses the canonical strict mapped-reaction text's
`mapping_hash` as text identity and requires conserved atom maps plus matching
participant count, role, and stoichiometry.

Persistence validation only confirms that the text agrees with the resolved
concrete topologies and atom maps, that RDKit can parse it, that canonical text
matches `mapping_hash`, and that both sides have the same map coverage. It must
not infer, correct, or rewrite E/Z, R/S, explicit hydrogens, or atom
correspondence already established by the 3D source. Inconsistency rejects
persistence and preserves error evidence instead of silently changing a label.

The system never auto-labels an inferred reaction as cycloaddition or another
class. Queries that need concrete configurations operate on `MappedReaction`
rows and may group the results by `LogicalReaction` for presentation.

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

Each mapped-reaction thermodynamic profile also stores file-level runtimes for
reactants, transition state, products, and the full path. Values are summed
over distinct `ArtifactFile` ids referenced by the selected calculation frames;
the full-path value uses the union across the three states. The CSV export
contains these four runtime columns. Per-frame runtime sums are not used.

## Storage and Deletion

Raw files live in RustFS/S3 and PostgreSQL records their locator, content hash,
authorization, and parse results. Buckets are private and all downloads pass
project authorization in the API. Upload compensation removes the current
pending object after failure; `tricycle-rustfs-gc` is an infrequent, auditable
crash-recovery measure. Artifact deletion creates a `retired` tombstone and
removes the managed object without erasing established audit facts.
