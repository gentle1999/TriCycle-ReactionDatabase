"""SQLAlchemy mapping for NumPy arrays stored as NPY binary payloads."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from typing import Any, Final

import numpy as np
import numpy.typing as npt
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import LargeBinary, TypeDecorator

# Prototype limit. Revisit with representative Gaussian and ORCA matrix sizes.
DEFAULT_MAX_INLINE_ARRAY_BYTES: Final = 64 * 1024 * 1024
_SUPPORTED_DTYPE_KINDS: Final = frozenset("biufc")


@dataclass(frozen=True, slots=True)
class NumpyArraySummary:
    """Queryable metadata derived from the exact persisted NPY payload."""

    dtype: str
    shape: tuple[int, ...]
    nbytes: int
    sha256: str


class EncodedNumpyArray(np.ndarray):
    """An array carrying the exact NPY payload generated for it.

    The payload is only attached to arrays created by ``encode_numpy_array``.
    It lets the ORM bind step reuse serialization work already performed by
    application-level validation.
    """

    _npy_payload: bytes
    _npy_summary: NumpyArraySummary | None

    def __new__(
        cls,
        value: npt.NDArray[np.generic],
        payload: bytes,
    ) -> EncodedNumpyArray:
        encoded = np.asarray(value).view(cls)
        encoded._npy_payload = payload
        encoded._npy_summary = None
        encoded.setflags(write=False)
        return encoded

    def __array_finalize__(self, value: object) -> None:
        del value


def _validate_max_inline_array_bytes(max_inline_array_bytes: int) -> None:
    if (
        isinstance(max_inline_array_bytes, bool)
        or not isinstance(max_inline_array_bytes, int)
        or max_inline_array_bytes <= 0
    ):
        raise ValueError("max_inline_array_bytes must be a positive integer")


def _validate_array(value: object) -> npt.NDArray[np.generic]:
    if not isinstance(value, np.ndarray):
        raise TypeError("NumpyArray only accepts numpy.ndarray values")
    if value.dtype.hasobject:
        raise TypeError("object dtype arrays cannot be stored with allow_pickle=False")
    if value.dtype.kind not in _SUPPORTED_DTYPE_KINDS:
        raise TypeError(f"unsupported NumPy dtype: {value.dtype}")
    return value


def _check_payload_size(payload: bytes, max_inline_array_bytes: int) -> None:
    if len(payload) > max_inline_array_bytes:
        raise ValueError(
            "NPY payload exceeds max_inline_array_bytes "
            f"({len(payload)} > {max_inline_array_bytes})"
        )


def _encode_numpy_array(
    value: object,
    *,
    max_inline_array_bytes: int,
) -> tuple[npt.NDArray[np.generic], bytes]:
    array = _validate_array(value)
    cached_payload = cached_numpy_array_payload(array)
    if cached_payload is not None:
        _check_payload_size(cached_payload, max_inline_array_bytes)
        return array, cached_payload
    buffer = BytesIO()
    np.save(buffer, array, allow_pickle=False)
    payload = buffer.getvalue()
    _check_payload_size(payload, max_inline_array_bytes)
    return array, payload


def encode_numpy_array(
    value: object,
    *,
    max_inline_array_bytes: int = DEFAULT_MAX_INLINE_ARRAY_BYTES,
) -> tuple[EncodedNumpyArray, bytes]:
    """Encode an array once and retain the exact payload on the array value."""

    _validate_max_inline_array_bytes(max_inline_array_bytes)
    array, payload = _encode_numpy_array(
        value,
        max_inline_array_bytes=max_inline_array_bytes,
    )
    return EncodedNumpyArray(array, payload), payload


def cached_numpy_array_payload(value: object) -> bytes | None:
    """Return a payload cached by ``encode_numpy_array``, if present."""

    if not isinstance(value, EncodedNumpyArray) or value.flags.writeable:
        return None
    payload = getattr(value, "_npy_payload", None)
    return payload if isinstance(payload, bytes) else None


def cached_numpy_array_summary(value: object) -> NumpyArraySummary | None:
    """Return summary metadata cached alongside an encoded array, if present."""

    if not isinstance(value, EncodedNumpyArray):
        return None
    summary = getattr(value, "_npy_summary", None)
    return summary if isinstance(summary, NumpyArraySummary) else None


def summarize_numpy_array(
    value: npt.NDArray[np.generic],
    *,
    max_inline_array_bytes: int = DEFAULT_MAX_INLINE_ARRAY_BYTES,
) -> NumpyArraySummary:
    """Return database metadata and the hash of the exact NPY encoding."""

    _validate_max_inline_array_bytes(max_inline_array_bytes)
    array, payload = _encode_numpy_array(
        value,
        max_inline_array_bytes=max_inline_array_bytes,
    )
    cached_summary = cached_numpy_array_summary(array)
    if cached_summary is not None:
        return cached_summary
    summary = NumpyArraySummary(
        dtype=str(array.dtype),
        shape=tuple(int(dimension) for dimension in array.shape),
        nbytes=int(array.nbytes),
        sha256=sha256(payload).hexdigest(),
    )
    if isinstance(array, EncodedNumpyArray):
        array._npy_summary = summary
    return summary


class NumpyArray(TypeDecorator[npt.NDArray[np.generic]]):
    """Persist numeric arrays as NPY-encoded ``LargeBinary`` values."""

    impl = LargeBinary
    cache_ok = True

    def __init__(
        self,
        max_inline_array_bytes: int = DEFAULT_MAX_INLINE_ARRAY_BYTES,
    ) -> None:
        _validate_max_inline_array_bytes(max_inline_array_bytes)
        self.max_inline_array_bytes = max_inline_array_bytes
        super().__init__()

    @property
    def python_type(self) -> type[np.ndarray[Any, Any]]:
        return np.ndarray

    def process_bind_param(
        self,
        value: npt.NDArray[np.generic] | None,
        dialect: Dialect,
    ) -> bytes | None:
        del dialect
        if value is None:
            return None
        payload = cached_numpy_array_payload(value)
        if payload is None:
            _, payload = _encode_numpy_array(
                value,
                max_inline_array_bytes=self.max_inline_array_bytes,
            )
        else:
            _check_payload_size(payload, self.max_inline_array_bytes)
        return payload

    def process_result_value(
        self,
        value: bytes | bytearray | memoryview | None,
        dialect: Dialect,
    ) -> npt.NDArray[np.generic] | None:
        del dialect
        if value is None:
            return None

        payload = bytes(value)
        _check_payload_size(payload, self.max_inline_array_bytes)
        try:
            loaded = np.load(BytesIO(payload), allow_pickle=False)
        except (EOFError, ValueError) as error:
            raise ValueError("invalid or unsupported NPY payload") from error
        if not isinstance(loaded, np.ndarray):
            loaded.close()
            raise ValueError("NumpyArray payload must contain one NPY array, not an NPZ archive")

        array = _validate_array(loaded)
        array.setflags(write=False)
        return array

    def compare_values(self, x: object, y: object) -> bool:
        """Compare persisted array values without returning an ndarray."""

        if x is y:
            return True
        if not isinstance(x, np.ndarray) or not isinstance(y, np.ndarray):
            return False
        if x.dtype != y.dtype or x.shape != y.shape:
            return False
        return bool(np.array_equal(x, y, equal_nan=True))
