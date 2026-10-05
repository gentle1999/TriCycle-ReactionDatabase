---
name: mapped-reaction-exports
description: Maintain mapped-reaction TS geometry and UniTS exports whose per-atom values must align with atom-map numbers.
---

# Mapped-reaction exports

Use this skill when adding or changing mapped-reaction TS geometry JSONL, UniTS JSONL/NPY, or their atom-indexed calculation data.

Read the [atom-indexing contract](../../../docs/mapped-reaction-atom-indexing.md) and the [detailed export guide](../../../docs/units-ts-dataset-export.md) before changing an output schema. The English contract is available at `../../../docs/en/mapped-reaction-atom-indexing.md`.

- Treat map number `n` as exported atom index `n - 1`. Keep Geometry atoms, Mol blocks, coordinates, atom features, graph edge endpoints/features, reactive indices, and fragment indices in that same order.
- Require a verified, complete, unique `1..N` Geometry mapping and conserved map-to-element/isotope identity. Skip or fail unverifiable data; never guess an atom permutation.
- For calculation data, compose the observed-frame-to-Geometry permutation with the Geometry-to-map permutation. Declare atom axes by scientific-array kind; do not infer them from array shape. Reorder every atom-indexed axis, including both Cartesian block axes of Hessians, and keep subset indices or NMR metadata aligned with the values.
- Preserve source coordinate reference and units while permuting; do not rotate or mutate stored source arrays. Leave components of data with no atom-indexed axes in their original order.
- Use `(mapped_reaction_smiles, atom_map_number)` for reaction-local atom identity. A reaction may have multiple TS bindings: use `geometry_binding_id` to identify a Geometry sample and `frame_id` to identify calculation-frame data. Do not treat the reaction key alone as a unique TS sample key.
- Generic Geometry-ID SDF/XYZ and TS-anchor exports without a selected mapped reaction keep Geometry/source-frame order; do not claim map-order semantics for them.

When an exported atom-indexed field or scientific-array kind changes, add or update a non-identity permutation test that checks array values, index metadata, and atom-map numbers together. Verify that all fields in each affected export path retain the same atom indexing.
