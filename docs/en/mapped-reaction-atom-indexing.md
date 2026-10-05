# Atom indexing in mapped-reaction exports

Atom order in mapped-reaction exports is defined by atom-map numbers, not by source Geometry order, calculation-file order, or SMILES traversal order. Map number `n` corresponds to zero-based index `n - 1` in every per-atom array.

## Scope

This contract applies to mapped-reaction TS Geometry JSONL and UniTS TS JSONL/NPY exports. The implementation is centered in `mapped_geometry_atom_order.py`, `mapped_calculation_order.py`, `mapped_reaction_geometry_export.py`, and `units_ts_dataset_export.py`.

A Geometry binding must map the reaction atoms completely to Geometry atoms. Map numbers must be unique and contiguous from `1..N`, and each map must identify the same element and isotope on both reaction sides and in the Geometry. Skip or fail records that cannot be verified; do not continue with a guessed permutation.

## Ordering and data coverage

- `atoms`, the RDKit Mol block, and UniTS `atom_symbols`, atomic numbers, masses, node features, and coordinates are ordered by ascending map number. Map `n` is at index `n - 1`.
- Graph edge endpoints, reactive atoms, reactive bond/angle indices, and fragment indices use that same atom order. Edge features remain aligned with their corresponding edge records.
- Calculation-frame source coordinates are projected first through the frame-to-Geometry mapping and then through the Geometry-to-atom-map binding. Reordering does not rotate or translate the coordinates; they remain in the source Cartesian reference frame.
- Per-atom scientific arrays are reordered using atom axes declared for their kinds; do not infer axes from array shape alone. Both `3N` Cartesian Hessian axes are reordered in atom blocks. Modes, forces, atomic populations, bond-order matrices, Fukui values, and fractional occupations use their declared axes.
- NMR coupling matrices and their atom subsets are reordered together. Export zero-based `atom_indices` and one-based `atom_map_numbers`. Per-atom NMR shielding/principal-value records expose both zero-based `atom_index` and one-based `atom_map_number`.
- Molecular vectors and frequencies without atom-indexed axes retain their component order. Never mutate source arrays in the database.

When adding a per-atom field or scientific-array kind, declare its atom axes and transformation explicitly. Add a test with a non-identity permutation that checks values, index metadata, and map numbers stay aligned. Do not silently treat an unclassified array as atom-ordered.

## Unique keys and sample identity

`(mapped_reaction_smiles, atom_map_number)` identifies an atom within a mapped reaction. For fields represented only as zero-based arrays, read map `n` from `array[n - 1]`; use the explicit `atom_map_number` field when joining per-atom records.

Mapped reaction SMILES is a reaction key, not a TS-sample key. One mapped reaction can have multiple Geometry bindings, so the JSONL `key` can repeat. Use `(key, geometry_binding_id)` to distinguish Geometry samples, and add `frame_id` to distinguish calculation-frame data. UniTS samples also include `mapped_reaction_id`, `geometry_binding_id`, and `geometry_id`.

Generic Geometry-ID SDF/XYZ downloads and calculation-frame TS-anchor exports without a selected mapped reaction have no unique mapped-reaction order; they retain Geometry or source-frame order. Use the mapped-reaction exports when atom data must join directly to atom maps.

For field and format details, see the [UniTS TS Geometry export guide](../units-ts-dataset-export.md). When implementing or maintaining these exports, also follow the [mapped-reaction-exports skill](../../.agents/skills/mapped-reaction-exports/SKILL.md).
