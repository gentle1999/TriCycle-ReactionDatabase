"""Stable adapter from public MolOP frame fields to ingestion DTOs."""

import logging
from importlib import import_module
from importlib.metadata import version
from typing import Any, Literal

from molgr.config import CONFIG as MOLGR_CONFIG
from molop.config import molopconfig
from molop.io.base_models.ChemFileFrame import BaseCalcFrame
from rdkit import Chem

from tricycle_reaction_db.application.dtos.chemistry import (
    NormalizedMoleculeRecord,
)
from tricycle_reaction_db.core.units import ANGSTROM, magnitude_in
from tricycle_reaction_db.ingestion.normalization import (
    normalize_molecule,
    normalize_molgr_stereochemistry,
)

logger = logging.getLogger(__name__)
MOLOP_VERSION = version("molop")
MOLGR_VERSION = version("molgr")
MOLECULAR_GRAPH_RECONSTRUCTION_FAILURE_POLICY: Literal["return_suspicious"] = "return_suspicious"


def _install_molop_gaussian_ingestion_compatibility() -> None:
    """Keep usable source-order frames when optional MolOP fields are malformed.

    MolOP 0.2.20 can reject an otherwise readable Gaussian log while building
    file-level orientation metadata from mismatched input/output atom arrays,
    or while validating optional orbital-symmetry/force arrays. Those values
    are not needed to establish a frame's atom order. Per-frame coordinates and
    their source indices remain untouched.
    """

    file_parser = import_module("molop.io.logic.gaussian.log.parsers.G16LogFileParser")
    original_transform = getattr(
        file_parser,
        "extract_g16_standard_orientation_transformation_matrix",
        None,
    )
    if callable(original_transform) and not getattr(
        original_transform, "_tricycle_safe_shape", False
    ):

        def safe_transform(context: Any) -> Any:
            try:
                return original_transform(context)
            except ValueError as error:
                if not str(error).startswith("Shape mismatch: P="):
                    raise
                # This is only file-level metadata. Frame-level input and
                # standard coordinates are parsed independently and retain
                # their original atom order.
                logger.warning(
                    "Skipping MolOP file-level orientation transform with mismatched arrays: %s",
                    error,
                )
                return None

        safe_transform._tricycle_safe_shape = True  # type: ignore[attr-defined]
        file_parser.extract_g16_standard_orientation_transformation_matrix = safe_transform

    frame_parser = import_module("molop.io.logic.gaussian.log.frame_parsers.G16LogFileFrameParser")
    mixin = getattr(frame_parser, "G16LogFileFrameParserMixin", None)
    if mixin is None:
        return

    original_population = getattr(mixin, "_run_population_phase", None)
    if callable(original_population) and not getattr(
        original_population, "_tricycle_optional_shape", False
    ):

        def safe_population(self: Any, state: Any, result: Any) -> Any:
            phase = original_population(self, state, result)
            fields = getattr(result, "fields", None)
            orbitals = fields.get("molecular_orbitals") if isinstance(fields, dict) else None
            if not isinstance(orbitals, dict):
                return phase

            cleaned_orbitals = dict(orbitals)
            discarded: list[str] = []
            for spin in ("alpha", "beta"):
                symmetries_key = f"{spin}_symmetries"
                energies_key = f"{spin}_energies"
                symmetries = cleaned_orbitals.get(symmetries_key)
                energies = cleaned_orbitals.get(energies_key)
                if symmetries and energies is not None and len(symmetries) != len(energies):
                    cleaned_orbitals.pop(symmetries_key, None)
                    discarded.append(spin)

            if discarded:
                fields["molecular_orbitals"] = cleaned_orbitals
                logger.warning(
                    "Dropping MolOP orbital symmetry labels with mismatched energy lengths: %s",
                    ", ".join(discarded),
                )
            return phase

        safe_population._tricycle_optional_shape = True  # type: ignore[attr-defined]
        mixin._run_population_phase = safe_population

    original_forces = getattr(mixin, "_run_forces_phase", None)
    if callable(original_forces) and not getattr(
        original_forces, "_tricycle_optional_shape", False
    ):

        def safe_forces(self: Any, state: Any, result: Any) -> Any:
            phase = original_forces(self, state, result)
            fields = getattr(result, "fields", None)
            if not isinstance(fields, dict):
                return phase
            forces = fields.get("forces")
            atoms = fields.get("atoms")
            if forces is None or not isinstance(atoms, (list, tuple)):
                return phase
            magnitude = getattr(forces, "m", forces)
            shape = getattr(magnitude, "shape", None)
            if shape is None:
                return phase
            actual_shape = tuple(shape)
            expected_shape = (len(atoms), 3)
            if actual_shape != expected_shape:
                for key in (
                    "forces",
                    "forces_axis_order",
                    "forces_atom_order",
                    "forces_orientation",
                    "force_source_field",
                    "force_transformation",
                ):
                    fields.pop(key, None)
                logger.warning(
                    "Dropping MolOP force array with shape %s for %s source-order atoms",
                    actual_shape,
                    len(atoms),
                )
            return phase

        safe_forces._tricycle_optional_shape = True  # type: ignore[attr-defined]
        mixin._run_forces_phase = safe_forces


def configure_molecular_graph_reconstruction(*, allow_native_parallel: bool = False) -> None:
    """Keep calculation frames when MolGR can only return an untrusted topology.

    MolOP 0.2.9 reads the policy from its own process-global ``molopconfig`` and
    applies it to the shared MolGR config; set both so the ``suspicious_fallback``
    frames keep flowing into the TS inference gate.
    """

    # ProcessPoolExecutor pickles normalized records and endpoint MOLs. RDKit's
    # default pickle omits atom properties, including MolGR metal spin evidence.
    Chem.SetDefaultPickleProperties(Chem.PropertyPickleOptions.AllProps)
    _install_molop_gaussian_ingestion_compatibility()
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
    coordinates = magnitude_in(frame.coords, ANGSTROM)
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
