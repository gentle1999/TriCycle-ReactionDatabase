"""Stable adapter from public MolOP frame fields to ingestion DTOs."""

from importlib.metadata import version
from typing import Any, Literal

from molgr.config import CONFIG as MOLGR_CONFIG
from molop.io.base_models.ChemFileFrame import BaseCalcFrame
from molop.unit import atom_ureg
from rdkit import Chem

from tricycle_reaction_db.application.dtos.chemistry import NormalizedMoleculeRecord
from tricycle_reaction_db.ingestion.normalization import normalize_molecule

MOLOP_VERSION = version("molop")
MOLGR_VERSION = version("molgr")
MOLECULAR_GRAPH_RECONSTRUCTION_FAILURE_POLICY: Literal["return_suspicious"] = "return_suspicious"


def configure_molecular_graph_reconstruction() -> None:
    """Keep calculation frames when MolGR can only return an untrusted topology."""

    MOLGR_CONFIG.interface.reconstruction_failure_policy = (
        MOLECULAR_GRAPH_RECONSTRUCTION_FAILURE_POLICY
    )


def normalize_molop_frame(frame: BaseCalcFrame[Any]) -> NormalizedMoleculeRecord:
    """Normalize one public MolOP frame without accessing parser-private state."""

    mol = frame.rdmol
    if not isinstance(mol, Chem.Mol):
        raise ValueError("MolOP frame does not provide a reconstructed RDKit molecule")
    coordinates = frame.coords.to(atom_ureg.angstrom).magnitude
    reconstruction_backend = frame.topology_reconstruction_backend or "unknown"
    reconstruction_status = getattr(
        frame.topology_reconstruction_status,
        "value",
        frame.topology_reconstruction_status,
    )
    return normalize_molecule(
        mol,
        coordinates,
        charge=frame.charge,
        multiplicity=frame.multiplicity,
        reconstruction_method=f"molgr/{reconstruction_backend}",
        reconstruction_version=MOLGR_VERSION,
        reconstruction_metadata={
            "molop_version": MOLOP_VERSION,
            "molgr_backend": reconstruction_backend,
            "molgr_config_sha256": frame.topology_reconstruction_config_sha256,
            "molgr_status": reconstruction_status,
            "make_dative_bonds": frame.topology_make_dative_bonds,
            "qm_software": frame.qm_software,
            "qm_software_version": frame.qm_software_version,
        },
    )


__all__ = [
    "configure_molecular_graph_reconstruction",
    "MOLECULAR_GRAPH_RECONSTRUCTION_FAILURE_POLICY",
    "normalize_molop_frame",
]
