# UniTS TS geometry dataset export

The API and MCP tools create an asynchronous export job for persisted
`MappedReactionNodeGeometry` rows whose mapped-reaction node role is
`transition_state`. The exporter reads the project database's Geometry and
verified atom-map bindings; it does not scan uploaded files or a local artifact
directory.

## API flow

1. `POST /api/units-ts-datasets` with `{"project_id": "..."}`. The response is
   `202 Accepted` and includes a job ID, a status URL, and a unique download URL.
2. Poll the status URL until `status` is `completed` or `failed`.
3. GET the download URL. It streams the completed `.npy` file from RustFS in
   bounded chunks. The bearer-style URL is valid for seven days.

The MCP tools are `create_units_ts_dataset(project_id)` and
`get_units_ts_dataset_status(job_id)`. MCP returns API-relative status and
download URL paths. Both MCP tools require project download access.

The mapped-reaction TS geometry JSONL export is also available as the paginated
MCP tool `export_mapped_reaction_transition_state_geometries(project_id,
after_binding_id?, limit?)`. It returns at most 100 records per call and caps
each response at 512 KiB; continue with `next_after_binding_id` while
`has_more` is true. It requires the same project download permission. For full
streaming exports, use the REST JSONL endpoint below.

The API only enqueues work. The `units-ts-dataset-worker` Compose service claims
the durable job, builds the NPY file, and uploads it to RustFS. Apply the
`0059_units_ts_dataset_export` migration and run that service alongside the API
to enable exports. The NPY download itself is streamed from RustFS, but the NPY
dataset is fully assembled by the worker before that download link becomes
available. This is required by the current UniTS object-array export.

On startup and every five minutes, the worker also removes historical exports
from the obsolete flat-key layout
`dataset-exports/units-ts/<project UUID>/<job UUID>.npy`.
Their job rows are expired before object deletion, immediately disabling old
download links. Failed referenced objects remain attached to expired rows for
retry; detached legacy files are found again by later scans. Current exports
use a lease-specific nested key and are not selected by this cleanup.

## Direct streaming UniTS feature JSONL

MCP clients can use `export_units_ts_dataset_jsonl(project_id,
after_binding_id?, limit?)` to retrieve bounded pages of the same records. The
tool requires `artifact:download`, returns at most 100 records and 512 KiB per
call, and provides `next_after_binding_id`/`has_more` for continuation.

Use `GET /api/units-ts-datasets/export.jsonl?project_id=<UUID>` to stream the
same UniTS feature fields as JSON Lines. It requires `artifact:download` on the
project. Each line is one sample; NumPy arrays are represented as JSON arrays,
and `feature_schema` is `units-ts-feature-record-v2`. Atom, coordinate, graph,
and reactive-atom indices are reordered so index `n - 1` represents atom map
`n`; maps must form a contiguous `1..N` sequence. Atom and edge feature columns
keep the UniTS order described below. The endpoint reads verified
bindings in database pages and starts returning samples without assembling the
full dataset first. Samples with unusable TS structures or reaction centers
are skipped, as they are in the NPY exporter.

Choose this route when the consumer can process samples incrementally. Choose
the NPY job when the consumer specifically needs the NumPy object-array file.

## Mapped-reaction geometry and Mol JSONL

For mapped-reaction keyed TS geometry, use
`GET /api/mapped-reactions/transition-state-geometries/export.jsonl?project_id=<UUID>`.
The caller must have `artifact:download` permission for the project. The
endpoint pages over database rows and streams each serialized record as soon as
it is ready; it does not stage the complete dataset in memory or RustFS.

Each line is one JSON object with this shape:

```json
{
  "schema": "mapped-reaction-ts-geometry-v2",
  "key": "<mapped reaction SMILES>",
  "value": {
    "geometry": {
      "geometry_id": "<UUID>",
      "geometry_binding_id": "<UUID>",
      "geometry_hash": "<sha256>",
      "charge": 0,
      "multiplicity": 1,
      "coordinate_units": "angstrom",
      "atoms": [
        {
          "element": "C",
          "atomic_number": 6,
          "atom_map_number": 1,
          "coordinates_angstrom": [0.0, 0.0, 0.0]
        }
      ]
    },
    "rdkit_mol": {"format": "molblock", "value": "..."}
  }
}
```

Both `atoms` and the Mol block are ordered by atom-map number, so map `n` is
always at array/Mol atom index `n - 1`. The Mol block contains the same 3D
conformer; read it with `Chem.MolFromMolBlock(block, removeHs=False,
sanitize=False)` to preserve the stored graph and atom-map labels. Some
metal/aromatic geometries cannot be kekulized by RDKit, so the exporter retains
their aromatic-bond representation instead of failing the JSONL stream;
sanitization may still fail for those structures. A mapped reaction can have
multiple bound TS geometries, so its key may appear on multiple JSONL lines.
Only records with a verified mapping, conserved map-to-element identities, and
exactly one finite 3D conformer are emitted. Inconsistent legacy mappings are
skipped and logged instead of being exported under the wrong reaction atom.

Use JSONL when the consumer needs incremental processing. Keep NPY for the
upstream UniTS-compatible feature arrays; changing that existing contract to
JSONL would require adapting the UniTS consumer as well.

## NPY records and UniTS adapter

The downloaded file is a one-dimensional NumPy object array. Each element is
the six-field positional record consumed by UniTS `MultiDatasetV2`:

```python
(mol_atoms, mol_coords, x_edge_index_attr, rdmol, blk_idxs, reactive_atoms)
```

`x_edge_index_attr` contains `(node_attr, edge_index, edge_attr, atom_mass,
new_edge_index, new_edge_attr)`. The arrays in this download are NumPy arrays
so the export worker does not require PyTorch. Run the included adapter in the
UniTS Python environment to convert the graph arrays to the PyTorch tensors
expected by the upstream loader:

```sh
python scripts/adapt_units_ts_dataset.py downloaded.npy dataset_0_1.npy
```

Point `MultiDatasetV2` at the directory containing the adapted file. The
`rdmol` field is an RDKit molecule reordered into atom-map order, with the
source IDs stored as molecule properties; `blk_idxs` contains the correspondingly
reordered fragment atom indices. For every record, atom map `n` is at coordinate
and graph atom index `n - 1`.

Atom columns follow upstream UniTS order: atom type, degree capped at 10,
total reaction charge plus 3, hybridization, chiral tag, aromatic flag, total
valence, total hydrogens, R/S/None CIP code, and multiplicity minus 1. Edge
columns are bond type, bond direction, bond stereo, in-ring, and conjugated.
The element, bond, direction, stereo, hybridization, and chirality vocabularies
match [`units/data.py`](https://github.com/licheng-xu-echo/UniTS/blob/main/units/data.py).
The upstream MIT notice is preserved in [`licenses/UniTS-MIT.txt`](../licenses/UniTS-MIT.txt).

Reactive atom indices are inferred from mapped bond, atom, and stereochemistry
changes between the mapped reaction sides and use the exported atom-map order.
Mapped RXN SMILES are parsed with RDKit's `ReactionFromSmarts(useSmiles=True)`
so metal-coordination `->` and `<-` bonds remain part of their reaction
templates; the dative-bond donor direction is included when comparing bond
changes.
Samples without verified atom
mapping, valid 3D coordinates, supported UniTS charge/multiplicity, or an
inferable reaction center are skipped. Their aggregated reasons appear in
`skip_reasons`; an export with no usable samples ends as `failed`.
