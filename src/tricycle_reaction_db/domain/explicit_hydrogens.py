"""The persisted molecular graph contains every hydrogen as an atom vertex."""

from rdkit import Chem


def require_explicit_hydrogens(molecule: Chem.Mol) -> None:
    """Reject implicit and bracket-count Hs without altering the source graph.

    Adding H coordinates to a QM result would fabricate scientific evidence.
    Topology-only callers may explicitly expand Hs before reaching this boundary.
    This cannot detect Hs deleted together with all evidence of their existence;
    ingestion must also compare the graph to the original frame's atom inventory.
    """
    candidate = Chem.Mol(molecule)
    candidate.UpdatePropertyCache(strict=False)
    if any(atom.GetNumImplicitHs() or atom.GetNumExplicitHs() for atom in candidate.GetAtoms()):  # type: ignore[no-untyped-call]
        raise ValueError("QM graph must contain every hydrogen explicitly (as an atom vertex)")
