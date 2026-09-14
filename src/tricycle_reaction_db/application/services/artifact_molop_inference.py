"""MolOP/MolGR transition-state endpoint inference.

The upload service still owns when inference is invoked and how its result is
persisted.  This module owns only the chemistry-specific transformation from
one reconstructed MolOP frame to signed pre/post endpoints and evidence.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
from molop.io.base_models.ChemFileFrame import BaseCalcFrame
from rdkit import Chem

from tricycle_reaction_db.core.units import ANGSTROM, CM_INVERSE, magnitude_in
from tricycle_reaction_db.ingestion import (
    ensure_serializable_double_bond_stereochemistry,
    infer_molgr_stereochemistry_from_3d,
)

from .artifact_upload_types import _FailedInference, _Inference, _SuccessfulInference

EndpointStereoInference = Callable[[Chem.Mol], Chem.Mol]
SignedEndpointInference = Callable[
    [BaseCalcFrame[Any], int],
    tuple[Chem.Mol, Chem.Mol, float, float],
]
MappedReactionSmiles = Callable[[Chem.Mol, Chem.Mol], str]


def mapped_reaction_smiles(reactant: Chem.Mol, product: Chem.Mol) -> str:
    reactant_atoms = [
        atom.GetAtomicNum()
        for atom in reactant.GetAtoms()  # type: ignore[no-untyped-call]
    ]
    product_atoms = [
        atom.GetAtomicNum()
        for atom in product.GetAtoms()  # type: ignore[no-untyped-call]
    ]
    if reactant_atoms != product_atoms:
        raise ValueError("MolOP TS endpoints do not preserve source atom order")

    sides: list[str] = []
    for endpoint in (reactant, product):
        mapped = Chem.Mol(endpoint)
        # The endpoint has already been assigned from its 3D conformer by
        # ``infer_endpoint_stereochemistry_from_3d``. This function is only a
        # one-way projection of that frozen graph; it must never infer stereo
        # again or let a SMILES traversal become a new source of truth.
        for prop_name in ("_smilesAtomOutputOrder", "_smilesBondOutputOrder"):
            if mapped.HasProp(prop_name):
                mapped.ClearProp(prop_name)
        for atom_index, atom in enumerate(mapped.GetAtoms()):  # type: ignore[no-untyped-call]
            atom.SetAtomMapNum(atom_index + 1)
        # MolGR owns the endpoint graph.  Sanitizing fragments here can erase
        # radical/electronic annotations before topology persistence. Keep the
        # endpoint's one trusted conformer through fragment extraction: RDKit
        # may retain BondStereo/BondDir values whose local atom order is no
        # longer sufficient to reconstruct the physical E/Z state after a
        # disconnected fragment is isolated.
        fragments = Chem.GetMolFrags(mapped, asMols=True, sanitizeFrags=False)
        serialized_fragments: list[Chem.Mol] = []
        for fragment in fragments:
            # Split while the endpoint conformer is still available, then
            # discard it only after the projection has completed. A failure is
            # propagated as an explicit inference failure; emitting a
            # non-isomeric reaction would silently lose trusted 3D stereo.
            repaired = ensure_serializable_double_bond_stereochemistry(
                fragment,
                preserve_atom_maps=True,
            )
            # A terminal alkene can carry direction-only flags from MolOP's
            # source traversal even though RDKit correctly reports no E/Z
            # assignment. Those flags are not stereochemical evidence and
            # would make the SMILES writer emit arbitrary slash markers.
            if not any(
                bond.GetStereo() in {Chem.BondStereo.STEREOE, Chem.BondStereo.STEREOZ}
                for bond in repaired.GetBonds()  # type: ignore[no-untyped-call]
            ):
                for bond in repaired.GetBonds():  # type: ignore[no-untyped-call]
                    bond.SetBondDir(Chem.BondDir.NONE)
            repaired.RemoveAllConformers()
            serialized_fragments.append(repaired)
        sides.append(
            ".".join(
                sorted(
                    Chem.MolToSmiles(
                        fragment,
                        canonical=True,
                        isomericSmiles=True,
                        allHsExplicit=True,
                    )
                    for fragment in serialized_fragments
                )
            )
        )
    # Fragment order is not stable across MolOP endpoint reconstruction. Each
    # fragment is canonicalized and sorted above, so the mapped reaction is
    # stable without round-tripping a metal-rich graph through RDKit reaction
    # templates before persistence validates it.
    return f"{sides[0]}>>{sides[1]}"


def infer_endpoint_stereochemistry_from_3d(endpoint: Chem.Mol) -> Chem.Mol:
    """Infer an endpoint's stereochemistry from its displaced 3D geometry."""

    return infer_molgr_stereochemistry_from_3d(endpoint)


