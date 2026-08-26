"""Internal helpers shared by idempotent persistence services."""

import json
import os
import secrets
from datetime import UTC, datetime
from hashlib import sha256
from math import isnan
from time import perf_counter, time
from typing import Any, cast
from uuid import UUID

import numpy as np
from pydantic import BaseModel
from sqlalchemy import insert, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import make_transient_to_detached
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.util import await_only
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
        "parse_revision_artifact",
        "parse_revision",
        "parse_revision_finalize",
    }
)

# ``_prepare_new_entity`` runs once for every revision-local row.  Mapper
# reflection is comparatively expensive at this scale, so keep the scalar
# relationship foreign-key bindings by mapped class and only inspect each
# mapper once per process.
_FAST_RELATIONSHIP_BINDINGS: dict[
    type[Any], tuple[tuple[str, str, str], ...]
] = {}
_FAST_RELATIONSHIP_KEYS: dict[type[Any], frozenset[str]] = {}
_FAST_RELATIONSHIPS: dict[type[Any], dict[str, Any]] = {}
_FAST_MAPPERS: dict[type[Any], Any] = {}


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
    if not lock_ids:
        return
    lock_stats = session.info.setdefault("_identity_lock_stats", {})
    lock_stats["calls"] = int(lock_stats.get("calls", 0)) + 1
    lock_stats["requested_ids"] = int(lock_stats.get("requested_ids", 0)) + len(lock_ids)
    prefixes = lock_stats.setdefault("prefixes", {})
    for key in keys:
        if key:
            prefix = str(key[0])
            prefixes[prefix] = int(prefixes.get(prefix, 0)) + 1
    connection = session.connection()
    transaction_marker = id(session.get_transaction())
    cached_transaction_marker = session.info.get("_identity_lock_transaction_marker")
    lock_cache = session.info.setdefault("_identity_lock_cache", set())
    if cached_transaction_marker != transaction_marker:
        lock_cache.clear()
        session.info["_identity_lock_transaction_marker"] = transaction_marker
    uncached_lock_ids = [lock_id for lock_id in lock_ids if lock_id not in lock_cache]
    if not uncached_lock_ids:
        return
    lock_stats["uncached_ids"] = int(lock_stats.get("uncached_ids", 0)) + len(
        uncached_lock_ids
    )
    # PostgreSQL acquires the sorted lock set in one statement. This preserves
    # deterministic deadlock ordering while avoiding one network round trip per
    # identity in a prepared upload batch.
    connection.execute(
        text(
            "SELECT pg_advisory_xact_lock(lock_id) "
            "FROM unnest(CAST(:lock_ids AS bigint[])) AS locks(lock_id)"
        ),
        {"lock_ids": uncached_lock_ids},
    )
    lock_cache.update(uncached_lock_ids)


def _fast_insert_enabled(session: Session) -> bool:
    return bool(session.info.get("tricycle_fast_insert", False))


def _new_entity(session: Session, entity_type: type[Any], **values: Any) -> Any:
    """Construct a validated table entity without repeating Pydantic parsing.

    Fast batch records already passed through their DTO validators. SQLModel's
    regular constructor validates every field again and becomes a measurable
    cost for tens of thousands of revision-local rows. The normal path keeps
    the original constructor and its strict validation semantics.
    """

    if not _fast_insert_enabled(session):
        return entity_type(**values)
    mapper = _FAST_MAPPERS.get(entity_type)
    if mapper is None:
        mapper = cast(Any, sa_inspect(entity_type)).mapper
        _FAST_MAPPERS[entity_type] = mapper
    entity = mapper.class_manager.new_instance()
    relationships = _FAST_RELATIONSHIPS.get(entity_type)
    if relationships is None:
        relationships = {
            relationship.key: relationship for relationship in mapper.relationships
        }
        _FAST_RELATIONSHIPS[entity_type] = relationships
        _FAST_RELATIONSHIP_KEYS[entity_type] = frozenset(relationships)
    relationship_keys = _FAST_RELATIONSHIP_KEYS[entity_type]
    # New instances are not dirty-tracked yet.  Store scalar values in one
    # dictionary update; this avoids invoking an instrumented descriptor for
    # every column of every revision-local row.
    if relationship_keys:
        entity.__dict__.update(
            (key, value) for key, value in values.items() if key not in relationship_keys
        )
    else:
        entity.__dict__.update(values)
    for key, value in values.items():
        if key not in relationship_keys:
            continue
        # Assigning a related persistent object through the normal instrumented
        # descriptor triggers ``save-update`` cascade and silently attaches
        # this new row to the Session.  Keep it transient until the table-level
        # Core executemany below while retaining the object graph for callers.
        set_committed_value(entity, key, value)
        relationship = relationships[key]
        inverse_key = relationship.back_populates
        if (
            inverse_key
            and inverse_key in {"segments", "frames"}
            and relationship.mapper.class_ is type(value)
        ):
            inverse = relationship.mapper.relationships.get(inverse_key)
            if inverse is not None and inverse.uselist:
                # Do not access the descriptor here: an unloaded collection
                # would issue a SELECT for every child row.
                current = value.__dict__.get(inverse_key)
                if current is None:
                    set_committed_value(value, inverse_key, [entity])
                else:
                    current.append(entity)
    return entity


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


