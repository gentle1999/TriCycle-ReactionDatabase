# RDKit Mol Database Round-Trip Contract

[中文](../rdkit-mol-roundtrip.md) | [Documentation index](README.md)

> Verified persistence contract. This page describes the supported boundary,
> not permission to mutate a trusted MolGR graph during ingestion.

## Contract

The PostgreSQL RDKit cartridge serializes and restores a binary `Chem.Mol` used
for structure-query projections and display. It preserves atom order and an
attached conformer within cartridge precision, but it is not the lossless source
of Geometry identity. Lossless internal coordinates and source Frame coordinates
remain the authority for matching and vibration-oriented arrays.

For a trusted MolGR graph, normalization removes transient properties and builds
ring information only. It does not run RDKit sanitization, add implicit hydrogens,
infer radicals, or canonicalize atom ordering. The stored explicit-H SMILES,
formal charge, and radical-electron count must agree with the MolGR graph.

## Usage Rules

- Store raw source coordinates and source-to-Geometry permutation on the Frame.
- Store total charge and multiplicity on Geometry and include both in its identity.
- Use RDKit GiST/fingerprint indexes for indexed structure predicates; never
  reconstruct a Mol or fingerprint per database row during ordinary queries.
- Treat cartridge precision and property-loss behavior as a tested transport
  boundary, not a reason to mutate the scientific graph.

Run `make test-db` after a RDKit, PostgreSQL, MolAlchemy, or driver upgrade. The
test suite covers binary Mol round trips, conformer precision, property behavior,
substructure queries, indexes, and readiness.
