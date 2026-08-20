"""Internal helpers shared by idempotent persistence services."""

import json
import secrets
from hashlib import sha256
from math import isnan
from time import time
from typing import Any, cast
from uuid import UUID

import numpy as np
from pydantic import BaseModel
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlmodel import Session

_FAST_INSERT_SAFE_LOCK_NAMES = frozenset(
    {
        "calculation_frame_segment",
        "calculation_frame_file",
        "frame_energy_result",
        "energy_observation",
        "geometry_optimization_result",
        "vibration_result",
        "calculation_status_result",
        "scientific_array",
        "ScientificArrayAssignment",
        "MolecularOrbitalResult",
        "ChargeSpinPopulationResult",
        "AtomicPopulationSeries",
        "PolarizabilityResult",
        "NMRResult",
        "NMRShieldingTensor",
        "BondOrderResult",
        "TotalSpinResult",
        "SinglePointPropertyResult",
        "ElectronicStateSet",
        "ElectronicState",
        "ElectronicConfiguration",
        "MultireferenceResult",
        "ImplicitSolvationResult",
        "thermochemistry_result",
    }
)


def _identity_lock_id(*parts: object) -> int:
    payload = json.dumps(
        [str(part) for part in parts],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return int.from_bytes(sha256(payload).digest()[:8], byteorder="big", signed=True)


def _uuid7() -> UUID:
    """Create a RFC 9562 UUIDv7 without a database round trip."""

    timestamp_ms = int(time() * 1000) & ((1 << 48) - 1)
    value = timestamp_ms << 80
    value |= 0x7 << 76
    value |= secrets.randbits(12) << 64
    value |= 0b10 << 62
    value |= secrets.randbits(62)
    return UUID(int=value)


def _acquire_identity_locks(session: Session, *keys: tuple[object, ...]) -> None:
    if session.info.get("tricycle_fast_insert", False) and all(
        key and str(key[0]) in _FAST_INSERT_SAFE_LOCK_NAMES for key in keys
    ):
        return
    lock_ids = sorted({_identity_lock_id(*key) for key in keys})
    connection = session.connection()
    for lock_id in lock_ids:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": lock_id},
        )


def _fast_insert_enabled(session: Session) -> bool:
    return bool(session.info.get("tricycle_fast_insert", False))


def _require_id(entity: object, *, label: str) -> UUID:
    entity_id = getattr(entity, "id", None)
    if not isinstance(entity_id, UUID):
        raise RuntimeError(f"{label} must be flushed before persisting dependent facts")
    return entity_id


def _same_value(actual: object, expected: object) -> bool:
    if isinstance(actual, np.ndarray) and isinstance(expected, np.ndarray):
        return bool(np.array_equal(actual, expected, equal_nan=True))
    if isinstance(actual, float) and isinstance(expected, float):
        return actual == expected or (isnan(actual) and isnan(expected))
    return bool(actual == expected)


def _assert_record_matches(
    entity: object,
    record: BaseModel,
    *,
    label: str,
    exclude: set[str] | None = None,
) -> None:
    for field_name, expected in record.model_dump(exclude=exclude).items():
        actual = getattr(entity, field_name)
        if not _same_value(actual, expected):
            raise ValueError(
                f"{label} identity resolved to different {field_name}: {actual!r} != {expected!r}"
            )


def _prepare_new_entity(session: Session, entity: object) -> None:
    if session.info.get("tricycle_fast_insert", False) and getattr(entity, "id", None) is None:
        # Client-side UUIDv7 lets SQLAlchemy batch independent child rows. The
        # regular path keeps server-generated IDs for strict idempotent retries.
        object.__setattr__(entity, "id", _uuid7())
    if session.info.get("tricycle_fast_insert", False):
        mapper = cast(Any, sa_inspect(entity)).mapper
        for relationship in mapper.relationships:
            if relationship.uselist:
                continue
            related = getattr(entity, relationship.key, None)
            if related is None:
                continue
            for local_column, remote_column in relationship.local_remote_pairs:
                value = getattr(related, remote_column.key, None)
                if value is not None and getattr(entity, local_column.key, None) is None:
                    setattr(entity, local_column.key, value)
    session.add(entity)


def _flush_new_entity(session: Session, entity: object, *, label: str) -> None:
    _prepare_new_entity(session, entity)
    # Defer new revision-local children so SQLAlchemy can batch them by table.
    if not session.info.get("tricycle_fast_insert", False):
        session.flush()
    _require_id(entity, label=label)


def _flush_shared_entity(session: Session, entity: object, *, label: str) -> None:
    """Flush one cross-revision identity without draining pending child rows."""

    _prepare_new_entity(session, entity)
    session.flush([entity] if session.info.get("tricycle_fast_insert", False) else None)
    _require_id(entity, label=label)


__all__ = [
    "_acquire_identity_locks",
    "_assert_record_matches",
    "_fast_insert_enabled",
    "_flush_new_entity",
    "_flush_shared_entity",
    "_identity_lock_id",
    "_require_id",
    "_uuid7",
]