def _prepare_new_entity(
    session: Session,
    entity: object,
    *,
    attach: bool = True,
) -> None:
    fast_insert = session.info.get("tricycle_fast_insert", False)
    entity_dict = cast(Any, entity).__dict__
    if fast_insert and entity_dict.get("id") is None:
        # Client-side UUIDv7 lets SQLAlchemy batch independent child rows. The
        # regular path keeps server-generated IDs for strict idempotent retries.
        object.__setattr__(entity, "id", _uuid7())
    if fast_insert:
        # Supplying the immutable timestamp avoids a per-row RETURNING round
        # trip for PostgreSQL's ``now()`` server default. The value remains
        # UTC and is only used for newly-created, revision-local rows.
        if entity_dict.get("created_at") is None:
            object.__setattr__(entity, "created_at", datetime.now(UTC))
        entity_type = type(entity)
        bindings = _FAST_RELATIONSHIP_BINDINGS.get(entity_type)
        if bindings is None:
            mapper = cast(Any, sa_inspect(entity)).mapper
            bindings = tuple(
                (
                    relationship.key,
                    local_column.key,
                    remote_column.key,
                )
                for relationship in mapper.relationships
                if not relationship.uselist
                for local_column, remote_column in relationship.local_remote_pairs
            )
            _FAST_RELATIONSHIP_BINDINGS[entity_type] = bindings
        for relationship_key, local_key, remote_key in bindings:
            related = entity_dict.get(relationship_key)
            if related is None:
                continue
            value = related.__dict__.get(remote_key)
            if value is not None and entity_dict.get(local_key) is None:
                object.__setattr__(entity, local_key, value)
    if attach:
        session.add(entity)


def _attach_pending_entities(session: Session) -> None:
    """Attach deferred fast-path rows in one ORM operation."""

    pending = session.info.pop("_fast_pending_entities", None)
    if pending:
        if session.info.get("tricycle_fast_insert", False) and not session.info.get(
            "tricycle_bulk_insert_disabled", False
        ):
            session.info["_fast_pending_entities"] = pending
            _bulk_insert_pending_entities(session)
        else:
            session.add_all(pending)


async def _copy_rows_to_postgresql(
    driver_connection: Any,
    statement: str,
    rows: list[tuple[Any, ...]],
) -> None:
    """Stream already type-adapted rows through psycopg's COPY protocol."""

    async with driver_connection.cursor() as cursor, cursor.copy(statement) as copy_writer:
        for row in rows:
            await copy_writer.write_row(row)


def _copy_compatible(columns: tuple[Any, ...]) -> bool:
    """Return whether COPY can preserve every SQLAlchemy bind expression."""

    return all(
        type(column.type).__name__ not in {"RdkitMol", "RdkitReaction"}
        for column in columns
    )


