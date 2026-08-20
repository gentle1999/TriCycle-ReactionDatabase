import numpy as np
import pytest

from tricycle_reaction_db.application.services.molecular_geometry import (
    _coordinate_alignment,
)


def test_geometry_error_removes_translation_and_proper_rotation() -> None:
    reference = np.asarray(
        [[0.0, 0.0, 0.0], [1.2, 0.1, 0.0], [-0.2, 0.9, 0.4]],
        dtype=np.float64,
    )
    angle = np.deg2rad(63.0)
    rotation = np.asarray(
        [
            [np.cos(angle), -np.sin(angle), 0.0],
            [np.sin(angle), np.cos(angle), 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    observed = reference @ rotation + np.asarray([7.0, -3.5, 2.25])

    rmsd, max_abs, transform_values = _coordinate_alignment(observed, reference)
    transform = np.asarray(transform_values).reshape(4, 4)
    homogeneous = np.column_stack((observed, np.ones(observed.shape[0])))
    aligned = (transform @ homogeneous.T).T[:, :3]

    assert rmsd < 1e-12
    assert max_abs < 1e-12
    assert aligned == pytest.approx(reference, abs=1e-12)
    assert np.linalg.det(transform[:3, :3]) == pytest.approx(1.0)
