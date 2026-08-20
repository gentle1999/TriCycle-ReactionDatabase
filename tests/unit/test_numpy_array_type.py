from hashlib import sha256
from io import BytesIO

import numpy as np
import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import LargeBinary

from tricycle_reaction_db.db.types import (
    DEFAULT_MAX_INLINE_ARRAY_BYTES,
    NumpyArray,
    NumpyArraySummary,
    summarize_numpy_array,
)


def _dialect() -> postgresql.dialect:
    return postgresql.dialect()


@pytest.mark.parametrize(
    "source",
    [
        np.array([[1, 2], [3, 4]], dtype=np.int32),
        np.array([True, False, True], dtype=np.bool_),
        np.array([1.25, np.nan, np.inf, -np.inf], dtype=np.float64),
        np.array([1 + 2j, 3 - 4j], dtype=np.complex128),
        np.asfortranarray(np.arange(12, dtype=np.float32).reshape(3, 4)),
        np.empty((0, 3), dtype=np.float64),
    ],
)
def test_round_trip_preserves_array_value_dtype_shape_and_memory_order(
    source: np.ndarray,
) -> None:
    array_type = NumpyArray()

    payload = array_type.process_bind_param(source, _dialect())
    assert isinstance(payload, bytes)
    loaded = array_type.process_result_value(payload, _dialect())

    assert isinstance(loaded, np.ndarray)
    assert loaded.dtype == source.dtype
    assert loaded.shape == source.shape
    assert np.array_equal(loaded, source, equal_nan=True)
    assert loaded.flags.f_contiguous == source.flags.f_contiguous
    assert not loaded.flags.writeable


def test_binary_payload_is_npy_with_pickle_disabled() -> None:
    source = np.arange(9, dtype=np.float64).reshape(3, 3)
    payload = NumpyArray().process_bind_param(source, _dialect())
    assert payload is not None

    loaded = np.load(BytesIO(payload), allow_pickle=False)

    np.testing.assert_array_equal(loaded, source)


def test_none_is_passed_through() -> None:
    array_type = NumpyArray()

    assert array_type.process_bind_param(None, _dialect()) is None
    assert array_type.process_result_value(None, _dialect()) is None


@pytest.mark.parametrize(
    "value",
    [
        [1.0, 2.0],
        np.array([{"unsafe": True}], dtype=object),
        np.array(["not", "numeric"]),
        np.array([(1.0, 2)], dtype=[("energy", "f8"), ("step", "i4")]),
    ],
)
def test_bind_rejects_non_arrays_and_unsupported_dtypes(value: object) -> None:
    with pytest.raises(TypeError):
        NumpyArray().process_bind_param(value, _dialect())  # type: ignore[arg-type]


def test_result_rejects_object_dtype_npy_without_loading_pickle() -> None:
    buffer = BytesIO()
    np.save(buffer, np.array([{"unsafe": True}], dtype=object), allow_pickle=True)

    with pytest.raises(ValueError, match="invalid or unsupported NPY payload"):
        NumpyArray().process_result_value(buffer.getvalue(), _dialect())


def test_result_rejects_npz_archive() -> None:
    buffer = BytesIO()
    np.savez(buffer, values=np.arange(3))

    with pytest.raises(ValueError, match="NPZ archive"):
        NumpyArray().process_result_value(buffer.getvalue(), _dialect())


def test_payload_size_limit_is_enforced_on_bind_and_result() -> None:
    source = np.arange(4, dtype=np.float64)
    payload = NumpyArray().process_bind_param(source, _dialect())
    assert payload is not None
    restrictive_type = NumpyArray(max_inline_array_bytes=len(payload) - 1)

    with pytest.raises(ValueError, match="exceeds max_inline_array_bytes"):
        restrictive_type.process_bind_param(source, _dialect())
    with pytest.raises(ValueError, match="exceeds max_inline_array_bytes"):
        restrictive_type.process_result_value(payload, _dialect())


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_payload_size_limit_must_be_a_positive_integer(limit: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        NumpyArray(max_inline_array_bytes=limit)  # type: ignore[arg-type]


def test_loaded_array_is_read_only() -> None:
    source = np.arange(3, dtype=np.float64)
    payload = NumpyArray().process_bind_param(source, _dialect())
    assert payload is not None
    loaded = NumpyArray().process_result_value(payload, _dialect())
    assert loaded is not None

    with pytest.raises(ValueError, match="read-only"):
        loaded[0] = 10.0


def test_compare_values_always_returns_scalar_bool() -> None:
    array_type = NumpyArray()
    source = np.array([1.0, np.nan])

    comparisons = [
        array_type.compare_values(source, source.copy()),
        array_type.compare_values(source, np.array([1.0, 2.0])),
        array_type.compare_values(source, source.astype(np.float32)),
        array_type.compare_values(source, np.array([[1.0, np.nan]])),
        array_type.compare_values(source, None),
        array_type.compare_values(None, None),
    ]

    assert all(type(result) is bool for result in comparisons)
    assert comparisons == [True, False, False, False, False, True]


def test_summary_matches_exact_persisted_payload() -> None:
    source = np.asfortranarray(np.arange(12, dtype=np.float64).reshape(3, 4))
    array_type = NumpyArray()
    payload = array_type.process_bind_param(source, _dialect())
    assert payload is not None

    summary = summarize_numpy_array(source)

    assert summary == NumpyArraySummary(
        dtype="float64",
        shape=(3, 4),
        nbytes=source.nbytes,
        sha256=sha256(payload).hexdigest(),
    )


def test_summary_obeys_the_same_validation_and_size_policy() -> None:
    with pytest.raises(TypeError, match="object dtype"):
        summarize_numpy_array(np.array([object()], dtype=object))
    with pytest.raises(ValueError, match="exceeds max_inline_array_bytes"):
        summarize_numpy_array(np.arange(3), max_inline_array_bytes=1)


def test_sqlalchemy_type_metadata_is_stable() -> None:
    array_type = NumpyArray()

    assert array_type.cache_ok is True
    assert array_type.python_type is np.ndarray
    assert isinstance(array_type.impl, LargeBinary)
    assert array_type.max_inline_array_bytes == DEFAULT_MAX_INLINE_ARRAY_BYTES