def _bulk_insert_pending_entities(session: Session) -> None:
    """Insert fast-path rows by mapped table, avoiding ORM flush bookkeeping.

    Fast ingestion assigns UUIDs and relationship foreign keys before rows are
    queued, so revision-local entities do not need ORM-generated identities or
    relationship synchronization. Core executemany keeps the same transaction
    and SQLAlchemy type adapters while removing per-object unit-of-work work.
    Objects remain available to the caller as detached identity holders; later
    reads use their UUIDs and normal SELECTs.
    """

    pending = session.info.pop("_fast_pending_entities", None)
    if not pending:
        return
    diagnostics = session.info.setdefault(
        "_fast_bulk_insert_diagnostics",
        {"pending": 0, "transient": 0, "group_ms": 0.0, "prepare_ms": 0.0, "execute_ms": 0.0},
    )
    diagnostics["pending"] += len(pending)
    state_counts = diagnostics.setdefault("states", {})
    group_started = perf_counter()
    grouped: dict[type[Any], list[Any]] = {}
    transient_entities: list[Any] = []
    for entity in pending:
        state = sa_inspect(entity)
        state_name = (
            "transient" if state.transient else "pending" if state.pending else
            "persistent" if state.persistent else "detached" if state.detached else "other"
        )
        state_counts[state_name] = int(state_counts.get(state_name, 0)) + 1
        # A relationship cascade or a nested inference savepoint may have
        # already attached this object since it was queued. Do not issue a
        # second Core INSERT for rows SQLAlchemy already owns.
        if state.pending:
            # Relationship assignment can attach a row through cascade before
            # it reaches this queue. Expunge that unflushed instance and treat
            # it like every other deferred row.
            session.expunge(entity)
            state = sa_inspect(entity)
        if state.transient or state.detached:
            transient_entities.append(entity)
            grouped.setdefault(type(entity), []).append(entity)
        elif state.persistent:
            # A nested operation may have already flushed this row. Keep it
            # out of the Core batch to avoid duplicate primary keys.
            continue
    diagnostics["transient"] += len(transient_entities)
    diagnostics["group_ms"] += (perf_counter() - group_started) * 1000

    # Relationship foreign keys are already copied to scalar columns by
    # ``_prepare_new_entity``. A stable parent-before-child order is enough for
    # these revision-local rows and avoids relying on SQLAlchemy's private UoW.
    dependencies: dict[type[Any], set[type[Any]]] = {
        entity_type: set()
        for entity_type in grouped
    }
    grouped_by_table = {
        cast(Any, sa_inspect(entity_type)).mapper.local_table: entity_type
        for entity_type in grouped
    }
    for entity_type in grouped:
        mapper = cast(Any, sa_inspect(entity_type)).mapper
        # Build dependencies from actual table-level foreign keys rather than
        # relationship descriptors.  Bidirectional one-to-one relationships
        # expose reverse pairs that do not represent an INSERT dependency.
        for column in mapper.local_table.columns:
            for foreign_key in column.foreign_keys:
                parent = grouped_by_table.get(foreign_key.column.table)
                if parent is not None and parent is not entity_type:
                    dependencies[entity_type].add(parent)
    ordered: list[type[Any]] = []
    remaining = set(grouped)
    while remaining:
        ready = sorted(
            [
                entity_type
                for entity_type in remaining
                if not (dependencies[entity_type] & remaining)
            ],
            key=lambda entity_type: entity_type.__name__,
        )
        if not ready:
            ready = sorted(remaining, key=lambda entity_type: entity_type.__name__)
        ordered.extend(ready)
        remaining.difference_update(ready)

    for entity_type in ordered:
        prepare_started = perf_counter()
        mapper = cast(Any, sa_inspect(entity_type)).mapper
        column_attrs = {
            column: attr.key
            for attr in mapper.column_attrs
            for column in attr.columns
        }
        columns = tuple(
            column
            for column in mapper.local_table.columns
            if column.computed is None
            and column.identity is None
            and not (column.autoincrement is True and column.primary_key)
        )
        rows_by_signature: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for entity in grouped[entity_type]:
            row: dict[str, Any] = {}
            for column in columns:
                value = entity.__dict__.get(column_attrs[column])
                # Passing NULL explicitly suppresses PostgreSQL server
                # defaults. Omit defaulted columns so computed/schema-version
                # defaults behave exactly as they do on the ORM path.
                if value is None and (
                    column.server_default is not None or column.default is not None
                ):
                    continue
                row[column.key] = value
            signature = tuple(sorted(row))
            rows_by_signature.setdefault(signature, []).append(row)
        diagnostics["prepare_ms"] += (perf_counter() - prepare_started) * 1000
        execute_started = perf_counter()
        for rows in rows_by_signature.values():
            # COPY is the normal fast-batch path.  Keep an explicit opt-out for
            # driver/cartridge environments that need the Core executemany
            # fallback while avoiding a deployment-specific performance switch.
            use_copy = os.getenv("TRICYCLE_FAST_COPY", "1") != "0" and len(rows) >= 100
            if use_copy and _copy_compatible(columns):
                dialect = session.get_bind().dialect
                bind_rows: list[tuple[Any, ...]] = []
                signature_columns = tuple(
                    column for column in columns if column.key in rows[0]
                )
                processors = tuple(
                    column.type._cached_bind_processor(dialect)
                    for column in signature_columns
                )
                for row in rows:
                    bind_rows.append(
                        tuple(
                            processor(row[column.key]) if processor else row[column.key]
                            for processor, column in zip(
                                processors,
                                signature_columns,
                                strict=True,
                            )
                        )
                    )
                preparer = dialect.identifier_preparer
                table_sql = preparer.format_table(mapper.local_table)
                column_sql = ", ".join(
                    preparer.quote(column.key) for column in signature_columns
                )
                copy_sql = f"COPY {table_sql} ({column_sql}) FROM STDIN"
                driver_connection = session.connection().connection.driver_connection
                await_only(_copy_rows_to_postgresql(driver_connection, copy_sql, bind_rows))
            else:
                # Core executemany remains the fallback for RDKit cartridge
                # columns and environments that do not opt into COPY.
                session.execute(
                    insert(mapper.local_table).execution_options(
                        insertmanyvalues=True,
                        insertmanyvalues_page_size=500,
                    ),
                    rows,
                )
        diagnostics["execute_ms"] += (perf_counter() - execute_started) * 1000

    # Core INSERT deliberately bypasses the unit-of-work. Keep objects as
    # detached identity holders: every scalar row is already inserted and all
    # downstream batch code uses client-side UUIDs plus eagerly populated
    # relationships. Re-attaching tens of thousands of rows to the ORM
    # identity map here adds substantial bookkeeping without adding safety.
    for entity in transient_entities:
        make_transient_to_detached(entity)


