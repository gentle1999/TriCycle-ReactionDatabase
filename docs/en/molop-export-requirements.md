# MolOP Calculation-Result Export Requirements

[中文](../molop-export-requirements.md) | [Documentation index](README.md)

> Status: the contract is implemented at the integration boundary. This page
> states the requirements used when upgrading MolOP/MolGR or adding parsers.

## Purpose and Boundary

MolOP exports parser facts; TriCycle persists those facts, reconstructs graphs
through MolGR, and owns database identity, authorization, object storage, and
query projections. The integration consumes MolOP public DTO/provenance rather
than private object internals or a source checkout commit.

## Required Export Facts

- Stable parser/provenance/version information and a portable JSON DTO.
- Segment and Frame boundaries with source locators and parser diagnostics.
- Source-order atoms and Cartesian coordinates without hidden reordering.
- Charge, multiplicity, electronic state, energies, units, frequencies, and
  typed numerical arrays with dtype, shape, unit, and byte provenance.
- Optimization, SCF, termination, thermochemistry, gradient/force, and ORCA
  parser-status facts where present.
- TS role evidence and positive/negative imaginary-mode endpoint candidates.

MolOP `0.2.11` may collect frame-role/source-locator evidence without implicitly
reconstructing graphs. Evidence collection remains enabled. MolGR is responsible
for graph reconstruction; the database treats a trusted MolGR graph as
authoritative and does not apply extra chemical repair, atom canonicalization,
or implicit-H inference.

## Atomic and Electronic-State Contract

Frame coordinates, vibration modes, and endpoint displacement coordinates share
the same direct source atom order. TS inference therefore uses that correspondence
directly and does not perform graph isomorphism. A Geometry's charge and spin
multiplicity come from the source Frame and are part of Geometry identity. TS
endpoint totals preserve the TS electronic state; fragment radical counts are
derived from actual MolGR atom annotations.

The topology presentation field retains its compatibility name
`canonical_isomeric_smiles`, but stores explicit-H isomeric SMILES. Failure to
emit a usable SMILES must not discard a valid binary graph or computation frame.

## Upgrade Acceptance

Pin a released MolOP/MolGR version, update `uv.lock`, and run parser, topology,
database, and fixture tests. Verify frame counts, source-order correspondence,
charge/multiplicity, explicit-H graph output, TS endpoint evidence, and absence
of automatic reaction-class assignment. Reparse requires a new `ParseRevision`;
it never overwrites prior scientific evidence.
