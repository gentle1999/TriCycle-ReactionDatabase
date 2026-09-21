"""Opt-in, explicitly unverified TS endpoint reconstruction from displaced XYZ."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
from molop.structure.ts_analysis import most_frequent_topology, sample_vibration_amplitudes
from openbabel import openbabel as ob
from rdkit import Chem

ENDPOINT_PROVENANCE_PROP = "_tricycle_ts_endpoint_provenance"


def endpoint_provenance(endpoint: Chem.Mol) -> dict[str, Any]:
    if endpoint.HasProp(ENDPOINT_PROVENANCE_PROP):
        return dict(json.loads(endpoint.GetProp(ENDPOINT_PROVENANCE_PROP)))
    return {
        "method": "molop.possible_pre_post_ts",
        "validation_status": "strict",
        "strict_validation_passed": True,
    }


def strict_side_endpoint(frame: Any, signed_direction: int) -> Chem.Mol:
    """Use MolOP's native sampling, crowding checks, graph reconstruction and vote.

    The public paired convenience method raises before returning either side
    when one side is empty. Its public analysis primitives let us retain the
    successful side. MolOP's one-step vibration runner negates its ratio.
    """
    candidates = []
    for amplitude in sample_vibration_amplitudes(0.6, 1.4, 8, "harmonic_potential"):
        for molecule in frame.ts_vibration(ratio=-signed_direction * float(amplitude), steps=1):
            graph = molecule.rdmol
            if (
                graph is not None
                and getattr(molecule, "topology_reconstruction_status", None)
                != "suspicious_fallback"
            ):
                candidates.append(graph)
                break
    return most_frequent_topology(
        candidates, side="positive" if signed_direction < 0 else "negative"
    )


def openbabel_endpoint(frame: Any, coordinates: np.ndarray, *, strict_error: Exception) -> Chem.Mol:
    """Perceive a compatibility graph, never claim MolOP/MolGR validation."""
    molecule = ob.OBMol()
    molecule.BeginModify()
    for number, xyz in zip(frame.atoms, coordinates, strict=True):
        atom = molecule.NewAtom()
        atom.SetAtomicNum(int(number))
        atom.SetVector(*(float(value) for value in xyz))
    molecule.EndModify()
    molecule.SetTotalCharge(int(frame.charge))
    molecule.SetTotalSpinMultiplicity(int(frame.multiplicity))
    molecule.ConnectTheDots()
    molecule.PerceiveBondOrders()
    writer = ob.OBConversion()
    if not writer.SetOutFormat("mol"):
        raise ValueError("Open Babel MOL writer is unavailable")
    endpoint = Chem.MolFromMolBlock(writer.WriteString(molecule), sanitize=False, removeHs=False)
    if endpoint is None or [a.GetAtomicNum() for a in endpoint.GetAtoms()] != list(frame.atoms):
        raise ValueError("Open Babel fallback did not preserve source atoms and order")
    # MOL text rounds Cartesian coordinates. Restore the exact mode-ratio=1
    # positions and forbid implicit atoms not present in the source file.
    endpoint.RemoveAllConformers()
    conformer = Chem.Conformer(len(frame.atoms))
    conformer.Set3D(True)
    for index, xyz in enumerate(coordinates):
        conformer.SetAtomPosition(index, tuple(float(value) for value in xyz))
        endpoint.GetAtomWithIdx(index).SetNoImplicit(True)
    endpoint.AddConformer(conformer)
    endpoint.UpdatePropertyCache(strict=False)
    graph_charge = sum(atom.GetFormalCharge() for atom in endpoint.GetAtoms())
    endpoint.SetProp(
        ENDPOINT_PROVENANCE_PROP,
        json.dumps(
            {
                "method": "openbabel/xyz-connect-the-dots-perceive-bond-orders",
                "openbabel_version": ob.OBReleaseVersion(),
                "validation_status": "unverified",
                "strict_validation_passed": False,
                "mode_ratio": 1.0,
                "fallback_reason": str(strict_error) or type(strict_error).__name__,
                "source_charge": int(frame.charge),
                "source_multiplicity": int(frame.multiplicity),
                "graph_formal_charge": graph_charge,
                "formal_charge_matches_source": graph_charge == int(frame.charge),
                "electronic_state_validated": False,
            }
        ),
    )
    return endpoint