def signed_ts_endpoints(
    frame: BaseCalcFrame[Any],
    vibration_position: int,
    *,
    infer_endpoint_stereochemistry: EndpointStereoInference = (
        infer_endpoint_stereochemistry_from_3d
    ),
) -> tuple[Chem.Mol, Chem.Mol, float, float]:
    """Return MolOP's inferred pre/post-TS endpoints with signed displacements."""

    reactant, product = frame.possible_pre_post_ts(show_3D=True)
    if frame.vibrations is None:
        raise ValueError("TS frame has no vibration mode")
    center = np.asarray(magnitude_in(frame.coords, ANGSTROM), dtype=np.float64)
    mode = np.asarray(
        magnitude_in(
            frame.vibrations[vibration_position].vibration_mode,
            ANGSTROM,
        ),
        dtype=np.float64,
    )
    mode_norm = float(np.sum(np.square(mode)))
    if mode.shape != center.shape or mode_norm <= 0:
        raise ValueError("TS imaginary mode does not match the source coordinates")

    def _signed_ratio(endpoint: Chem.Mol) -> float:
        if endpoint.GetNumConformers() != 1 or not endpoint.GetConformer().Is3D():
            raise ValueError("MolOP TS endpoint lost its 3D conformer")
        coordinates = np.asarray(
            endpoint.GetConformer().GetPositions(),
            dtype=np.float64,
        )
        if coordinates.shape != center.shape or not np.isfinite(coordinates).all():
            raise ValueError("MolOP TS endpoint coordinates are invalid")
        return float(np.sum((center - coordinates) * mode) / mode_norm)

    negative_ratio = _signed_ratio(reactant)
    positive_ratio = _signed_ratio(product)
    if negative_ratio > positive_ratio:
        negative_ratio, positive_ratio = positive_ratio, negative_ratio
        reactant, product = product, reactant
    if negative_ratio >= 0 or positive_ratio <= 0:
        raise ValueError(
            "MolOP pre/post-TS endpoints do not bracket the TS center on the imaginary mode"
        )
    reactant = infer_endpoint_stereochemistry(reactant)
    product = infer_endpoint_stereochemistry(product)
    return reactant, product, abs(negative_ratio), positive_ratio


def infer_ts_frame(
    frame: BaseCalcFrame[Any],
    fallback_index: int,
    *,
    signed_endpoints: SignedEndpointInference = signed_ts_endpoints,
    mapped_smiles: MappedReactionSmiles = mapped_reaction_smiles,
) -> _Inference | None:
    """Validate and infer one TS frame after its topology was reconstructed."""

    if frame.is_TS is not True:
        return None
    file_frame_index = frame.file_frame_index
    if file_frame_index is None:
        file_frame_index = fallback_index
    vibrations = frame.vibrations
    if vibrations is None or len(vibrations.imaginary_idxs) != 1:
        return None
    imaginary_position = vibrations.imaginary_idxs[0]
    imaginary_mode_index = (
        vibrations.mode_indices[imaginary_position]
        if vibrations.mode_indices
        else imaginary_position
    )
    frequency = vibrations[imaginary_position].frequency
    if frequency is None:
        return None
    frequency_cm1 = float(magnitude_in(frequency, CM_INVERSE))
    try:
        (
            negative_endpoint,
            positive_endpoint,
            negative_displacement_ratio,
            positive_displacement_ratio,
        ) = signed_endpoints(frame, imaginary_position)
        if any(
            endpoint.HasProp("_MolGRReconstructionStatus")
            and endpoint.GetProp("_MolGRReconstructionStatus") == "suspicious_fallback"
            for endpoint in (negative_endpoint, positive_endpoint)
        ):
            return _FailedInference(
                file_frame_index=file_frame_index,
                imaginary_mode_index=imaginary_mode_index,
                imaginary_frequency_cm1=frequency_cm1,
                error_code="ts_topology_untrusted",
                error_message=("MolOP returned a suspicious fallback topology for a TS endpoint"),
            )
        reactant, product = sorted(
            (negative_endpoint, positive_endpoint),
            key=lambda endpoint: len(Chem.GetMolFrags(endpoint)),
            reverse=True,
        )
        for endpoint in (negative_endpoint, positive_endpoint, reactant, product):
            endpoint_atoms = [
                atom.GetAtomicNum()
                for atom in endpoint.GetAtoms()  # type: ignore[no-untyped-call]
            ]
            if endpoint_atoms != frame.atoms:
                raise ValueError("MolOP TS endpoint atom order differs from the TS source frame")
        return _SuccessfulInference(
            file_frame_index=file_frame_index,
            imaginary_mode_index=imaginary_mode_index,
            imaginary_frequency_cm1=frequency_cm1,
            reaction_smiles=mapped_smiles(reactant, product),
            negative_endpoint=negative_endpoint,
            positive_endpoint=positive_endpoint,
            negative_displacement_ratio=negative_displacement_ratio,
            positive_displacement_ratio=positive_displacement_ratio,
            charge=int(frame.charge),
            multiplicity=int(frame.multiplicity),
        )
    except Exception as error:
        error_code = getattr(error, "error_code", "ts_endpoint_inference_failed")
        error_metadata = None
        evidence = getattr(error, "evidence", None)
        if callable(evidence):
            candidate = evidence()
            if isinstance(candidate, dict):
                error_metadata = candidate
        return _FailedInference(
            file_frame_index=file_frame_index,
            imaginary_mode_index=imaginary_mode_index,
            imaginary_frequency_cm1=frequency_cm1,
            error_code=error_code,
            error_message=str(error) or type(error).__name__,
            error_metadata=error_metadata,
        )


__all__ = [
    "infer_endpoint_stereochemistry_from_3d",
    "infer_ts_frame",
    "mapped_reaction_smiles",
    "signed_ts_endpoints",
]