def _flush_new_entity(session: Session, entity: object, *, label: str) -> None:
    fast_insert = session.info.get("tricycle_fast_insert", False)
    _prepare_new_entity(session, entity, attach=not fast_insert)
    if fast_insert:
        session.info.setdefault("_fast_pending_entities", []).append(entity)
    # Defer new revision-local children so SQLAlchemy can batch them by table.
    if not fast_insert:
        session.flush()
    _require_id(entity, label=label)


def _flush_shared_entity(
    session: Session,
    entity: object,
    *,
    label: str,
    defer_if_fast: bool = False,
) -> None:
    """Persist one shared identity, optionally deferring fast-batch I/O.

    Fast ingestion assigns client-side UUIDs and foreign keys before adding
    rows.  Callers that have already resolved identities in a batch can defer
    the flush until the batch boundary, allowing SQLAlchemy to use one
    executemany per table.  The default preserves the immediate-flush
    behavior required by idempotent single-entity paths.
    """

    fast_insert = session.info.get("tricycle_fast_insert", False)
    _prepare_new_entity(session, entity, attach=not fast_insert)
    if fast_insert and defer_if_fast:
        session.info.setdefault("_fast_pending_entities", []).append(entity)
    elif fast_insert:
        session.add(entity)
    if not (defer_if_fast and fast_insert):
        session.flush([entity] if session.info.get("tricycle_fast_insert", False) else None)
    _require_id(entity, label=label)


__all__ = [
    "_acquire_identity_locks",
    "_attach_pending_entities",
    "_bulk_insert_pending_entities",
    "_assert_record_matches",
    "_fast_insert_enabled",
    "_new_entity",
    "_flush_new_entity",
    "_flush_shared_entity",
    "_identity_lock_id",
    "_require_id",
    "_uuid7",
]
