# Business Model

[中文](../business-model.md) | [Documentation index](README.md)

> Current product boundary. The historical Chinese title refers to
> cycloaddition, but the product does not automatically classify inferred
> reactions as cycloadditions.

## User-Facing Objects

| Object | Meaning | Identity or mutability |
| --- | --- | --- |
| ArtifactFile | A project's original input, output, or auxiliary file | Content hash and object location are immutable; the artifact can be retired |
| ParseRevision | One auditable parse of an artifact | Appended; a prior revision is never overwritten |
| CalculationFrame | One recovered calculation frame | Keeps source coordinates, method, frequency, and array provenance |
| MolecularTopology | A MolGR reconstructed graph | Explicit H, bonds, charge, radical electrons, and stereochemistry identify it |
| Geometry | A topology, coordinates, charge, and multiplicity fact | Same coordinates with a different electronic state are distinct |
| TransitionStateInference | Imaginary-mode evidence from one TS frame | May succeed, be rejected, or fail without mutating the Frame |
| LogicalReaction | Net reaction between topology sides | Independent of file, coordinates, and reaction name |
| MappedReaction | One exact atom mapping and path nodes | A net reaction may have several mappings |

## Ingestion Workflow

The uploader chooses a project and submits files. Browser, batch API, and local
CLI flow through the same upload service: raw bytes are verified in object
storage before a parse revision is created. MolOP parses calculation outputs and
each recoverable frame is persisted independently. A bad frame yields a
`partial` outcome rather than swallowing good frames. A file with no recoverable
frame remains downloadable and auditable as `filtered`. The UI renders `pending`
as parsing in progress, not as a failure.

A project manager can rename an artifact, adjust visibility, request reparse, or
retire it. Content, hashes, and established observations are never changed in
place. Reparse consumes the same source bytes and appends a revision, allowing
comparison of different MolOP/MolGR versions or parser configurations.

## Chemical Facts and Presentation

MolGR output is the authoritative graph source. Trusted graphs receive RDKit
ring metadata only: the service does not chemically sanitize them, infer
implicit hydrogens, or canonically reorder atoms. Explicit-H SMILES is the
shared display and exact-string representation; `canonical_isomeric_smiles` is
a compatibility field name. ChemDoodle disables implicit-hydrogen inference so
the browser does not display atoms absent from the persisted graph.

A Geometry detail shows total charge, spin multiplicity, and coordinates and
offers XYZ/SDF downloads. XYZ comments contain charge and multiplicity. Frame
charge/multiplicity creates the Geometry identity, and endpoint totals inherit
the TS electronic state. Fragmentation must not discard graph-recorded radical
electrons.

## TS and Reaction Paths

For TS and `suspicious_fallback` Frames, MolOP receives the positive and
negative imaginary-mode displacements in source atom order. Both endpoints and
all rejection/error evidence are retained. A fallback endpoint cannot be a
reactant/product, while a fallback Frame itself does not skip evidence
collection. Source atom correspondence is direct, so no expensive graph
isomorphism is needed.

Successful endpoints form a reaction deduplicated by topology multiset. A
topology change is normal for a successful inference. The system does not
require reactant/product equality and does not automatically set a reaction
class. Reaction and TS queries can filter whether endpoint topologies changed
and can sort by the supported fields.

## Permissions and Public Scope

Organizations group projects and projects authorize data. Membership permissions
control upload, read, download, reparse, deletion, and project administration.
`public` artifacts are anonymously readable; default `project` data is only
available to project members through the API. Production identity comes from
OIDC; a fixed development identity exists only for local development and tests.
An MCP token authorizes only the MCP endpoint and does not broaden REST or
GraphQL access.

## Non-Goals

The project does not schedule HPC jobs, modify a result to force a reaction
label, replace a general electronic lab notebook, or store experimental yields,
conditions, or literature claims. It stores traceable calculation files, frames,
molecular graphs, geometries, and inference evidence.
