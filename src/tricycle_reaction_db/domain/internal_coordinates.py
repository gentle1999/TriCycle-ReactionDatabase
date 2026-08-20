"""MolOP-backed, E(3)-invariant internal-coordinate helpers."""

from hashlib import sha256
from typing import Final

import numpy as np
import numpy.typing as npt
from molop.io.base_models.DataClasses import AtomInInternalCoords, InternalCoords
from molop.unit import atom_ureg

INTERNAL_COORDINATE_DECIMAL_PLACES: Final = 12


def canonical_cartesian_coordinates(
    coordinates: object,
    *,
    atom_count: int,
) -> npt.NDArray[np.float64]:
    """Return immutable little-endian Cartesian coordinates in angstrom."""

    array = np.asarray(coordinates, dtype=np.float64)
    if array.shape != (atom_count, 3):
        raise ValueError(f"coordinates must have shape ({atom_count}, 3), got {array.shape}")
    if not np.isfinite(array).all():
        raise ValueError("coordinates must contain only finite values")
    canonical = np.array(array, dtype="<f8", order="C", copy=True)
    canonical[canonical == 0] = 0.0
    canonical.setflags(write=False)
    return canonical


def internal_coordinates_from_cartesian(
    symbols: list[str],
    coordinates: npt.NDArray[np.float64],
) -> npt.NDArray[np.float64]:
    """Convert ordered Cartesian coordinates to a stable ``[r, angle, dihedral]`` matrix."""

    internal = InternalCoords.from_cartesian_coords(
        symbols,
        coordinates * atom_ureg.angstrom,
    )
    values = np.asarray(
        [
            [
                float(atom.distance.to(atom_ureg.angstrom).magnitude),
                float(atom.angle.to(atom_ureg.degree).magnitude),
                float(atom.dihedral.to(atom_ureg.degree).magnitude),
            ]
            for atom in internal
        ],
        dtype=np.float64,
    )
    if values.shape != (len(symbols), 3) or not np.isfinite(values).all():
        raise ValueError("MolOP produced invalid internal coordinates")

    if len(values):
        values[0] = 0.0
    if len(values) > 1:
        values[1, 1:] = 0.0
    if len(values) > 2:
        values[2, 2] = 0.0
    if len(values) > 3:
        values[3:, 2] = (values[3:, 2] + 180.0) % 360.0 - 180.0
    values = np.round(values, decimals=INTERNAL_COORDINATE_DECIMAL_PLACES)
    values[values == 0] = 0.0
    values = np.array(values, dtype="<f8", order="C", copy=True)
    values.setflags(write=False)
    return values


def cartesian_from_internal_coordinates(
    symbols: list[str],
    internal_coordinates: npt.NDArray[np.float64],
) -> npt.NDArray[np.float64]:
    """Rebuild the canonical Cartesian representative using MolOP's Z-matrix converter."""

    values = np.asarray(internal_coordinates, dtype=np.float64)
    if values.shape != (len(symbols), 3):
        raise ValueError(
            f"internal_coordinates must have shape ({len(symbols)}, 3), got {values.shape}"
        )
    if not np.isfinite(values).all():
        raise ValueError("internal_coordinates must contain only finite values")

    atoms: list[AtomInInternalCoords] = []
    for index, (symbol, row) in enumerate(zip(symbols, values, strict=True)):
        dihedral = float(row[2])
        if index > 2:
            # MolOP's forward and reverse standard-dihedral conventions
            # differ by 180 degrees. Keep the persisted values in the forward
            # convention and adapt only the reverse conversion.
            dihedral -= 180.0
        atoms.append(
            AtomInInternalCoords(
                symbol=symbol,
                distance_to_index=max(index - 1, 0),
                distance=float(row[0]) * atom_ureg.angstrom,
                angle_to_index=max(index - 2, 0),
                angle=float(row[1]) * atom_ureg.degree,
                dihedral_to_index=max(index - 3, 0),
                dihedral=dihedral * atom_ureg.degree,
            )
        )
    coordinates = InternalCoords.from_atoms(atoms).to_cartesian_coords()
    return canonical_cartesian_coordinates(
        coordinates.to(atom_ureg.angstrom).magnitude,
        atom_count=len(symbols),
    )


def internal_coordinate_hash(internal_coordinates: npt.NDArray[np.float64]) -> str:
    """Hash the canonical little-endian internal-coordinate matrix."""

    values = np.asarray(internal_coordinates)
    if values.dtype != np.dtype("<f8") or not values.flags.c_contiguous:
        raise ValueError("internal coordinates must be C-contiguous little-endian float64")
    return sha256(values.tobytes(order="C")).hexdigest()


def proper_rigid_alignment(
    observed: npt.NDArray[np.float64],
    reference: npt.NDArray[np.float64],
) -> tuple[float, float, tuple[float, ...]]:
    """Align ordered coordinates with one proper rotation and translation."""

    if observed.shape != reference.shape or observed.ndim != 2 or observed.shape[1] != 3:
        raise ValueError("coordinate alignment requires matching (atom_count, 3) arrays")
    observed_center = np.mean(observed, axis=0)
    reference_center = np.mean(reference, axis=0)
    observed_centered = observed - observed_center
    reference_centered = reference - reference_center
    left, _, right_transpose = np.linalg.svd(observed_centered.T @ reference_centered)
    correction = np.ones(3, dtype=np.float64)
    if np.linalg.det(left @ right_transpose) < 0:
        correction[-1] = -1.0
    rotation = (left * correction) @ right_transpose
    translation = reference_center - observed_center @ rotation
    aligned = observed @ rotation + translation
    delta = aligned - reference
    transform = np.eye(4, dtype=np.float64)
    transform[:3, :3] = rotation.T
    transform[:3, 3] = translation
    return (
        float(np.sqrt(np.mean(np.square(delta)))),
        float(np.max(np.abs(delta))),
        tuple(float(value) for value in transform.ravel(order="C")),
    )


__all__ = [
    "INTERNAL_COORDINATE_DECIMAL_PLACES",
    "canonical_cartesian_coordinates",
    "cartesian_from_internal_coordinates",
    "internal_coordinate_hash",
    "internal_coordinates_from_cartesian",
    "proper_rigid_alignment",
]
