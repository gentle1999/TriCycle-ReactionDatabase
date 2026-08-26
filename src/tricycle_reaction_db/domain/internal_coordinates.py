"""MolOP-backed, E(3)-invariant internal-coordinate helpers."""

from hashlib import sha256
from typing import Final

import numpy as np
import numpy.typing as npt

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

    expected_atom_count = len(symbols)
    xyz = np.asarray(coordinates, dtype=np.float64)
    atom_count = xyz.shape[0]
    values = np.zeros((atom_count, 3), dtype=np.float64)
    if atom_count > 1:
        values[1:, 0] = np.linalg.norm(xyz[1:] - xyz[:-1], axis=1)
    if atom_count > 2:
        first = xyz[:-2] - xyz[1:-1]
        second = xyz[2:] - xyz[1:-1]
        denominator = np.linalg.norm(first, axis=1) * np.linalg.norm(second, axis=1)
        cosine = np.divide(
            np.sum(first * second, axis=1),
            denominator,
            out=np.ones_like(denominator),
            where=denominator != 0,
        )
        values[2:, 1] = np.degrees(np.arccos(np.clip(cosine, -1.0, 1.0)))
    for index in range(3, atom_count):
        p0, p1, p2, p3 = xyz[index - 3 : index + 1]
        b0 = p1 - p0
        b1 = p2 - p1
        b2 = p3 - p2
        b1_norm = np.linalg.norm(b1)
        if b1_norm == 0:
            continue
        b1_unit = b1 / b1_norm
        v = b0 - np.dot(b0, b1_unit) * b1_unit
        w = b2 - np.dot(b2, b1_unit) * b1_unit
        values[index, 2] = np.degrees(np.arctan2(np.dot(np.cross(b1_unit, v), w), np.dot(v, w)))
    if values.shape != (expected_atom_count, 3) or not np.isfinite(values).all():
        raise ValueError("MolOP produced invalid internal coordinates")

    if len(values):
        values[0] = 0.0
    if len(values) > 1:
        values[1, 1:] = 0.0
    if len(values) > 2:
        values[2, 2] = 0.0
    if len(values) > 3:
        values[3:, 2] = (values[3:, 2] + 180.0) % 360.0 - 180.0
    values[...] = np.round(values, decimals=INTERNAL_COORDINATE_DECIMAL_PLACES)
    values[values == 0] = 0.0
    values = np.array(values, dtype="<f8", order="C", copy=True)  # type: ignore[assignment]
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

    del symbols  # All rows use the standard (non-alternate) Z-matrix form.
    atom_count = len(values)
    coordinates = np.zeros((atom_count, 3), dtype=np.float64)
    if atom_count > 1:
        coordinates[1, 0] = values[1, 0]
    if atom_count > 2:
        r = values[2, 0]
        theta = np.radians(values[2, 1])
        e1 = np.array([1.0, 0.0, 0.0])
        e2 = np.array([0.0, 1.0, 0.0])
        coordinates[2] = coordinates[1] + r * (-np.cos(theta) * e1 + np.sin(theta) * e2)
    for index in range(3, atom_count):
        r = values[index, 0]
        theta = np.radians(values[index, 1])
        phi = np.radians(values[index, 2] - 180.0)
        rc = coordinates[index - 1]
        rb = coordinates[index - 2]
        ra = coordinates[index - 3]
        e1 = rc - rb
        e1_norm = np.linalg.norm(e1)
        e1 = np.array([1.0, 0.0, 0.0]) if np.isclose(e1_norm, 0.0) else e1 / e1_norm
        normal = np.cross(rb - ra, e1)
        normal_norm = np.linalg.norm(normal)
        if np.isclose(normal_norm, 0.0):
            trial = np.array([0.0, 0.0, 1.0])
            if np.linalg.norm(np.cross(e1, trial)) < 1e-8:
                trial = np.array([0.0, 1.0, 0.0])
            normal = np.cross(e1, trial)
            normal_norm = np.linalg.norm(normal)
        e3 = normal / normal_norm
        e2 = np.cross(e3, e1)
        e2 = e2 / np.linalg.norm(e2)
        coordinates[index] = rc + r * (
            -np.cos(theta) * e1
            + np.sin(theta) * np.cos(phi) * e2
            + np.sin(theta) * np.sin(phi) * e3
        )
    return canonical_cartesian_coordinates(coordinates, atom_count=atom_count)


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
