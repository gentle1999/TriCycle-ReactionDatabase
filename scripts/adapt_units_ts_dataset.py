"""Convert exported UniTS NumPy arrays to tensors expected by MultiDatasetV2."""

from __future__ import annotations

import argparse
from importlib import import_module
from pathlib import Path
from typing import Any

import numpy as np


def adapt_dataset(source: Path, destination: Path) -> int:
    """Write an adapter-ready NPY object array; run inside the UniTS environment."""

    try:
        torch: Any = import_module("torch")
    except ImportError as error:
        raise RuntimeError(
            "PyTorch is required; run this adapter in the UniTS environment"
        ) from error

    records = np.load(source, allow_pickle=True)
    if records.ndim != 1:
        raise ValueError("expected a one-dimensional UniTS export object array")

    adapted = np.empty(len(records), dtype=object)
    for index, record in enumerate(records):
        if len(record) != 6:
            raise ValueError(f"record {index} does not have the six UniTS fields")
        mol_atoms, mol_coords, graph_features, rdmol, blk_idxs, reactive_atoms = record
        if len(graph_features) != 6:
            raise ValueError(f"record {index} does not have the six graph feature fields")
        node_attr, edge_index, edge_attr, atom_mass, new_edge_index, new_edge_attr = graph_features
        tensor_graph_features = (
            torch.as_tensor(node_attr, dtype=torch.long),
            torch.as_tensor(edge_index, dtype=torch.long),
            torch.as_tensor(edge_attr, dtype=torch.long),
            torch.as_tensor(atom_mass),
            torch.as_tensor(new_edge_index, dtype=torch.long),
            torch.as_tensor(new_edge_attr, dtype=torch.long),
        )
        adapted[index] = (
            mol_atoms,
            mol_coords,
            tensor_graph_features,
            rdmol,
            blk_idxs,
            reactive_atoms,
        )

    with destination.open("wb") as output:
        np.save(output, adapted, allow_pickle=True)
    return len(adapted)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Adapt a UniTS TS export for direct MultiDatasetV2 loading."
    )
    parser.add_argument("source", type=Path, help="downloaded adapter-format NPY file")
    parser.add_argument("destination", type=Path, help="UniTS-ready NPY output file")
    arguments = parser.parse_args()
    count = adapt_dataset(arguments.source, arguments.destination)
    print(f"Adapted {count} UniTS records to {arguments.destination}")


if __name__ == "__main__":
    main()
