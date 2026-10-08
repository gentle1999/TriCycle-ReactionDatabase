"""The persisted molecular graph contains every hydrogen as an atom vertex."""

from functools import lru_cache
from typing import Any, cast

from rdkit import Chem


def require_explicit_hydrogens(molecule: Chem.Mol) -> None:
    """Reject implicit and bracket-count Hs without altering the source graph.

    Adding H coordinates to a QM result would fabricate scientific evidence.
    Topology-only callers may explicitly expand Hs before reaching this boundary.
    This cannot detect Hs deleted together with all evidence of their existence;
    ingestion must also compare the graph to the original frame's atom inventory.
    """
    # Coordinates do not affect hydrogen inventory. Keep the entire atom/bond
    # graph (including explicit counts and no-implicit flags) in the key, so
    # mutating a validated molecule must undergo validation again. Never cache
    # by object identity or SMILES, and never retain the caller's mutable MOL.
    _require_explicit_hydrogens_from_graph(
        molecule.ToBinary(Chem.PropertyPickleOptions.NoConformers)
    )


@lru_cache(maxsize=1024)
def _require_explicit_hydrogens_from_graph(graph: bytes) -> None:
    """Cache only successful validation of bounded, immutable graph evidence."""

    # RDKit accepts binary pickles at runtime; its constructor stub only
    # declares str for the pickle overload.
    candidate = Chem.Mol(cast(Any, graph))
    candidate.UpdatePropertyCache(strict=False)
    # RDKit's Python atom iterator repeatedly checks the molecule size. This
    # boundary is called for every persisted graph; direct indexing retains
    # the same hydrogen checks without that iterator overhead.
    for atom_index in range(candidate.GetNumAtoms()):
        atom = candidate.GetAtomWithIdx(atom_index)
        if atom.GetNumImplicitHs() or atom.GetNumExplicitHs():
            raise ValueError("QM graph must contain every hydrogen explicitly (as an atom vertex)")
