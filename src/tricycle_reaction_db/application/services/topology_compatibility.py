"""Compatibility predicates for source topologies used by derived data."""

from __future__ import annotations

import logging

from rdkit import Chem

from tricycle_reaction_db.application.services.rdkit_graph_matching import (
    MolecularGraphMatchTimeoutError,
    get_substruct_matches,
)

logger = logging.getLogger(__name__)


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
        atom.SetChiralTag(Chem.ChiralType.CHI_UNSPECIFIED)
        atom.SetIsAromatic(False)
        for property_name in ("_CIPCode", "_CIPRank"):
            if atom.HasProp(property_name):
                atom.ClearProp(property_name)
    for bond in normalized.GetBonds():  # type: ignore[no-untyped-call]
        bond.SetBondType(Chem.BondType.SINGLE)
        bond.SetIsAromatic(False)
        bond.SetStereo(Chem.BondStereo.STEREONONE)
        bond.SetBondDir(Chem.BondDir.NONE)
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
    geometry is therefore treated as a full subgraph of the endpoint after a
    charge/bond-order/stereo-free projection, with at most one endpoint-only
    bond.  Element identity, explicit hydrogens, isotope identity, atom count,
    and all source connectivity remain constrained.

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
    try:
        return bool(
            get_substruct_matches(
                endpoint_graph,
                source_graph,
                use_chirality=False,
                max_matches=1,
                hard_timeout_for_large_molecules=True,
            )
        )
    except MolecularGraphMatchTimeoutError:
        # Compatibility is a conservative source-selection predicate. A
        # timed-out candidate is not eligible, while the surrounding upload or
        # profile refresh can continue with the remaining DAG candidates.
        logger.warning("Skipping source geometry compatibility after an RDKit graph-match timeout")
        return False


__all__ = ["source_geometry_compatible_topology"]
