"""Compatibility predicates for source topologies used by derived data."""

from __future__ import annotations

from itertools import combinations

from rdkit import Chem

from tricycle_reaction_db.application.services.canonical_atom_mapping import (
    canonical_atom_index_mapping,
)


def _electronic_graph_projection(molecule: Chem.Mol) -> Chem.Mol:
    """Return a graph with charge, bond order, and stereo annotations removed.

    This projection is intentionally narrower than a general molecular
    equivalence relation. It is used only to recognize a source geometry whose
    parser assigned a different electronic form from a TS endpoint. The caller
    must still enforce project, formula, atom-count, charge, and
    topology-uniqueness constraints before accepting the result.
    """

    normalized = Chem.RWMol(Chem.Mol(molecule))
    for atom in normalized.GetAtoms():  # type: ignore[no-untyped-call]
        atom.SetAtomMapNum(0)
        atom.SetFormalCharge(0)
        atom.SetNumRadicalElectrons(0)
        atom.SetNumExplicitHs(0)
        atom.SetNoImplicit(True)
        atom.SetChiralTag(Chem.ChiralType.CHI_UNSPECIFIED)
        atom.SetIsAromatic(False)
        for property_name in ("_CIPCode", "_CIPRank"):
            if atom.HasProp(property_name):
                atom.ClearProp(property_name)
    for bond in normalized.GetBonds():  # type: ignore[no-untyped-call]
        bond.SetBondType(Chem.BondType.SINGLE)
        bond.SetIsAromatic(False)
        bond.SetIsConjugated(False)
        bond.SetStereo(Chem.BondStereo.STEREONONE)
        bond.SetBondDir(Chem.BondDir.NONE)
    normalized.RemoveAllConformers()
    normalized.UpdatePropertyCache(strict=False)
    return normalized.GetMol()


def source_geometry_compatible_topology(
    endpoint: Chem.Mol,
    source_geometry: Chem.Mol,
    *,
    max_endpoint_extra_bonds: int = 1,
) -> bool:
    """Return whether a source geometry can explain an endpoint topology.

    MolOP endpoint reconstruction can retain an endpoint-only bond in
    addition to changing bond orders and formal-charge placement.  The source
    comparison therefore removes the allowed endpoint-only bonds and compares
    complete canonical charge/bond-order/stereo-free graphs. No subgraph
    search or partial-graph atom assignment is used. Element identity, explicit
    hydrogens, isotope identity, atom count, and source connectivity are constrained.

    Callers must additionally enforce project, formula, charge, and candidate
    uniqueness constraints; this predicate alone is not permission to bind
    arbitrary molecules together.
    """

    if max_endpoint_extra_bonds < 0:
        raise ValueError("max_endpoint_extra_bonds must be non-negative")
    if endpoint.GetNumAtoms() != source_geometry.GetNumAtoms():
        return False
    endpoint_graph = _electronic_graph_projection(endpoint)
    source_graph = _electronic_graph_projection(source_geometry)
    extra_bonds = endpoint_graph.GetNumBonds() - source_graph.GetNumBonds()
    if extra_bonds < 0 or extra_bonds > max_endpoint_extra_bonds:
        return False
    endpoint_edges = [(b.GetBeginAtomIdx(), b.GetEndAtomIdx()) for b in endpoint_graph.GetBonds()]
    for removed_edges in combinations(endpoint_edges, extra_bonds):
        candidate = Chem.RWMol(endpoint_graph)
        for begin, end in removed_edges:
            candidate.RemoveBond(begin, end)
        candidate.UpdatePropertyCache(strict=False)
        # Stereo is already removed; keep isomeric serialization enabled so
        # isotope identity remains part of the canonical graph comparison.
        if canonical_atom_index_mapping(source_graph, candidate.GetMol()) is not None:
            return True
    return False


__all__ = ["source_geometry_compatible_topology"]
