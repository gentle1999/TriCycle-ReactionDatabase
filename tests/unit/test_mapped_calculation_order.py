"""Atom axes, Cartesian block axes, and subset indices use the same permutation."""

import numpy as np
import pytest

from tricycle_reaction_db.application.services.mapped_calculation_order import (
    MappedCalculationOrder,
)
from tricycle_reaction_db.domain.enums import ScientificArrayKind as Kind


def test_composes_source_geometry_and_mapped_orders():
    projection = MappedCalculationOrder.from_geometry([2, 0, 1], [2, 3, 1])
    assert projection.source_to_mapped == (0, 1, 2)
    with pytest.raises(ValueError, match="permutation"):
        MappedCalculationOrder.from_geometry([0, 0, 2], [1, 2, 3])


def test_every_atomic_axis_follows_same_order_without_mutating_source():
    projection = MappedCalculationOrder((2, 0, 1))
    forces = np.arange(9).reshape(3, 3)
    result, _ = projection.scientific_array(Kind.FORCES, forces)
    np.testing.assert_array_equal(result, forces[[1, 2, 0]])
    modes = np.arange(18).reshape(2, 3, 3)
    result, _ = projection.scientific_array(Kind.NORMAL_MODES, modes)
    np.testing.assert_array_equal(result, modes[:, [1, 2, 0], :])
    hessian = np.arange(81).reshape(9, 9)
    result, _ = projection.scientific_array(Kind.HESSIAN, hessian)
    blocks = [3, 4, 5, 6, 7, 8, 0, 1, 2]
    np.testing.assert_array_equal(result, hessian[np.ix_(blocks, blocks)])
    result, _ = projection.scientific_array(Kind.BOND_ORDER_MATRIX, forces)
    np.testing.assert_array_equal(result, forces[np.ix_([1, 2, 0], [1, 2, 0])])
    result, _ = projection.scientific_array(Kind.ATOMIC_POPULATION, [10, 20, 30])
    np.testing.assert_array_equal(result, [20, 30, 10])
    np.testing.assert_array_equal(forces, np.arange(9).reshape(3, 3))


@pytest.mark.parametrize(
    ("kind", "shape", "axes"),
    [
        (Kind.FORCES, (3, 3), (0,)),
        (Kind.NORMAL_MODES, (2, 3, 3), (1,)),
        (Kind.ATOMIC_POPULATION, (3,), (0,)),
        (Kind.BOND_ORDER_MATRIX, (3, 3), (0, 1)),
        (Kind.FUKUI_POSITIVE, (3,), (0,)),
        (Kind.FUKUI_NEGATIVE, (3,), (0,)),
        (Kind.FUKUI_ZERO, (3,), (0,)),
        (Kind.FRACTIONAL_OCCUPATION_DENSITY, (3,), (0,)),
    ],
)
def test_each_atom_indexed_scientific_array_uses_map_order(kind, shape, axes):
    projection = MappedCalculationOrder((2, 0, 1))
    source = np.arange(np.prod(shape)).reshape(shape)

    result, metadata = projection.scientific_array(kind, source)
    expected = source
    for axis in axes:
        expected = np.take(expected, [1, 2, 0], axis=axis)

    np.testing.assert_array_equal(result, expected)
    assert metadata["atom_axes"] == list(axes)


def test_nmr_subset_reorders_values_and_indices_together():
    projection = MappedCalculationOrder((2, 0, 1))
    result, metadata = projection.scientific_array(
        Kind.NMR_COUPLING_J, [[1, 2], [3, 4]], coupling_atom_indices=[0, 2]
    )
    np.testing.assert_array_equal(result, [[4, 3], [2, 1]])
    assert metadata["atom_indices"] == [1, 2]
    assert metadata["atom_map_numbers"] == [2, 3]
    assert projection.atom_indices([0]) == [2]
    with pytest.raises(ValueError, match="outside"):
        projection.atom_indices([-1])


@pytest.mark.parametrize(
    ("kind", "shape"),
    [
        (Kind.NMR_SHIELDING_TENSOR, (3, 3)),
        (Kind.NMR_PRINCIPAL_VALUES, (3,)),
    ],
)
def test_per_atom_nmr_arrays_export_both_mapped_index_forms(kind, shape):
    from types import SimpleNamespace

    from tricycle_reaction_db.application.services.mapped_reaction_geometry_export import (
        _calculation_record,
    )

    frame = SimpleNamespace(
        id="frame",
        observed_to_geometry_atom_indices=[1, 2, 0],
        observed_coordinates=np.arange(9).reshape(3, 3),
    )
    array = SimpleNamespace(
        id="array",
        kind=kind,
        ordinal=0,
        unit="ppm",
        data=np.arange(np.prod(shape)).reshape(shape),
        array_metadata={"atom_order": "geometry_source_atom_order"},
    )
    shielding = SimpleNamespace(
        atom_index=0,
        isotropic_ppm=12.5,
        anisotropy_ppm=2.5,
        orientation="source",
    )

    record = _calculation_record(
        frame,  # pyright: ignore[reportArgumentType]
        [2, 3, 1],
        [(array, None, shielding)],  # pyright: ignore[reportArgumentType]
    )

    metadata = record["scientific_arrays"][0]["metadata"]
    assert metadata["atom_order"] == "mapped_reaction"
    assert metadata["atom_index"] == 2
    assert metadata["atom_map_number"] == 3
    assert "geometry_source_atom_order" not in metadata.values()


def test_non_atom_arrays_are_not_reordered_based_on_coincidental_shape():
    projection = MappedCalculationOrder((2, 0, 1))
    dipole = np.asarray([10, 20, 30])
    result, _ = projection.scientific_array(Kind.DIPOLE, dipole)
    np.testing.assert_array_equal(result, dipole)
    with pytest.raises(ValueError, match="shape"):
        projection.scientific_array(Kind.HESSIAN, np.zeros((3, 3)))


def test_export_composes_frame_mapping_for_coordinates_and_all_array_values():
    from types import SimpleNamespace
    from uuid import UUID

    from tricycle_reaction_db.application.services.mapped_reaction_geometry_export import (
        _calculation_record,
    )

    frame = SimpleNamespace(
        id=UUID(int=1),
        observed_to_geometry_atom_indices=[1, 2, 0],
        observed_coordinates=np.arange(9).reshape(3, 3),
    )
    forces = SimpleNamespace(
        id=UUID(int=2),
        kind=Kind.FORCES,
        ordinal=0,
        unit="hartree/bohr",
        data=np.arange(9).reshape(3, 3),
        array_metadata={"axis_order": ["source_atom", "xyz"]},
    )
    record = _calculation_record(
        frame,  # pyright: ignore[reportArgumentType]
        [2, 1, 3],
        [(forces, None, None)],  # pyright: ignore[reportArgumentType]
    )
    assert record["source_to_mapped_atom_indices"] == [0, 2, 1]
    assert record["observed_coordinates_angstrom"] == [[0, 1, 2], [6, 7, 8], [3, 4, 5]]
    array = record["scientific_arrays"][0]
    assert array["data"] == record["observed_coordinates_angstrom"]
    assert "axis_order" not in array["metadata"]
    assert array["metadata"]["atom_axes"] == [0]
    assert forces.array_metadata["axis_order"] == ["source_atom", "xyz"]
