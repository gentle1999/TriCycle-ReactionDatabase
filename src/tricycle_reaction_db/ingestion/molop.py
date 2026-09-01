"""Stable adapter from public MolOP frame fields to ingestion DTOs."""

from importlib.metadata import version
from typing import Any, Literal

from molgr.config import CONFIG as MOLGR_CONFIG
from molop.config import molopconfig
from molop.io.base_models.ChemFileFrame import BaseCalcFrame
from molop.unit import atom_ureg
from rdkit import Chem

from tricycle_reaction_db.application.dtos.chemistry import NormalizedMoleculeRecord
from tricycle_reaction_db.ingestion.normalization import (
    normalize_molecule,
    normalize_molgr_stereochemistry,
)

MOLOP_VERSION = version("molop")
MOLGR_VERSION = version("molgr")
MOLECULAR_GRAPH_RECONSTRUCTION_FAILURE_POLICY: Literal["return_suspicious"] = "return_suspicious"


def configure_molecular_graph_reconstruction(*, allow_native_parallel: bool = False) -> None:
    """Keep calculation frames when MolGR can only return an untrusted topology.

    MolOP 0.2.9 reads the policy from its own process-global ``molopconfig`` and
    applies it to the shared MolGR config; set both so the ``suspicious_fallback``
    frames keep flowing into the TS inference gate.
    """

    molopconfig.reconstruction_failure_policy = MOLECULAR_GRAPH_RECONSTRUCTION_FAILURE_POLICY
    molopconfig.apply_molgr_reconstruction_policy()
    cpp_backend = MOLGR_CONFIG.cpp_backend
    if allow_native_parallel:
        # Parent-process prewarming is the outermost MolGR boundary. Let its
        # native batch scheduler flatten all frame reconstructions across cores.
        cpp_backend.max_threads = None
        cpp_backend.enable_target_bucket_parallelism = True
        cpp_backend.target_bucket_parallel_max_threads = None
    else:
        # MolGR may enter native OpenMP/thread pools from each MolOP worker. The
        # ingestion process already owns the outer process pool, so nested native
        # parallelism causes oversubscription and can crash with BrokenProcessPool.
        cpp_backend.max_threads = 1
        cpp_backend.enable_target_bucket_parallelism = False
        cpp_backend.target_bucket_parallel_max_threads = 1
    cpp_backend.enable_candidate_scoring_parallelism = False
    MOLGR_CONFIG.interface.reconstruction_failure_policy = (
        MOLECULAR_GRAPH_RECONSTRUCTION_FAILURE_POLICY
    )


def normalize_molop_frame(frame: BaseCalcFrame[Any]) -> NormalizedMoleculeRecord:
    """Normalize one public MolOP frame without accessing parser-private state."""

    rdmol = frame.rdmol
    if not isinstance(rdmol, Chem.Mol):
        raise ValueError("MolOP frame does not provide a reconstructed RDKit molecule")
    # MolGR returns the reconstructed graph and its Cartesian conformer, but
    # RDKit's SMILES writer may still need neighboring BondDir metadata. Keep
    # this as the single MolGR -> ingestion stereo boundary.
    mol = normalize_molgr_stereochemistry(rdmol)
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
