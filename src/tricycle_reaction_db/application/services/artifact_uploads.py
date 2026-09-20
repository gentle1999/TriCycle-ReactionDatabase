"""Public facade for artifact storage, ingestion, and TS reaction inference.

The transport-facing API remains here for compatibility.  Shared upload types,
bounded source validation, and MolOP/MolGR endpoint inference live in focused
modules so the orchestration code does not own every concern.
"""

from __future__ import annotations

import asyncio
import copy
import gzip
import json
import logging
import multiprocessing
import os
import tempfile
import threading
import zlib
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, MutableMapping, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from queue import Queue
from time import perf_counter, sleep
from typing import Any, cast
from uuid import UUID, uuid4

import numpy as np
from molop import AutoFileParser
from molop.config import molopconfig
from molop.io.base_models.ChemFileFrame import BaseCalcFrame
from molop.io.base_models.Molecule import reconstruct_topologies_batch
from rdkit import Chem
from sqlalchemy import case, text, update
from sqlalchemy import cast as sa_cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlalchemy.orm import joinedload
from sqlalchemy.orm.attributes import set_committed_value
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.dtos import (
    ArtifactBatchUploadItem,
    ArtifactBatchUploadResult,
    ArtifactFileRecord,
    ArtifactUploadResult,
    ArtifactValidationInferenceView,
    ArtifactValidationResult,
    CreateReactionCommand,
    NormalizedTopologyRecord,
    TransitionStateInferenceView,
)
from tricycle_reaction_db.application.services._persistence import (
    LEGACY_BULK_IMPORT_SESSION_INFO_KEY,
    _acquire_identity_locks,
    _attach_or_reuse_entity,
    _attach_pending_entities,
    _fast_insert_enabled,
    _fast_pending_entity_count,
    _flush_if_needed,
    _flush_new_entity,
    _new_entity,
    _prepare_new_entity,
    _require_id,
    _set_fast_pending_entities,
    _truncate_fast_pending_entities,
)
from tricycle_reaction_db.application.services.artifact_content import (
    detect_artifact_media_type,
)
from tricycle_reaction_db.application.services.artifact_molop_inference import (
    infer_endpoint_stereochemistry_from_3d as _infer_endpoint_stereochemistry_from_3d_impl,
)
from tricycle_reaction_db.application.services.artifact_molop_inference import (
    infer_ts_frame as _infer_ts_frame_impl,
)
from tricycle_reaction_db.application.services.artifact_molop_inference import (
    mapped_reaction_smiles as _mapped_reaction_smiles_impl,
)
from tricycle_reaction_db.application.services.artifact_molop_inference import (
    signed_ts_endpoints as _signed_ts_endpoints_impl,
)
from tricycle_reaction_db.application.services.artifact_parse_replacement import (
    ParseCleanupSummary,
    clear_previous_parse_results_batch,
)
from tricycle_reaction_db.application.services.artifact_upload_types import (
    ArtifactUploadConflictError,
    ArtifactUploadError,
    ArtifactUploadLimitError,
    ArtifactUploadPayload,
    MolOPFileParseTimeoutError,
    NoCalculationFramesError,
    ParsedArtifactTask,
    _DeferredArtifactInferences,
    _FailedInference,
    _Inference,
    _InferencePersistenceTask,
    _IngestionCompletion,
    _InspectedUploadSource,
    _ParsedArtifact,
    _PreparedCalculationUpload,
    _ProcessedFrame,
    _RetiredArtifactReservation,
    _SuccessfulInference,
)
from tricycle_reaction_db.application.services.artifact_upload_validation import (
    inspect_upload_source as _inspect_upload_source_impl,
)
from tricycle_reaction_db.application.services.artifact_upload_validation import (
    parser_payload as _parser_payload_impl,
)
from tricycle_reaction_db.application.services.artifact_upload_validation import (
    require_batch_upload_budget as _require_batch_upload_budget_impl,
)
from tricycle_reaction_db.application.services.artifact_upload_validation import (
    require_upload_size as _require_upload_size_impl,
)
from tricycle_reaction_db.application.services.artifact_upload_validation import (
    safe_parser_suffix as _safe_parser_suffix,
)
from tricycle_reaction_db.application.services.authorization import (
    AuthorizationService,
    ProjectPermission,
)
from tricycle_reaction_db.application.services.database_statistics import (
    refresh_project_statistics,
)
from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence import (
    mark_mapped_reactions_thermodynamics_dirty,
    refresh_mapped_reactions_thermodynamics,
)
from tricycle_reaction_db.application.services.molecular_geometry import (
    GeometryAssignmentAmbiguityError,
    GeometryPersistenceContext,
    persist_molecular_topology,
    preload_molecular_geometry_context,
)
from tricycle_reaction_db.application.services.molop_artifact_ingestion import (
    _revision_record_hash,
    persist_molop_calculation_artifact,
    reconcile_molop_geometry_context,
)
from tricycle_reaction_db.application.services.reaction_commands import (
    create_reaction_in_session,
)
from tricycle_reaction_db.application.services.reaction_geometry_reconciliation import (
    ReconciliationBatchCache,
    bind_transition_state_frame,
    ensure_transition_state_path,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.core.units import CM_INVERSE, magnitude_in
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    ArtifactIngestion,
    CalculationFrame,
    MappedReaction,
    ParseRevision,
    TransitionStateEndpoint,
    TransitionStateInference,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactKind,
    ArtifactVisibility,
    MappedReactionKind,
    ParseCompleteness,
    StorageStatus,
    TransitionStateEndpointDirection,
    TransitionStateInferenceStatus,
)
from tricycle_reaction_db.domain.reaction_frames import is_transition_state_frame_eligible
from tricycle_reaction_db.ingestion import (
    MolOPFrameRecords,
    StereoProjectionError,
    configure_molecular_graph_reconstruction,
    frame_records_from_molop,
    normalize_topology,
    normalize_topology_with_mapping,
    serialize_molecule_smiles,
)
from tricycle_reaction_db.ingestion.manifest import normalize_relative_path
from tricycle_reaction_db.storage.rustfs import (
    RustFSObjectStore,
    RustFSSettings,
    time_partitioned_content_addressed_key_for_sha256,
)


@dataclass(frozen=True, slots=True)
class _PreparedEndpointTopology:
    record: NormalizedTopologyRecord
    source_to_topology_atom_indices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _PreparedInferenceTopologyRecords:
    negative_endpoint: _PreparedEndpointTopology
    positive_endpoint: _PreparedEndpointTopology
    participant_records: tuple[NormalizedTopologyRecord, ...]

    @property
    def all_records(self) -> tuple[NormalizedTopologyRecord, ...]:
        return (
            self.negative_endpoint.record,
            self.positive_endpoint.record,
            *self.participant_records,
        )


MOLOP_VERSION = version("molop")
logger = logging.getLogger(__name__)
# Keep the parser claim window independent from the database write window. A
# file can contain many frames and one 32-file transaction was large enough to
# hold hundreds of identity locks for tens of seconds. Eight files is the
# normal hand-off/commit stack; the frame ceiling below prevents eight large
# multi-frame files from recreating the same long transaction.
# Defaults for callers that do not supply worker settings.  The durable
# worker resolves the same limits from Settings so deployments can tune the
# commit boundary without changing parser admission.
PERSISTENCE_PRELOAD_BATCH_SIZE = 16
PERSISTENCE_BATCH_FRAME_LIMIT = 256
# MolGR reconstruction is CPU-heavy and each frame crosses a process boundary.
# Larger chunks amortize pickle/future overhead while retaining enough tasks to
# keep all configured workers busy across a multi-file batch.
FRAME_CONVERSION_CHUNK_SIZE = 256
INFERENCE_PERSIST_BATCH_SIZE = 16
# The configured timeout covers the first 10 MiB. Above that, each additional
# 10 MiB gets a configurable allowance. This gives large files headroom without
# making a small malformed file hold a parser slot for an unbounded period.
MOLOP_PARSE_TIMEOUT_REFERENCE_BYTES = 10 * 1024 * 1024
GZIP_MAGIC = b"\x1f\x8b"
RUSTFS_PARSE_DOWNLOAD_CHUNK_SIZE = 4 * 1024 * 1024


def _is_no_calculation_frames_message(message: str | None) -> bool:
    """Recognize MolOP's parser-level no-frame outcome as a filter result."""

    if not message:
        return False
    normalized = " ".join(message.lower().split())
    return "reader returned a file model with no frames" in normalized


def _parser_error_from_worker(message: str | None) -> ArtifactUploadError:
    """Convert an isolated parser result into the correct durable outcome."""

    normalized_message = message or "MolOP did not return a result for this input file"
    if _is_no_calculation_frames_message(normalized_message):
        return NoCalculationFramesError(
            "source contains no QM calculation frames; artifact was filtered"
        )
    return ArtifactUploadError(normalized_message)


def _require_prepared_ingestion_id(reservation: _PreparedCalculationUpload) -> UUID:
    if reservation.ingestion_id is None:
        raise RuntimeError("calculation upload is missing its ingestion reservation")
    return reservation.ingestion_id


_molop_process_pool: ProcessPoolExecutor | None = None
_molop_process_pool_workers: int | None = None
_molop_process_pool_pid: int | None = None
_molop_process_pool_lock = threading.Lock()
_storage_process_pool: ProcessPoolExecutor | None = None
_storage_process_pool_workers: int | None = None
_storage_process_pool_pid: int | None = None
_storage_process_pool_lock = threading.Lock()
_file_worker_slots: tuple[asyncio.AbstractEventLoop, int, asyncio.Semaphore] | None = None
_frame_worker_slots: tuple[asyncio.AbstractEventLoop, int, asyncio.Semaphore] | None = None
_rustfs_download_slots: tuple[asyncio.AbstractEventLoop, int, asyncio.Semaphore] | None = None

# A storage-pool child handles many files over its lifetime. Recreating a
# boto3 client and performing a bucket HEAD for every file adds a large fixed
# latency to small and medium calculation outputs. Keep one client per child
# and initialize the bucket lazily on its first task.
_storage_worker_store: RustFSObjectStore | None = None
_storage_worker_store_key: tuple[Any, ...] | None = None
_storage_worker_bucket_ready = False


def _initialize_molop_process_worker() -> None:
    """Configure MolGR once when a shared MolOP child starts."""

    configure_molecular_graph_reconstruction()
    molopconfig.prewarm_topologies = False


def _frame_file_index(frame: Any, fallback_index: int) -> int:
    value = getattr(frame, "file_frame_index", None)
    return fallback_index if value is None else int(value)


def _frame_failure_diagnostic(
    *,
    file_frame_index: int,
    error: Exception,
    stage: str,
    segment_index: int | None = None,
    error_code: str | None = None,
    error_type: str | None = None,
    error_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    explicit_code = getattr(error, "error_code", None)
    code = error_code or (
        explicit_code
        if isinstance(explicit_code, str) and explicit_code
        else {
            "conversion": "frame_conversion_failed",
            "inference": "ts_inference_failed",
            "persistence": "frame_persistence_failed",
        }.get(stage, "frame_processing_failed")
    )
    if error_metadata is None:
        evidence = getattr(error, "evidence", None)
        if callable(evidence):
            candidate = evidence()
            if isinstance(candidate, dict):
                error_metadata = candidate
    diagnostic = {
        "code": code,
        "stage": stage,
        "file_frame_index": file_frame_index,
        "error_type": error_type or type(error).__name__,
        "message": str(error) or type(error).__name__,
    }
    if segment_index is not None:
        diagnostic["segment_index"] = segment_index
    if error_metadata:
        diagnostic["metadata"] = error_metadata
    return diagnostic


def _parse_failure_metadata(
    error: Exception,
    *,
    parsed: _ParsedArtifact | None = None,
) -> dict[str, Any]:
    """Build a compact, queryable file-level failure summary."""

    metadata: dict[str, Any] = {}
    evidence = getattr(error, "evidence", None)
    if callable(evidence):
        candidate = evidence()
        if isinstance(candidate, dict):
            metadata.update(candidate)
    if parsed is not None:
        diagnostics = list(parsed.parse_diagnostics)
        counts = Counter(str(item.get("code", "unknown")) for item in diagnostics)
        metadata.update(
            {
                "source_frame_count": parsed.source_frame_count,
                "persisted_frame_count": 0,
                "diagnostic_counts": dict(sorted(counts.items())),
                "diagnostic_samples": diagnostics[:8],
            }
        )
    metadata.setdefault("failure_stage", "artifact_processing")
    metadata.setdefault("error_type", type(error).__name__)
    metadata.setdefault("message", str(error) or type(error).__name__)
    return metadata


def _frame_records_with_diagnostics(
    chem_file: Any,
) -> tuple[tuple[MolOPFrameRecords, ...], tuple[dict[str, Any], ...]]:
    """Convert every frame while retaining failures as file diagnostics."""

    diagnostics: list[dict[str, Any]] = []
    records: list[MolOPFrameRecords] = []
    for index, frame in enumerate(chem_file):
        file_frame_index = _frame_file_index(frame, index)
        try:
            records.append(
                frame_records_from_molop(
                    frame,
                    export_schema_version=chem_file.schema_version,
                    fallback_index=index,
                )
            )
        except Exception as error:
            diagnostics.append(
                _frame_failure_diagnostic(
                    file_frame_index=file_frame_index,
                    error=error,
                    stage="conversion",
                    segment_index=int(getattr(frame, "segment_index", 0) or 0),
                )
            )
    return tuple(records), tuple(diagnostics)


async def _await_cancellation_safe(operation: Awaitable[Any]) -> Any:
    """Wait for an external operation to finish before propagating cancellation."""

    operation_task = asyncio.ensure_future(operation)
    try:
        return await asyncio.shield(operation_task)
    except asyncio.CancelledError:
        current_task = asyncio.current_task()
        if current_task is not None:
            current_task.uncancel()
        with suppress(BaseException):
            await operation_task
        raise


def _resolve_molop_process_workers(n_jobs: int) -> int:
    return max(1, (os.cpu_count() or 1) if n_jobs == -1 else n_jobs)


def molop_process_worker_count() -> int:
    """Return the effective shared MolOP process count for this service."""

    return _resolve_molop_process_workers(get_settings().molop_batch_n_jobs)


def _frame_submission_limit() -> int:
    workers = _resolve_molop_process_workers(get_settings().molop_batch_n_jobs)
    return max(1, workers * 2)


def _file_worker_submission_slots() -> asyncio.Semaphore:
    """Share the file-worker limit across concurrent upload requests."""

    global _file_worker_slots
    loop = asyncio.get_running_loop()
    workers = _resolve_molop_process_workers(get_settings().molop_batch_n_jobs)
    if (
        _file_worker_slots is None
        or _file_worker_slots[0] is not loop
        or _file_worker_slots[1] != workers
    ):
        _file_worker_slots = (loop, workers, asyncio.Semaphore(workers))
    return _file_worker_slots[2]


def _frame_worker_submission_slots() -> asyncio.Semaphore:
    """Share frame-conversion submissions across all upload requests."""

    global _frame_worker_slots
    loop = asyncio.get_running_loop()
    workers = _frame_submission_limit()
    if (
        _frame_worker_slots is None
        or _frame_worker_slots[0] is not loop
        or _frame_worker_slots[1] != workers
    ):
        _frame_worker_slots = (loop, workers, asyncio.Semaphore(workers))
    return _frame_worker_slots[2]


def _rustfs_download_submission_slots() -> asyncio.Semaphore:
    """Share RustFS download admission across all concurrent worker groups."""

    global _rustfs_download_slots
    loop = asyncio.get_running_loop()
    workers = max(1, get_settings().upload_max_concurrency)
    if (
        _rustfs_download_slots is None
        or _rustfs_download_slots[0] is not loop
        or _rustfs_download_slots[1] != workers
    ):
        _rustfs_download_slots = (loop, workers, asyncio.Semaphore(workers))
    return _rustfs_download_slots[2]


def _fast_molop_ingestion_enabled() -> bool:
    """Return whether deferred MolGR work and batched frame writes are enabled.

    Source-evidence capture changes the fields emitted by MolOP, not the
    identity or dependency guarantees of the revision-local rows.  It must
    therefore not disable the set-based persistence path; doing so silently
    turns an evidence-rich upload into one ORM flush per row.
    """

    settings = get_settings()
    return settings.molop_parallel_frame_persistence


def _parsed_artifact_requires_isolated_frame_persistence(
    parsed: _ParsedArtifact,
) -> bool:
    """Return whether a known persistence error requires a real savepoint.

    MolOP parse/completeness diagnostics do not mean that the corresponding
    frame rows are unsafe to write.  Routing every partial source through the
    regular per-frame ORM path made a file with one bad frame discard the
    batch writer for all of its valid sibling frames.  The deferred writer
    already checkpoints its pending rows around each frame, so parser and
    inference diagnostics can use the fast path and still retain the valid
    records.

    Only an explicit diagnostic from the persistence stage is evidence that a
    final bulk flush might need an isolated database boundary.  Such a
    diagnostic is normally produced by a retry/recovery caller rather than by
    MolOP itself, but keeping the escape hatch makes the safety rule explicit.
    """

    diagnostics = (*parsed.parse_diagnostics,)
    diagnostics += tuple(
        diagnostic
        for record in parsed.frame_records
        for diagnostic in record.frame.parse_diagnostics
    )
    return any(diagnostic.get("stage") == "persistence" for diagnostic in diagnostics)


def _get_molop_process_pool(n_jobs: int) -> ProcessPoolExecutor:
    """Return this API worker's reusable, spawn-safe MolOP process pool."""

    global _molop_process_pool, _molop_process_pool_pid, _molop_process_pool_workers
    workers = _resolve_molop_process_workers(n_jobs)
    pid = os.getpid()
    previous_pool: ProcessPoolExecutor | None = None
    with _molop_process_pool_lock:
        if (
            _molop_process_pool is not None
            and _molop_process_pool_workers == workers
            and _molop_process_pool_pid == pid
        ):
            return _molop_process_pool
        previous_pool = _molop_process_pool
        _molop_process_pool = ProcessPoolExecutor(
            max_workers=workers,
            mp_context=multiprocessing.get_context("spawn"),
            max_tasks_per_child=100,
            initializer=_initialize_molop_process_worker,
        )
        _molop_process_pool_workers = workers
        _molop_process_pool_pid = pid
        process_pool = _molop_process_pool
    if previous_pool is not None:
        previous_pool.shutdown(wait=True, cancel_futures=True)
    return process_pool


def _warm_molop_process_worker(delay_seconds: float = 0.25) -> int:
    """Return the child PID after the shared MolOP initializer has run.

    The short delay is intentional: a zero-work warmup can be consumed by the
    first child before ``ProcessPoolExecutor`` has finished spawning the rest
    of the pool, so it does not actually remove the cold-start penalty from
    the other workers.
    """

    sleep(delay_seconds)
    return os.getpid()


async def warm_molop_process_pool() -> int:
    """Start every shared MolOP child before the first upload reaches it.

    ``ProcessPoolExecutor`` starts children lazily.  Without an explicit warm
    boundary, the first small upload pays the import/configuration cost once
    per child while its files appear to be parsing serially.  Submit one
    harmless task per configured worker so the durable upload worker reaches a
    steady-state pool before claiming user work.
    """

    workers = molop_process_worker_count()
    pool = _get_molop_process_pool(get_settings().molop_batch_n_jobs)
    loop = asyncio.get_running_loop()
    warmed: set[int] = set()
    for _ in range(3):
        warmup = asyncio.gather(
            *(loop.run_in_executor(pool, _warm_molop_process_worker) for _ in range(workers))
        )
        results = await _await_cancellation_safe(warmup)
        warmed.update(int(pid) for pid in results)
        if len(warmed) >= workers:
            break
    return len(warmed)


def _get_storage_process_pool(n_jobs: int) -> ProcessPoolExecutor:
    """Return the reusable process pool for RustFS upload and HEAD validation."""

    global _storage_process_pool, _storage_process_pool_workers, _storage_process_pool_pid
    workers = _resolve_molop_process_workers(n_jobs)
    pid = os.getpid()
    previous_pool: ProcessPoolExecutor | None = None
    with _storage_process_pool_lock:
        if (
            _storage_process_pool is not None
            and _storage_process_pool_workers == workers
            and _storage_process_pool_pid == pid
        ):
            return _storage_process_pool
        previous_pool = _storage_process_pool
        _storage_process_pool = ProcessPoolExecutor(
            max_workers=workers,
            mp_context=multiprocessing.get_context("spawn"),
        )
        _storage_process_pool_workers = workers
        _storage_process_pool_pid = pid
        process_pool = _storage_process_pool
    if previous_pool is not None:
        previous_pool.shutdown(wait=True, cancel_futures=True)
    return process_pool


def _warm_storage_process_worker(
    settings: RustFSSettings,
    delay_seconds: float = 0.25,
) -> int:
    """Initialize one RustFS child client before user work reaches the pool."""

    _storage_worker_store_for(settings)
    sleep(delay_seconds)
    return os.getpid()


async def warm_storage_process_pool() -> int:
    """Start and initialize every shared RustFS storage child."""

    workers = max(1, get_settings().upload_max_concurrency)
    pool = _get_storage_process_pool(workers)
    settings = RustFSSettings()
    loop = asyncio.get_running_loop()
    warmed: set[int] = set()
    for _ in range(3):
        warmup = asyncio.gather(
            *(
                loop.run_in_executor(pool, _warm_storage_process_worker, settings)
                for _ in range(workers)
            )
        )
        results = await _await_cancellation_safe(warmup)
        warmed.update(int(pid) for pid in results)
        if len(warmed) >= workers:
            break
    return len(warmed)


async def close_molop_process_pool() -> None:
    """Release parser workers during ASGI shutdown."""

    await asyncio.to_thread(_shutdown_molop_process_pool_sync)
    await asyncio.to_thread(_shutdown_upload_stage_pools_sync)


def _shutdown_molop_process_pool_sync() -> None:
    """Stop parser workers before entering MolGR's native boundary."""

    global _molop_process_pool, _molop_process_pool_pid, _molop_process_pool_workers
    with _molop_process_pool_lock:
        pool = _molop_process_pool
        _molop_process_pool = None
        _molop_process_pool_workers = None
        _molop_process_pool_pid = None
    if pool is not None:
        pool.shutdown(wait=True, cancel_futures=False)


def _shutdown_upload_stage_pools_sync() -> None:
    """Stop the RustFS storage pool owned by this API worker."""

    global _storage_process_pool, _storage_process_pool_workers, _storage_process_pool_pid
    with _storage_process_pool_lock:
        storage_pool = _storage_process_pool
        _storage_process_pool = None
        _storage_process_pool_workers = None
        _storage_process_pool_pid = None
    if storage_pool is not None:
        storage_pool.shutdown(wait=True, cancel_futures=False)


async def _run_molop_source_parser(
    source: bytes | Path,
    filename: str,
    *,
    artifact_sha256: str | None = None,
) -> _ParsedArtifact:
    """Parse one source asynchronously in the shared MolOP process pool.

    ``AutoFileParser`` is synchronous and CPU-bound.  The awaitable boundary is
    deliberately around the process-pool future, so RustFS, parsing, and the
    database writer can make progress independently on the event loop.
    """

    with tempfile.TemporaryDirectory(prefix="tricycle-molop-file-") as directory:
        parser_path, source_compression = await asyncio.to_thread(
            _prepare_calculation_parser_path,
            source,
            filename,
            temporary_dir=Path(directory),
            input_index=0,
        )
        try:
            process_pool = _get_molop_process_pool(get_settings().molop_batch_n_jobs)
            loop = asyncio.get_running_loop()
            parsed, error_message = await loop.run_in_executor(
                process_pool,
                _parse_calculation_path_worker,
                parser_path,
                source_compression,
                artifact_sha256,
            )
            if parsed is None:
                raise _parser_error_from_worker(error_message)
            return parsed
        except Exception as error:
            if isinstance(error, ArtifactUploadError):
                raise
            raise ArtifactUploadError(str(error) or type(error).__name__) from error


async def _run_molop_file_pipeline(
    source: bytes | Path,
    filename: str,
    *,
    artifact_sha256: str | None = None,
    submission_slots: asyncio.Semaphore | None = None,
    file_slots: asyncio.Semaphore | None = None,
) -> _ParsedArtifact:
    """Parse and reconstruct one file through the shared MolOP pipeline.

    The file slot bounds submissions while the reusable process pool keeps its
    workers hot across the whole queue.
    """

    timeout_seconds = _molop_file_parse_timeout_seconds(source)
    acquired_file_slot = False
    effective_file_slots = file_slots or _file_worker_submission_slots()
    try:
        # Queue wait is intentionally outside the size-derived per-file budget:
        # a file must get a worker before its parser deadline starts.
        await effective_file_slots.acquire()
        acquired_file_slot = True
        async with asyncio.timeout(timeout_seconds):
            parsed = await _run_molop_source_parser(
                source,
                filename,
                artifact_sha256=artifact_sha256,
            )
            return await _process_parsed_artifact_frames(
                parsed,
                submission_slots=(
                    submission_slots
                    if submission_slots is not None
                    else _frame_worker_submission_slots()
                ),
            )

    except TimeoutError as error:
        raise MolOPFileParseTimeoutError(
            f"MolOP parsing/post-processing exceeded {timeout_seconds:g}s for {Path(filename).name}"
        ) from error
    finally:
        if acquired_file_slot:
            effective_file_slots.release()


def _source_size_bytes(source: bytes | Path) -> int:
    """Return the parser-input size, expanding a gzip trailer hint when available.

    Gzip's ISIZE trailer gives the uncompressed size in constant time for the
    single-member files used by the import pipeline. Clamp that hint to the
    configured decompressed upload limit so malformed trailers cannot create
    excessive deadlines.
    """

    if isinstance(source, bytes):
        compressed_size = len(source)
        is_gzip = source.startswith(GZIP_MAGIC)
        trailer = source[-4:] if is_gzip and compressed_size >= 18 else b""
    else:
        compressed_size = source.stat().st_size
        trailer = b""
        if compressed_size >= 18:
            with source.open("rb") as stream:
                is_gzip = stream.read(2) == GZIP_MAGIC
                if is_gzip:
                    stream.seek(-4, 2)
                    trailer = stream.read(4)

    if len(trailer) != 4:
        return compressed_size
    uncompressed_size = int.from_bytes(trailer, byteorder="little", signed=False)
    if uncompressed_size == 0:
        return compressed_size
    return min(max(compressed_size, uncompressed_size), get_settings().max_upload_bytes)


def _molop_file_parse_timeout_seconds(source: bytes | Path) -> float:
    """Budget parsing from source size, with extra headroom above 10 MiB.

    The configured baseline covers the first 10 MiB. Each additional 10 MiB
    adds ``baseline * size_multiplier`` seconds; for the defaults, 20 MiB gets
    150 seconds and 30 MiB gets 240 seconds.
    """

    settings = get_settings()
    size_scale = _source_size_bytes(source) / MOLOP_PARSE_TIMEOUT_REFERENCE_BYTES
    extra_size_scale = max(0.0, size_scale - 1.0)
    return settings.molop_file_parse_timeout_seconds * (
        1.0 + extra_size_scale * settings.molop_file_parse_timeout_size_multiplier
    )


def _recover_aborted_batch_sync(
    session: Session,
    *,
    prepared: Mapping[int, _PreparedCalculationUpload],
    stored: Mapping[int, Any],
    error: BaseException,
    completed_at: datetime,
) -> None:
    """Close pending rows after a batch abort outside the failed transaction.

    ``_prepare_upload_batch`` commits reservations before storage and parsing
    begin.  If the later persistence transaction is cancelled or fails, its
    rollback cannot touch those already-committed rows.  Reconcile only the
    reservations owned by this request, under the same content locks used by
    upload and GC. Objects whose storage result is known to be valid are kept;
    unknown/pending objects remain eligible for the normal storage GC grace
    period.
    """

    unique_by_artifact_id = {
        reservation.artifact_id: reservation for reservation in prepared.values()
    }
    if not unique_by_artifact_id:
        return
    artifact_ids = sorted(unique_by_artifact_id)
    _acquire_identity_locks(
        session,
        *(
            ("artifact-content", unique_by_artifact_id[artifact_id].content_sha256)
            for artifact_id in artifact_ids
        ),
    )
    artifacts = {
        artifact.id: artifact
        for artifact in session.exec(
            select(ArtifactFile).where(col(ArtifactFile.id).in_(artifact_ids))
        ).all()
        if artifact.id is not None
    }
    ingestion_ids = [
        reservation.ingestion_id
        for reservation in unique_by_artifact_id.values()
        if reservation.ingestion_id is not None
    ]
    ingestions = {
        ingestion.id: ingestion
        for ingestion in session.exec(
            select(ArtifactIngestion).where(col(ArtifactIngestion.id).in_(ingestion_ids))
        ).all()
        if ingestion.id is not None
    }
    stored_by_artifact_id = {
        prepared[index].artifact_id: value for index, value in stored.items() if index in prepared
    }
    message = str(error) or type(error).__name__
    if isinstance(error, asyncio.CancelledError):
        error_code = "artifact_batch_cancelled"
        message = "upload batch was cancelled before persistence completed"
    else:
        error_code = "artifact_batch_failed"
    recovery_error = ArtifactUploadError(message)
    for artifact_id, reservation in unique_by_artifact_id.items():
        artifact = artifacts.get(artifact_id)
        if artifact is not None and artifact.storage_status is StorageStatus.PENDING:
            stored_result = stored_by_artifact_id.get(artifact_id)
            if (
                stored_result is not None
                and artifact.object_key == reservation.object_key
                and stored_result.size == reservation.size_bytes
                and stored_result.sha256 == reservation.content_sha256
            ):
                artifact.storage_status = StorageStatus.AVAILABLE
                artifact.version_id = stored_result.version_id
                artifact.etag = stored_result.etag
                artifact.storage_verified_at = stored_result.last_modified
                session.add(artifact)
        ingestion_id = reservation.ingestion_id
        ingestion = ingestions.get(ingestion_id) if ingestion_id is not None else None
        if ingestion is not None and ingestion.status in {
            ArtifactIngestionStatus.PENDING,
            ArtifactIngestionStatus.PROCESSING,
        }:
            resolved_ingestion_id = _require_id(ingestion, label="ArtifactIngestion")
            _mark_ingestion_failed(
                session,
                ingestion_id=resolved_ingestion_id,
                error=recovery_error,
                error_code=error_code,
                completed_at=completed_at,
                ingestion=ingestion,
            )


async def _recover_aborted_batch(
    *,
    prepared: Mapping[int, _PreparedCalculationUpload],
    stored: Mapping[int, Any],
    error: BaseException,
) -> None:
    """Best-effort durable recovery for an aborted prepared batch."""

    try:
        async with session_factory() as session:
            await session.run_sync(
                cast(
                    Any,
                    partial(
                        _recover_aborted_batch_sync,
                        prepared=prepared,
                        stored=stored,
                        error=error,
                        completed_at=datetime.now(UTC),
                    ),
                )
            )
            await session.commit()
    except Exception:
        # Recovery must never mask the original parser/database exception. A
        # stale pending row remains protected by the storage GC grace period.
        logger.exception("failed to recover aborted artifact upload batch")


@asynccontextmanager
async def _pipeline_task_lifecycle(
    tasks: list[asyncio.Task[Any]],
    *,
    on_abort: Callable[[BaseException], Awaitable[None]] | None = None,
) -> AsyncIterator[None]:
    """Ensure file tasks cannot outlive a failed/cancelled batch request."""

    async def cancel_unfinished_tasks() -> None:
        unfinished = [task for task in tasks if not task.done()]
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.gather(*unfinished, return_exceptions=True)

    try:
        yield
    except BaseException as abort_error:
        # Stop parser/storage tasks before touching the committed reservations.
        # Otherwise a late storage future could race the recovery transaction.
        await cancel_unfinished_tasks()
        if on_abort is not None:

            async def run_recovery(error: BaseException = abort_error) -> None:
                await on_abort(error)

            recovery_task: asyncio.Task[None] = asyncio.create_task(run_recovery())
            try:
                await asyncio.shield(recovery_task)
            except asyncio.CancelledError:
                # The context itself may be unwinding due to cancellation;
                # finish the independent recovery task before propagating it.
                current_task = asyncio.current_task()
                if current_task is not None:
                    current_task.uncancel()
                await recovery_task
        raise
    finally:
        await cancel_unfinished_tasks()


def _require_upload_size(payload: bytes) -> None:
    _require_upload_size_impl(payload, maximum_size=get_settings().max_upload_bytes)


def _inspect_upload_source(
    file: ArtifactUploadPayload,
    *,
    maximum_size: int,
) -> _InspectedUploadSource:
    return _inspect_upload_source_impl(file, maximum_size=maximum_size)


def _require_batch_upload_budget(
    files: list[ArtifactUploadPayload],
    *,
    enforce_batch_files: bool = True,
    enforce_batch_bytes: bool = True,
) -> dict[int, _InspectedUploadSource]:
    settings = get_settings()
    return _require_batch_upload_budget_impl(
        files,
        maximum_upload_size=settings.max_upload_bytes,
        maximum_batch_files=settings.max_batch_files,
        maximum_batch_bytes=settings.max_batch_bytes,
        enforce_batch_files=enforce_batch_files,
        enforce_batch_bytes=enforce_batch_bytes,
    )


def _parser_payload(
    payload: bytes,
    filename: str,
    *,
    max_decompressed_bytes: int | None = None,
) -> tuple[bytes, str | None]:
    maximum = max_decompressed_bytes or get_settings().max_upload_bytes
    return _parser_payload_impl(payload, filename, max_decompressed_bytes=maximum)


def _mapped_reaction_smiles(reactant: Chem.Mol, product: Chem.Mol) -> str:
    return _mapped_reaction_smiles_impl(reactant, product)


def _infer_endpoint_stereochemistry_from_3d(endpoint: Chem.Mol) -> Chem.Mol:
    """Infer an endpoint's stereochemistry from its displaced 3D geometry.

    MolOP reconstructs a displaced endpoint from coordinates, so the endpoint
    itself is the authority for the pre/post-TS stereochemistry. The returned
    clone is frozen for downstream projections; writer-facing direction repair
    is deliberately performed by a separate serialization-only helper.
    """

    return _infer_endpoint_stereochemistry_from_3d_impl(endpoint)


def _signed_ts_endpoints(
    frame: BaseCalcFrame[Any],
    vibration_position: int,
) -> tuple[Chem.Mol, Chem.Mol, float, float]:
    return _signed_ts_endpoints_impl(
        frame,
        vibration_position,
        infer_endpoint_stereochemistry=_infer_endpoint_stereochemistry_from_3d,
    )


def _infer_ts_frame(frame: BaseCalcFrame[Any], fallback_index: int) -> _Inference | None:
    return _infer_ts_frame_impl(
        frame,
        fallback_index,
        signed_endpoints=_signed_ts_endpoints,
        mapped_smiles=_mapped_reaction_smiles,
    )


def _detach_frame_for_process(frame: BaseCalcFrame[Any]) -> BaseCalcFrame[Any]:
    """Break ChemFile navigation links before sending one frame over IPC."""

    detached = copy.copy(frame)
    detached._prev_frame = None
    detached._next_frame = None
    return detached


def _process_frame_without_configuration(
    frame: BaseCalcFrame[Any],
    fallback_index: int,
    schema_version: str,
) -> _ProcessedFrame:
    """Reconstruct, normalize, and validate one frame under configured MolGR."""
    file_frame_index = _frame_file_index(frame, fallback_index)
    try:
        record = frame_records_from_molop(
            frame,
            export_schema_version=schema_version,
            fallback_index=fallback_index,
        )
    except Exception as error:
        return _ProcessedFrame(
            file_frame_index=file_frame_index,
            record=None,
            inference=None,
            topology_reconstruction_status=getattr(frame, "topology_reconstruction_status", None),
            error_code=getattr(error, "error_code", "frame_conversion_failed"),
            error_message=str(error) or type(error).__name__,
            error_type=type(error).__name__,
            error_metadata=(error.evidence() if isinstance(error, StereoProjectionError) else None),
        )
    try:
        inference = _infer_ts_frame(frame, fallback_index)
    except Exception as error:
        # A broken TS displacement must not discard an otherwise valid
        # calculation frame.  It is surfaced as a diagnostic instead.
        return _ProcessedFrame(
            file_frame_index=file_frame_index,
            record=record,
            inference=None,
            topology_reconstruction_status=frame.topology_reconstruction_status,
            error_code=getattr(error, "error_code", "ts_inference_failed"),
            error_message=str(error) or type(error).__name__,
            error_type=type(error).__name__,
            error_metadata=(error.evidence() if isinstance(error, StereoProjectionError) else None),
        )
    return _ProcessedFrame(
        file_frame_index=file_frame_index,
        record=record,
        inference=inference,
        topology_reconstruction_status=frame.topology_reconstruction_status,
    )


def _frame_processing_failure(
    frame: BaseCalcFrame[Any],
    fallback_index: int,
    error: Exception,
) -> _ProcessedFrame:
    """Convert an unexpected frame boundary error into one frame diagnostic."""

    try:
        file_frame_index = _frame_file_index(frame, fallback_index)
    except Exception:
        file_frame_index = fallback_index
    return _ProcessedFrame(
        file_frame_index=file_frame_index,
        record=None,
        inference=None,
        topology_reconstruction_status=None,
        error_code=getattr(error, "error_code", "frame_conversion_failed"),
        error_message=str(error) or type(error).__name__,
        error_type=type(error).__name__,
        error_metadata=(error.evidence() if isinstance(error, StereoProjectionError) else None),
    )


def _process_frame_chunk_item(
    frame: BaseCalcFrame[Any],
    fallback_index: int,
    schema_version: str,
) -> _ProcessedFrame:
    """Keep an unexpected frame exception local to its chunk item.

    The normal conversion and inference boundaries already return a diagnostic
    for one bad frame.  This outer guard covers failures before those
    boundaries (for example a malformed MolOP object or a reconstruction
    status property that raises).  Without it, the chunk worker raises and the
    async collector has no source-level way to tell one failed frame from all
    of its otherwise valid neighbours.
    """

    try:
        return _process_frame_without_configuration(frame, fallback_index, schema_version)
    except Exception as error:
        return _frame_processing_failure(frame, fallback_index, error)


def _process_frame_worker(
    frame: BaseCalcFrame[Any],
    fallback_index: int,
    schema_version: str,
) -> _ProcessedFrame:
    """Reconstruct one frame after pool-level MolGR initialization."""

    return _process_frame_without_configuration(frame, fallback_index, schema_version)


def _process_frame_chunk_worker(
    frames: tuple[tuple[BaseCalcFrame[Any], int], ...],
    schema_version: str,
) -> tuple[_ProcessedFrame, ...]:
    """Process a frame chunk after one-time worker initialization."""

    # A frame-by-frame MolGR call pays the native boundary and topology
    # scheduler overhead once per frame.  The outer ProcessPoolExecutor is
    # already the shared CPU admission boundary, so use MolOP's native batch
    # API inside one pool task with one native worker.  This keeps the total
    # number of active CPU slots bounded by the shared process pool while
    # still letting MolGR amortize its C++ setup over the whole frame chunk.
    candidates = [
        frame
        for frame, _fallback_index in frames
        if isinstance(frame, BaseCalcFrame)
        and getattr(frame, "_rdmol", None) is None
        and not frame.bonds
        and bool(frame.atoms)
        and frame.topology_reconstruction_status is None
    ]
    if candidates:
        configure_molecular_graph_reconstruction(allow_native_parallel=True)
        try:
            results = reconstruct_topologies_batch(
                candidates,
                max_workers=1,
                queue_size=max(1, len(candidates)),
                ordered=False,
                raise_on_error=False,
                retain_results=True,
            )
        finally:
            configure_molecular_graph_reconstruction()
        if len(results) != len(candidates):
            raise RuntimeError(
                "MolOP native batch reconstruction returned an incomplete frame result set"
            )
        for frame, result in zip(candidates, results, strict=True):
            frame._apply_batch_reconstruction_result(result)

    return tuple(
        _process_frame_chunk_item(frame, fallback_index, schema_version)
        for frame, fallback_index in frames
    )


def _storage_worker_store_for(settings: RustFSSettings) -> RustFSObjectStore:
    """Return the child-local RustFS client, initializing its bucket once."""

    global _storage_worker_store, _storage_worker_store_key, _storage_worker_bucket_ready
    settings_key = (
        settings.endpoint_url,
        settings.access_key,
        settings.secret_key,
        settings.bucket,
        settings.region,
        settings.verify_tls,
        settings.ca_bundle,
        settings.connect_timeout_seconds,
        settings.read_timeout_seconds,
    )
    if _storage_worker_store is None or _storage_worker_store_key != settings_key:
        if _storage_worker_store is not None:
            with suppress(Exception):
                _storage_worker_store.close()
        _storage_worker_store = RustFSObjectStore(settings)
        _storage_worker_store_key = settings_key
        _storage_worker_bucket_ready = False
    store = _storage_worker_store
    if not _storage_worker_bucket_ready:
        store.ensure_bucket()
        _storage_worker_bucket_ready = True
    return store


def _store_payload_worker(
    settings: RustFSSettings,
    object_key: str,
    source: bytes | Path,
    media_type: str,
    content_sha256: str | None,
    size_bytes: int | None,
    check_existing_object: bool,
) -> Any:
    """Process-pool entry point for one RustFS transfer plus HEAD check."""

    store = _storage_worker_store_for(settings)
    if check_existing_object and store.exists(object_key):
        return store.head(object_key)
    if isinstance(source, Path):
        if content_sha256 is None or size_bytes is None:
            raise ValueError("streamed uploads require precomputed source identity")
        return store.put_file(
            key=object_key,
            path=source,
            content_sha256=content_sha256,
            size_bytes=size_bytes,
            content_type=media_type,
            metadata={"ingestion": "artifact-upload"},
        )
    return store.put_bytes(
        key=object_key,
        payload=source,
        content_type=media_type,
        metadata={"ingestion": "artifact-upload"},
    )


def _download_payload_worker(
    settings: RustFSSettings,
    object_key: str,
    destination: Path,
    expected_size: int,
    expected_sha256: str,
) -> tuple[int, str]:
    """Stream one staged object to the parser spool in a storage child."""

    maximum = get_settings().max_upload_bytes
    if expected_size > maximum:
        raise ArtifactUploadError(f"uploaded artifact exceeds the {maximum}-byte limit")
    digest = sha256()
    size = 0
    store = _storage_worker_store_for(settings)
    with destination.open("wb") as output:
        for chunk in store.iter_bytes(
            object_key,
            chunk_size=RUSTFS_PARSE_DOWNLOAD_CHUNK_SIZE,
        ):
            size += len(chunk)
            if size > expected_size or size > maximum:
                raise ArtifactUploadError("stored artifact bytes exceed database identity")
            digest.update(chunk)
            output.write(chunk)
    actual_sha256 = digest.hexdigest()
    if size != expected_size or actual_sha256 != expected_sha256:
        raise ArtifactUploadError("stored artifact bytes do not match database identity")
    return size, actual_sha256


def _parsed_artifact_from_chem_file(
    chem_file: Any,
    *,
    source_compression: str | None,
    artifact_sha256: str | None = None,
    materialize_topologies: bool = True,
) -> _ParsedArtifact:
    frame_records, conversion_diagnostics = (
        _frame_records_with_diagnostics(chem_file) if materialize_topologies else ((), ())
    )
    # ``frame.rdmol`` is lazy in MolOP. Materialize frame records first so
    # MolGR's reconstruction status is known before TS endpoint inference is
    # allowed to consume the topology.
    inferred: list[_Inference] = []
    if materialize_topologies:
        for fallback_index, frame in enumerate(chem_file):
            if isinstance(frame, BaseCalcFrame):
                try:
                    inference = _infer_ts_frame(frame, fallback_index)
                except Exception as error:
                    conversion_diagnostics = (
                        *conversion_diagnostics,
                        _frame_failure_diagnostic(
                            file_frame_index=_frame_file_index(frame, fallback_index),
                            error=error,
                            stage="inference",
                        ),
                    )
                    inference = None
                if inference is not None:
                    inferred.append(inference)
    return _ParsedArtifact(
        chem_file=chem_file,
        frame_records=frame_records,
        source_frame_count=len(chem_file),
        source_format=chem_file.source_format,
        source_compression=source_compression,
        inferences=tuple(inferred),
        record_sha256=(
            _revision_record_hash(artifact_sha256, chem_file, list(frame_records))
            if artifact_sha256 is not None and materialize_topologies
            else None
        ),
        artifact_sha256=artifact_sha256,
        parse_diagnostics=tuple(conversion_diagnostics),
    )


def _materialize_parsed_artifacts(
    parsed_artifacts: list[_ParsedArtifact],
) -> list[_ParsedArtifact]:
    """Process deferred frames through the dedicated frame process pool."""

    materialized: dict[int, _ParsedArtifact] = {}
    pool = _get_molop_process_pool(get_settings().molop_batch_n_jobs)
    for parsed in parsed_artifacts:
        if parsed.frame_records:
            materialized[id(parsed)] = parsed
            continue
        chem_file = parsed.chem_file
        jobs = [
            (
                fallback_index,
                frame,
                pool.submit(
                    _process_frame_worker,
                    _detach_frame_for_process(frame),
                    fallback_index,
                    str(chem_file.schema_version),
                ),
            )
            for fallback_index, frame in enumerate(chem_file)
            if isinstance(frame, BaseCalcFrame)
        ]
        processed: list[_ProcessedFrame] = []
        for fallback_index, frame, job in jobs:
            try:
                processed.append(job.result())
            except Exception as error:
                processed.append(_frame_processing_failure(frame, fallback_index, error))
        status_by_index = {
            item.file_frame_index: item.topology_reconstruction_status for item in processed
        }
        for fallback_index, frame in enumerate(chem_file):
            file_frame_index = frame.file_frame_index
            if file_frame_index is None:
                file_frame_index = fallback_index
            frame.topology_reconstruction_status = status_by_index.get(file_frame_index)
        records = tuple(
            item.record
            for item in sorted(processed, key=lambda item: item.file_frame_index)
            if item.record is not None
        )
        inferences = tuple(item.inference for item in processed if item.inference is not None)
        diagnostics = list(parsed.parse_diagnostics)
        frames_by_index = {
            _frame_file_index(frame, fallback_index): frame
            for fallback_index, frame in enumerate(chem_file)
        }
        diagnostics.extend(
            _frame_failure_diagnostic(
                file_frame_index=item.file_frame_index,
                error=ValueError(
                    item.error_message or item.error_code or "frame processing failed"
                ),
                stage=("conversion" if item.record is None else "inference"),
                segment_index=int(
                    getattr(frames_by_index.get(item.file_frame_index), "segment_index", 0) or 0
                ),
                error_code=item.error_code,
                error_type=item.error_type,
                error_metadata=item.error_metadata,
            )
            for item in processed
            if item.error_code is not None
        )
        materialized[id(parsed)] = _ParsedArtifact(
            chem_file=chem_file,
            frame_records=records,
            source_frame_count=parsed.source_frame_count,
            source_format=parsed.source_format,
            source_compression=parsed.source_compression,
            inferences=inferences,
            record_sha256=_revision_record_hash(
                parsed.artifact_sha256 or "", chem_file, list(records)
            )
            if parsed.artifact_sha256 is not None
            else None,
            artifact_sha256=parsed.artifact_sha256,
            parse_diagnostics=tuple(diagnostics),
        )
    return [materialized[id(parsed)] for parsed in parsed_artifacts]


async def _process_parsed_artifact_frames(
    parsed: _ParsedArtifact,
    *,
    submission_slots: asyncio.Semaphore,
) -> _ParsedArtifact:
    """Submit frame chunks and collect completions in source order."""

    if parsed.frame_records:
        return parsed
    chem_file = parsed.chem_file
    pool = _get_molop_process_pool(get_settings().molop_batch_n_jobs)
    loop = asyncio.get_running_loop()

    frame_inputs = tuple(
        (
            frame,
            fallback_index,
        )
        for fallback_index, frame in enumerate(chem_file)
        if isinstance(frame, BaseCalcFrame)
    )
    frame_chunks = tuple(
        frame_inputs[start : start + FRAME_CONVERSION_CHUNK_SIZE]
        for start in range(0, len(frame_inputs), FRAME_CONVERSION_CHUNK_SIZE)
    )

    async def process_frame_chunk(
        chunk: tuple[tuple[BaseCalcFrame[Any], int], ...],
    ) -> tuple[_ProcessedFrame, ...]:
        async with submission_slots:
            detached_chunk = tuple(
                (_detach_frame_for_process(frame), fallback_index)
                for frame, fallback_index in chunk
            )
            return await loop.run_in_executor(
                pool,
                _process_frame_chunk_worker,
                detached_chunk,
                str(chem_file.schema_version),
            )

    async def recover_frame_chunk(
        chunk: tuple[tuple[BaseCalcFrame[Any], int], ...],
    ) -> tuple[_ProcessedFrame, ...]:
        """Retry a failed chunk item-by-item so valid neighbours survive."""

        async def recover_frame(
            frame: BaseCalcFrame[Any],
            fallback_index: int,
        ) -> _ProcessedFrame:
            async with submission_slots:
                try:
                    return await loop.run_in_executor(
                        pool,
                        _process_frame_worker,
                        _detach_frame_for_process(frame),
                        fallback_index,
                        str(chem_file.schema_version),
                    )
                except Exception as error:
                    return _frame_processing_failure(frame, fallback_index, error)

        return tuple(
            await asyncio.gather(
                *(recover_frame(frame, fallback_index) for frame, fallback_index in chunk)
            )
        )

    gathered_chunks = await asyncio.gather(
        *(process_frame_chunk(chunk) for chunk in frame_chunks),
        return_exceptions=True,
    )
    processed: list[_ProcessedFrame] = []
    for chunk, result in zip(frame_chunks, gathered_chunks, strict=True):
        if isinstance(result, tuple):
            processed.extend(result)
            continue
        processed.extend(await recover_frame_chunk(chunk))
    status_by_index = {
        item.file_frame_index: item.topology_reconstruction_status for item in processed
    }
    for fallback_index, frame in enumerate(chem_file):
        file_frame_index = frame.file_frame_index
        if file_frame_index is None:
            file_frame_index = fallback_index
        frame.topology_reconstruction_status = status_by_index.get(file_frame_index)
    records = tuple(
        item.record
        for item in sorted(processed, key=lambda item: item.file_frame_index)
        if item.record is not None
    )
    inferences = tuple(item.inference for item in processed if item.inference is not None)
    diagnostics = list(parsed.parse_diagnostics)
    frames_by_index = {
        _frame_file_index(frame, fallback_index): frame
        for fallback_index, frame in enumerate(chem_file)
    }
    diagnostics.extend(
        _frame_failure_diagnostic(
            file_frame_index=item.file_frame_index,
            error=ValueError(item.error_message or item.error_code or "frame processing failed"),
            stage=("conversion" if item.record is None else "inference"),
            segment_index=int(
                getattr(frames_by_index.get(item.file_frame_index), "segment_index", 0) or 0
            ),
            error_code=item.error_code,
            error_type=item.error_type,
            error_metadata=item.error_metadata,
        )
        for item in processed
        if item.error_code is not None
    )
    return _ParsedArtifact(
        chem_file=chem_file,
        frame_records=records,
        source_frame_count=parsed.source_frame_count,
        source_format=parsed.source_format,
        source_compression=parsed.source_compression,
        inferences=inferences,
        record_sha256=_revision_record_hash(parsed.artifact_sha256 or "", chem_file, list(records))
        if parsed.artifact_sha256 is not None
        else None,
        artifact_sha256=parsed.artifact_sha256,
        parse_diagnostics=tuple(diagnostics),
    )


def _parse_calculation_path_worker(
    path: str,
    source_compression: str | None,
    artifact_sha256: str | None = None,
) -> tuple[_ParsedArtifact | None, str | None]:
    """Parse one source file in a worker without entering MolGR."""

    previous_prewarm = molopconfig.prewarm_topologies
    try:
        configure_molecular_graph_reconstruction()
        molopconfig.prewarm_topologies = False
        chem_file = AutoFileParser(
            path,
            parser_detection="auto",
            # Segment boundaries and frame locators are part of the durable
            # calculation identity.  Every ingestion route uses the same
            # evidence-complete parser contract.
            capture_source_evidence=True,
            release_file_content=True,
        )
        return (
            _parsed_artifact_from_chem_file(
                chem_file,
                source_compression=source_compression,
                artifact_sha256=artifact_sha256,
                materialize_topologies=False,
            ),
            None,
        )
    except Exception as error:
        return None, str(error) or type(error).__name__
    finally:
        molopconfig.prewarm_topologies = previous_prewarm
        configure_molecular_graph_reconstruction()


def _parse_calculation_paths_parallel(
    paths: list[str],
    compressions: list[str | None],
    *,
    n_jobs: int,
    on_result: Callable[[int, tuple[_ParsedArtifact | None, str | None]], None] | None = None,
) -> list[tuple[_ParsedArtifact | None, str | None]]:
    process_pool = _get_molop_process_pool(n_jobs)
    if on_result is None:
        return list(
            process_pool.map(
                _parse_calculation_path_worker,
                paths,
                compressions,
                chunksize=1,
            )
        )

    futures = {
        process_pool.submit(_parse_calculation_path_worker, path, compression): index
        for index, (path, compression) in enumerate(zip(paths, compressions, strict=True))
    }
    results: list[tuple[_ParsedArtifact | None, str | None] | None] = [None] * len(paths)
    for future in as_completed(futures):
        index = futures[future]
        try:
            result = future.result()
        except Exception as error:  # pragma: no cover - worker normally isolates failures
            result = (None, str(error) or type(error).__name__)
        results[index] = result
        on_result(index, result)
    return [result for result in results if result is not None]


def _parse_calculation_output(payload: bytes, filename: str) -> _ParsedArtifact:
    """Parse an in-memory payload with full evidence for validation callers."""
    configure_molecular_graph_reconstruction()
    decoded_payload, source_compression = _parser_payload(payload, filename)
    with tempfile.NamedTemporaryFile(suffix=_safe_parser_suffix(filename)) as temporary:
        temporary.write(decoded_payload)
        temporary.flush()
        chem_file = AutoFileParser(
            temporary.name,
            parser_detection="auto",
            capture_source_evidence=True,
            release_file_content=True,
        )
        return _parsed_artifact_from_chem_file(
            chem_file,
            source_compression=source_compression,
        )


def infer_transition_states_from_calculation_output(
    payload: bytes,
    filename: str,
) -> tuple[_Inference, ...]:
    """Re-evaluate TS endpoints without rebuilding unrelated frame topologies.

    Offline endpoint maintenance already has immutable CalculationFrame rows;
    it needs only the source-order coordinates, vibrations, and MolGR graphs
    of the two displaced endpoints. Reconstructing every optimization frame
    again would add cost without contributing evidence to the re-inference.
    """

    configure_molecular_graph_reconstruction()
    decoded_payload, _source_compression = _parser_payload(payload, filename)
    with tempfile.NamedTemporaryFile(suffix=_safe_parser_suffix(filename)) as temporary:
        temporary.write(decoded_payload)
        temporary.flush()
        chem_file = AutoFileParser(
            temporary.name,
            parser_detection="auto",
            capture_source_evidence=True,
            release_file_content=True,
        )
        inferred: list[_Inference] = []
        for fallback_index, frame in enumerate(chem_file):
            if isinstance(frame, BaseCalcFrame):
                inference = _infer_ts_frame(frame, fallback_index)
                if inference is not None:
                    inferred.append(inference)
        return tuple(inferred)


def _prepare_calculation_parser_path(
    source: bytes | Path,
    filename: str,
    *,
    temporary_dir: Path,
    input_index: int,
) -> tuple[str, str | None]:
    """Give MolOP a file path while retaining spooled raw files in place."""

    if not isinstance(source, Path):
        decoded_payload, source_compression = _parser_payload(source, filename)
        path = temporary_dir / f"{input_index:08d}{_safe_parser_suffix(filename)}"
        path.write_bytes(decoded_payload)
        return str(path), source_compression

    maximum = get_settings().max_upload_bytes
    if source.stat().st_size > maximum:
        raise ArtifactUploadError(f"uploaded artifact exceeds the {maximum}-byte limit")
    parser_suffix = _safe_parser_suffix(filename)
    with source.open("rb") as stream:
        is_gzip = filename.lower().endswith(".gz") or stream.peek(2)[:2] == b"\x1f\x8b"
        if not is_gzip:
            # HTTP upload routes spool files with an opaque ``.upload`` suffix.
            # MolOP's automatic parser selection uses the path suffix, so do
            # not pass that opaque path through for an otherwise uncompressed
            # calculation output. Local CLI imports keep their native suffix
            # and can continue to avoid this copy.
            source_suffix = source.suffix.lower()
            if source_suffix in {".log", ".out", ".xyz"} and source_suffix == parser_suffix:
                return str(source), None
            path = temporary_dir / f"{input_index:08d}{parser_suffix}"
            copied_size = 0
            with path.open("wb") as output:
                while chunk := stream.read(1024 * 1024):
                    copied_size += len(chunk)
                    if copied_size > maximum:
                        raise ArtifactUploadError(
                            f"uploaded artifact exceeds the {maximum}-byte limit"
                        )
                    output.write(chunk)
            return str(path), None
        path = temporary_dir / f"{input_index:08d}{parser_suffix}"
        try:
            with (
                gzip.GzipFile(fileobj=stream, mode="rb") as decompressed,
                path.open("wb") as output,
            ):
                decompressed_size = 0
                while chunk := decompressed.read(1024 * 1024):
                    decompressed_size += len(chunk)
                    if decompressed_size > maximum:
                        raise ArtifactUploadError(
                            f"decompressed artifact exceeds the {maximum}-byte limit"
                        )
                    output.write(chunk)
        except (EOFError, OSError, zlib.error) as error:
            raise ArtifactUploadError("uploaded gzip artifact is invalid") from error
    return str(path), "gzip"


def _parse_calculation_outputs_batch(
    files: list[tuple[bytes | Path, str]],
    *,
    n_jobs: int,
    progress_queue: Queue[tuple[int, Any]] | None = None,
    timings_ms: MutableMapping[str, float] | None = None,
) -> dict[int, _ParsedArtifact | Exception]:
    """Parse all supplied files in one MolOP batch while retaining input order."""

    started_at = perf_counter()
    configure_molecular_graph_reconstruction()
    with tempfile.TemporaryDirectory(prefix="tricycle-molop-batch-") as temporary_dir:
        parsed_by_index: dict[int, _ParsedArtifact | Exception] = {}
        paths: list[str] = []
        file_indices: list[int] = []
        compressions: list[str | None] = []
        for index, (source, filename) in enumerate(files):
            try:
                path, source_compression = _prepare_calculation_parser_path(
                    source,
                    filename,
                    temporary_dir=Path(temporary_dir),
                    input_index=index,
                )
            except Exception as error:
                parsed_by_index[index] = error
                if progress_queue is not None:
                    progress_queue.put((index, error))
                continue
            paths.append(path)
            file_indices.append(index)
            compressions.append(source_compression)
        if timings_ms is not None:
            timings_ms["prepare_inputs_ms"] = (perf_counter() - started_at) * 1000
        if not paths:
            if timings_ms is not None:
                timings_ms["molop_parse_ms"] = 0.0
                timings_ms["total_ms"] = (perf_counter() - started_at) * 1000
            return parsed_by_index

        parse_started_at = perf_counter()

        def report_result(
            parser_index: int,
            result: tuple[_ParsedArtifact | None, str | None],
        ) -> None:
            if progress_queue is not None:
                progress_queue.put((file_indices[parser_index], result))

        if progress_queue is None:
            parallel_results = _parse_calculation_paths_parallel(
                paths,
                compressions,
                n_jobs=n_jobs,
            )
        else:
            parallel_results = _parse_calculation_paths_parallel(
                paths,
                compressions,
                n_jobs=n_jobs,
                on_result=report_result,
            )
        for parser_index, (parsed, error_message) in enumerate(parallel_results):
            input_index = file_indices[parser_index]
            parsed_by_index[input_index] = (
                parsed
                if parsed is not None
                else ArtifactUploadError(
                    error_message or "MolOP did not return a result for this input file"
                )
            )
        deferred = [
            parsed
            for parsed in parsed_by_index.values()
            if isinstance(parsed, _ParsedArtifact) and not parsed.frame_records
        ]
        if deferred:
            materialized = _materialize_parsed_artifacts(deferred)
            materialized_by_id = {
                id(original): converted
                for original, converted in zip(deferred, materialized, strict=True)
            }
            parsed_by_index = {
                index: materialized_by_id.get(id(parsed), parsed)
                for index, parsed in parsed_by_index.items()
            }
        if timings_ms is not None:
            timings_ms["molop_parse_ms"] = (perf_counter() - parse_started_at) * 1000
            timings_ms["total_ms"] = (perf_counter() - started_at) * 1000
        return parsed_by_index


def _prepare_pending_upload(
    session: Session,
    *,
    record: ArtifactFileRecord,
) -> tuple[ArtifactFile, _RetiredArtifactReservation | None, bool]:
    """Register the DB relation before writing bytes to RustFS.

    A pending row is the durable reservation for an upload.  Retries reuse a
    still-pending key so concurrent requests cannot move the reservation while
    one request is writing it; stale reservations receive a fresh hourly-partitioned
    key so GC can observe the retry in its normal window.
    """

    _acquire_identity_locks(session, ("artifact-content", record.content_sha256))
    content_artifacts = session.exec(
        select(ArtifactFile).where(ArtifactFile.content_sha256 == record.content_sha256)
    ).all()
    if any(item.artifact_kind is not record.artifact_kind for item in content_artifacts):
        raise ArtifactUploadConflictError(
            "an identical artifact is already registered with a different artifact kind"
        )
    artifact = next(
        (item for item in content_artifacts if item.project_id == record.project_id),
        None,
    )
    shared_available = next(
        (
            item
            for item in content_artifacts
            if item.storage_status is StorageStatus.AVAILABLE and item.bucket == record.bucket
        ),
        None,
    )
    if artifact is None:
        values = record.model_dump()
        if shared_available is not None:
            values.update(
                bucket=shared_available.bucket,
                object_key=shared_available.object_key,
            )
        artifact = ArtifactFile(**values)
        if session.info.get("tricycle_fast_insert", False):
            _prepare_new_entity(session, artifact)
        else:
            session.add(artifact)
            session.flush()
        return artifact, None, shared_available is not None
    if artifact.size_bytes != record.size_bytes:
        raise ValueError("artifact SHA-256 resolved to a different byte size")
    if artifact.artifact_kind is not record.artifact_kind:
        raise ArtifactUploadConflictError(
            "an identical artifact is already registered with a different artifact kind"
        )
    if artifact.storage_status is StorageStatus.AVAILABLE and artifact.bucket != record.bucket:
        raise ArtifactUploadConflictError(
            "an identical artifact is registered in a different RustFS bucket"
        )
    retired_reservation = None
    if artifact.storage_status is StorageStatus.RETIRED:
        retired_reservation = _RetiredArtifactReservation(
            bucket=artifact.bucket,
            object_key=artifact.object_key,
            version_id=artifact.version_id,
            etag=artifact.etag,
            storage_verified_at=artifact.storage_verified_at,
        )
    if artifact.storage_status is not StorageStatus.AVAILABLE:
        if shared_available is not None and shared_available.id != artifact.id:
            artifact.object_key = shared_available.object_key
            artifact.bucket = shared_available.bucket
        elif not (
            artifact.storage_status is StorageStatus.PENDING
            and _is_partitioned_upload_key(artifact.object_key)
        ):
            artifact.object_key = record.object_key
        artifact.bucket = record.bucket
        artifact.storage_status = StorageStatus.PENDING
        artifact.version_id = None
        artifact.etag = None
        artifact.storage_verified_at = None
        session.add(artifact)
        if not session.info.get("tricycle_fast_insert", False):
            session.flush()
    return artifact, retired_reservation, True


def _prepare_pending_uploads(
    session: Session,
    *,
    records: list[ArtifactFileRecord],
) -> dict[str, tuple[ArtifactFile, _RetiredArtifactReservation | None, bool]]:
    """Reserve batch artifact identities with set-based PostgreSQL lookups."""

    if not records:
        return {}
    by_digest = {record.content_sha256: record for record in records}
    _acquire_identity_locks(
        session,
        *(("artifact-content", digest) for digest in sorted(by_digest)),
    )
    existing_by_digest: dict[str, list[ArtifactFile]] = {}
    for existing in session.exec(
        select(ArtifactFile).where(col(ArtifactFile.content_sha256).in_(by_digest))
    ).all():
        existing_by_digest.setdefault(existing.content_sha256, []).append(existing)
    prepared: dict[
        str,
        tuple[ArtifactFile, _RetiredArtifactReservation | None, bool],
    ] = {}
    for digest, record in by_digest.items():
        content_artifacts = existing_by_digest.get(digest, [])
        if any(item.artifact_kind is not record.artifact_kind for item in content_artifacts):
            raise ArtifactUploadConflictError(
                "an identical artifact is already registered with a different artifact kind"
            )
        artifact = next(
            (item for item in content_artifacts if item.project_id == record.project_id),
            None,
        )
        shared_available = next(
            (
                item
                for item in content_artifacts
                if item.storage_status is StorageStatus.AVAILABLE and item.bucket == record.bucket
            ),
            None,
        )
        if artifact is None:
            values = record.model_dump()
            if shared_available is not None:
                values.update(
                    bucket=shared_available.bucket,
                    object_key=shared_available.object_key,
                )
            artifact = ArtifactFile(**values)
            _prepare_new_entity(session, artifact)
            prepared[digest] = (artifact, None, shared_available is not None)
            continue
        if artifact.size_bytes != record.size_bytes:
            raise ValueError("artifact SHA-256 resolved to a different byte size")
        if artifact.artifact_kind is not record.artifact_kind:
            raise ArtifactUploadConflictError(
                "an identical artifact is already registered with a different artifact kind"
            )
        if artifact.storage_status is StorageStatus.AVAILABLE and artifact.bucket != record.bucket:
            raise ArtifactUploadConflictError(
                "an identical artifact is registered in a different RustFS bucket"
            )
        retired_reservation = None
        if artifact.storage_status is StorageStatus.RETIRED:
            retired_reservation = _RetiredArtifactReservation(
                bucket=artifact.bucket,
                object_key=artifact.object_key,
                version_id=artifact.version_id,
                etag=artifact.etag,
                storage_verified_at=artifact.storage_verified_at,
            )
        if artifact.storage_status is not StorageStatus.AVAILABLE:
            if shared_available is not None and shared_available.id != artifact.id:
                artifact.object_key = shared_available.object_key
                artifact.bucket = shared_available.bucket
            elif not (
                artifact.storage_status is StorageStatus.PENDING
                and _is_partitioned_upload_key(artifact.object_key)
            ):
                artifact.object_key = record.object_key
            artifact.bucket = record.bucket
            artifact.storage_status = StorageStatus.PENDING
            artifact.version_id = None
            artifact.etag = None
            artifact.storage_verified_at = None
            session.add(artifact)
        prepared[digest] = (artifact, retired_reservation, True)
    return prepared


def _is_partitioned_upload_key(object_key: str) -> bool:
    parts = object_key.split("/")
    return (
        len(parts) == 8
        and parts[0] == "uploads"
        and len(parts[1]) == 4
        and len(parts[2]) == 2
        and len(parts[3]) == 2
        and len(parts[4]) == 2
        and parts[5] == "sha256"
        and len(parts[6]) == 2
        and len(parts[7]) == 64
    )


def _mark_upload_available(
    session: Session,
    *,
    artifact_id: UUID,
    object_key: str,
    stored: Any,
) -> ArtifactFile:
    _acquire_identity_locks(session, ("artifact-content", stored.sha256 or ""))
    artifact = session.get(ArtifactFile, artifact_id)
    if artifact is None:
        raise ArtifactUploadError("artifact reservation disappeared before storage verification")
    if artifact.object_key != object_key:
        raise ArtifactUploadError("artifact reservation changed during storage verification")
    if artifact.storage_status not in {StorageStatus.PENDING, StorageStatus.AVAILABLE}:
        raise ArtifactUploadError("artifact reservation is no longer writable")
    artifact.storage_status = StorageStatus.AVAILABLE
    artifact.version_id = stored.version_id
    artifact.etag = stored.etag
    artifact.storage_verified_at = stored.last_modified
    session.add(artifact)
    if not session.info.get("tricycle_fast_insert", False):
        session.flush()
    return artifact


def _mark_uploads_available(
    session: Session,
    *,
    stored_by_artifact_id: dict[UUID, tuple[str, Any]],
) -> None:
    """Advance verified batch reservations after one identity lookup."""

    if not stored_by_artifact_id:
        return
    artifacts = {
        artifact.id: artifact
        for artifact in session.exec(
            select(ArtifactFile).where(col(ArtifactFile.id).in_(stored_by_artifact_id))
        ).all()
        if artifact.id is not None
    }
    for artifact_id, (object_key, stored) in stored_by_artifact_id.items():
        artifact = artifacts.get(artifact_id)
        if artifact is None:
            raise ArtifactUploadError(
                "artifact reservation disappeared before storage verification"
            )
        if artifact.object_key != object_key:
            raise ArtifactUploadError("artifact reservation changed during storage verification")
        if artifact.storage_status not in {StorageStatus.PENDING, StorageStatus.AVAILABLE}:
            raise ArtifactUploadError("artifact reservation is no longer writable")
        artifact.storage_status = StorageStatus.AVAILABLE
        artifact.version_id = stored.version_id
        artifact.etag = stored.etag
        artifact.storage_verified_at = stored.last_modified
        session.add(artifact)


def _begin_upload_compensation(
    session: Session,
    *,
    artifact_id: UUID,
    object_key: str,
    content_sha256: str,
) -> tuple[UUID | None, bool]:
    """Reserve the content identity while a failed object write is cleaned up."""

    _acquire_identity_locks(session, ("artifact-content", content_sha256))
    artifact = session.get(ArtifactFile, artifact_id)
    if artifact is None:
        return None, True
    if artifact.storage_status is StorageStatus.AVAILABLE or artifact.object_key != object_key:
        return artifact_id, False
    shared_reference = session.exec(
        select(ArtifactFile.id).where(
            ArtifactFile.id != artifact_id,
            ArtifactFile.bucket == artifact.bucket,
            ArtifactFile.object_key == object_key,
            ArtifactFile.storage_status != StorageStatus.RETIRED,
        )
    ).first()
    return artifact_id, shared_reference is None


def _delete_reserved_object(
    settings: RustFSSettings,
    *,
    object_key: str,
    content_sha256: str,
) -> None:
    with RustFSObjectStore(settings) as store:
        if not store.exists(object_key):
            return
        metadata = store.head(object_key)
        if metadata.sha256 is not None and metadata.sha256 != content_sha256:
            raise ArtifactUploadError(
                f"refusing to delete an object with a different SHA-256: {object_key}"
            )
        store.delete(object_key, version_id=metadata.version_id)


def _finish_upload_compensation(
    session: Session,
    *,
    artifact_id: UUID | None,
    object_key: str,
    retired_reservation: _RetiredArtifactReservation | None = None,
) -> None:
    if artifact_id is None:
        return
    artifact = session.get(ArtifactFile, artifact_id)
    if artifact is None or artifact.object_key != object_key:
        return
    if artifact.storage_status is StorageStatus.PENDING:
        if retired_reservation is None:
            session.delete(artifact)
            return
        artifact.bucket = retired_reservation.bucket
        artifact.object_key = retired_reservation.object_key
        artifact.version_id = retired_reservation.version_id
        artifact.storage_status = StorageStatus.RETIRED
        artifact.etag = retired_reservation.etag
        artifact.storage_verified_at = retired_reservation.storage_verified_at
        session.add(artifact)


async def _compensate_upload(
    *,
    settings: RustFSSettings,
    artifact_id: UUID,
    object_key: str,
    content_sha256: str,
    retired_reservation: _RetiredArtifactReservation | None = None,
) -> None:
    """Best-effort cleanup for an object written before its DB state became available."""

    try:
        async with session_factory() as session:
            reservation = await session.run_sync(
                lambda sync_session: _begin_upload_compensation(
                    cast(Session, sync_session),
                    artifact_id=artifact_id,
                    object_key=object_key,
                    content_sha256=content_sha256,
                )
            )
            reserved_artifact_id, should_delete = reservation
            if should_delete:
                await asyncio.to_thread(
                    _delete_reserved_object,
                    settings,
                    object_key=object_key,
                    content_sha256=content_sha256,
                )
            await session.run_sync(
                lambda sync_session: _finish_upload_compensation(
                    cast(Session, sync_session),
                    artifact_id=reserved_artifact_id,
                    object_key=object_key,
                    retired_reservation=retired_reservation,
                )
            )
            await session.commit()
    except Exception:
        logger.exception("artifact upload compensation failed for %s", object_key)


def _create_pending_ingestion(
    session: Session,
    *,
    artifact: ArtifactFile,
    started_at: datetime,
) -> tuple[ArtifactIngestion, bool]:
    artifact_id = _require_id(artifact, label="ArtifactFile")
    _acquire_identity_locks(session, ("artifact_ingestion", artifact_id))
    ingestion = session.exec(
        select(ArtifactIngestion).where(ArtifactIngestion.artifact_file_id == artifact_id)
    ).first()
    created = ingestion is None
    if ingestion is None:
        ingestion = ArtifactIngestion(
            artifact_file_id=artifact_id,
            artifact_file=artifact,
            parser_version=MOLOP_VERSION,
            started_at=started_at,
        )
        ingestion.worker_lease_id = uuid4()
        ingestion.worker_lease_expires_at = started_at + timedelta(
            seconds=get_settings().upload_worker_lease_seconds
        )
        if session.info.get("tricycle_fast_insert", False):
            _prepare_new_entity(session, ingestion)
        else:
            session.add(ingestion)
            session.flush()
    else:
        has_revision = session.exec(
            select(ParseRevision.id).where(ParseRevision.artifact_file_id == artifact_id)
        ).first()
        if ingestion.status is ArtifactIngestionStatus.PENDING or (
            ingestion.status is not ArtifactIngestionStatus.PROCESSING and has_revision is None
        ):
            ingestion.status = ArtifactIngestionStatus.PENDING
            ingestion.source_frame_count = None
            ingestion.transition_state_frame_count = None
            ingestion.started_at = started_at
            ingestion.completed_at = None
            ingestion.worker_lease_id = uuid4()
            ingestion.worker_lease_expires_at = started_at + timedelta(
                seconds=get_settings().upload_worker_lease_seconds
            )
            ingestion.error_code = None
            ingestion.error_message = None
            session.add(ingestion)
            created = True
    return ingestion, created


def _create_pending_ingestions(
    session: Session,
    *,
    artifacts: list[ArtifactFile],
    started_by_artifact_id: dict[UUID, datetime],
) -> dict[UUID, tuple[ArtifactIngestion, bool]]:
    """Create or reopen batch ingestions with set-based existence checks."""

    if not artifacts:
        return {}
    artifact_ids = [_require_id(artifact, label="ArtifactFile") for artifact in artifacts]
    _acquire_identity_locks(
        session,
        *(("artifact_ingestion", artifact_id) for artifact_id in sorted(artifact_ids)),
    )
    existing_by_artifact_id = {
        ingestion.artifact_file_id: ingestion
        for ingestion in session.exec(
            select(ArtifactIngestion).where(
                col(ArtifactIngestion.artifact_file_id).in_(artifact_ids)
            )
        ).all()
    }
    artifacts_with_revisions = set(
        session.exec(
            select(ParseRevision.artifact_file_id).where(
                col(ParseRevision.artifact_file_id).in_(artifact_ids)
            )
        ).all()
    )
    result: dict[UUID, tuple[ArtifactIngestion, bool]] = {}
    for artifact in artifacts:
        artifact_id = _require_id(artifact, label="ArtifactFile")
        ingestion = existing_by_artifact_id.get(artifact_id)
        created = ingestion is None
        if ingestion is None:
            ingestion = ArtifactIngestion(
                artifact_file_id=artifact_id,
                artifact_file=artifact,
                parser_version=MOLOP_VERSION,
                started_at=started_by_artifact_id[artifact_id],
            )
            ingestion.worker_lease_id = uuid4()
            ingestion.worker_lease_expires_at = started_by_artifact_id[artifact_id] + timedelta(
                seconds=get_settings().upload_worker_lease_seconds
            )
            _prepare_new_entity(session, ingestion)
        elif ingestion.status is ArtifactIngestionStatus.PENDING or (
            ingestion.status is not ArtifactIngestionStatus.PROCESSING
            and artifact_id not in artifacts_with_revisions
        ):
            ingestion.status = ArtifactIngestionStatus.PENDING
            ingestion.source_frame_count = None
            ingestion.transition_state_frame_count = None
            ingestion.started_at = started_by_artifact_id[artifact_id]
            ingestion.completed_at = None
            ingestion.worker_lease_id = uuid4()
            ingestion.worker_lease_expires_at = started_by_artifact_id[artifact_id] + timedelta(
                seconds=get_settings().upload_worker_lease_seconds
            )
            ingestion.error_code = None
            ingestion.error_message = None
            session.add(ingestion)
            created = True
        result[artifact_id] = (ingestion, created)
    return result


def _persist_transition_state_endpoint(
    session: Session,
    *,
    calculation_frame: CalculationFrame,
    endpoint: Chem.Mol,
    direction: TransitionStateEndpointDirection,
    displacement_ratio: float,
    topology_context: GeometryPersistenceContext | None = None,
    prepared_topology: _PreparedEndpointTopology | None = None,
    identity_is_new: bool = False,
    defer_flush: bool = False,
) -> TransitionStateEndpoint:
    """Persist one signed endpoint without creating a normalized Geometry.

    Topology identity is canonicalized for reuse, but the Cartesian payload is
    intentionally kept in the original TS source atom order.  This preserves
    the exact common coordinate frame used by the TS and both displaced modes.
    """

    frame_id = _require_id(calculation_frame, label="CalculationFrame")
    existing = None
    if not identity_is_new:
        existing = session.exec(
            select(TransitionStateEndpoint).where(
                TransitionStateEndpoint.calculation_frame_id == frame_id,
                TransitionStateEndpoint.direction == direction,
            )
        ).first()
    if existing is not None:
        if (
            existing.charge != calculation_frame.charge
            or existing.multiplicity != calculation_frame.multiplicity
        ):
            raise ValueError("persisted TS endpoint electronic state differs from its TS frame")
        return existing
    if endpoint.GetNumConformers() != 1 or not endpoint.GetConformer().Is3D():
        raise ValueError("TS vibration endpoint must contain one 3D conformer")
    coordinates = np.array(
        endpoint.GetConformer().GetPositions(),
        dtype="<f8",
        order="C",
        copy=True,
    )
    if coordinates.shape != (endpoint.GetNumAtoms(), 3) or not np.isfinite(coordinates).all():
        raise ValueError("TS vibration endpoint coordinates are invalid")
    if endpoint.GetNumAtoms() != len(calculation_frame.observed_to_geometry_atom_indices):
        raise ValueError("TS vibration endpoint atom count differs from its source frame")
    endpoint_charge = sum(
        atom.GetFormalCharge()
        for atom in endpoint.GetAtoms()  # type: ignore[no-untyped-call]
    )
    if endpoint_charge != calculation_frame.charge:
        raise ValueError(
            "TS vibration endpoint atom formal-charge sum differs from its CalculationFrame charge"
        )
    prepared = prepared_topology
    if prepared is None:
        topology_record, source_to_topology = _normalize_transition_state_endpoint_topology(
            endpoint,
            direction,
        )
        prepared = _PreparedEndpointTopology(
            record=topology_record,
            source_to_topology_atom_indices=tuple(source_to_topology),
        )
    persisted_topology = persist_molecular_topology(
        session,
        prepared.record,
        context=topology_context,
    )
    topology_id = _require_id(persisted_topology.topology, label="MolecularTopology")
    source_coordinate_hash = sha256(coordinates.tobytes(order="C")).hexdigest()
    endpoint_values = {
        "calculation_frame_id": frame_id,
        "calculation_frame": calculation_frame,
        "topology_id": topology_id,
        "topology": persisted_topology.topology,
        "charge": int(calculation_frame.charge),
        "multiplicity": int(calculation_frame.multiplicity),
        "direction": direction,
        "atom_count": endpoint.GetNumAtoms(),
        "displacement_ratio": displacement_ratio,
        "source_coordinates": coordinates,
        "source_coordinate_hash": source_coordinate_hash,
        "source_to_topology_atom_indices": list(prepared.source_to_topology_atom_indices),
        "provenance": {
            "method": "molop.possible_pre_post_ts",
            "molop_version": MOLOP_VERSION,
            "coordinate_frame": "calculation_frame.observed_coordinates",
            "coordinate_order": "molop_source_atom_order",
            "direction": direction.value,
        },
    }
    endpoint_row = (
        _new_entity(session, TransitionStateEndpoint, **endpoint_values)
        if _fast_insert_enabled(session)
        else cast(Any, TransitionStateEndpoint)(**endpoint_values)
    )
    _flush_new_entity(session, endpoint_row, label="TransitionStateEndpoint")
    if not defer_flush:
        _attach_pending_entities(session)
        session.flush()
    return endpoint_row


def _normalize_transition_state_endpoint_topology(
    endpoint: Chem.Mol,
    direction: TransitionStateEndpointDirection,
) -> tuple[Any, list[int]]:
    return normalize_topology_with_mapping(
        endpoint,
        add_hydrogens=False,
        reconstruction_method="molop/possible_pre_post_ts",
        reconstruction_version=MOLOP_VERSION,
        reconstruction_metadata={
            "coordinate_frame": "calculation_frame.observed_coordinates",
            "coordinate_policy": "source-cartesian-no-independent-normalization",
            "direction": direction.value,
            "topology_source_trusted": True,
        },
    )


def _persist_transition_state_endpoints(
    session: Session,
    *,
    calculation_frame: CalculationFrame,
    inferred: _SuccessfulInference,
    topology_context: GeometryPersistenceContext | None = None,
    prepared_topology_records: _PreparedInferenceTopologyRecords | None = None,
    identity_is_new: bool = False,
    defer_flush: bool = False,
) -> None:
    if inferred.charge != calculation_frame.charge:
        raise ValueError("TS endpoint charge must match its CalculationFrame charge")
    if inferred.multiplicity != calculation_frame.multiplicity:
        raise ValueError("TS endpoint multiplicity must match its CalculationFrame multiplicity")
    if prepared_topology_records is None and topology_context is not None:
        cached = topology_context.inference_topology_records_by_object_id.get(id(inferred))
        if cached is not None and cached[0] is inferred:
            prepared_topology_records = cast(_PreparedInferenceTopologyRecords, cached[1])
    _persist_transition_state_endpoint(
        session,
        calculation_frame=calculation_frame,
        endpoint=inferred.negative_endpoint,
        direction=TransitionStateEndpointDirection.NEGATIVE,
        displacement_ratio=inferred.negative_displacement_ratio,
        topology_context=topology_context,
        prepared_topology=(
            prepared_topology_records.negative_endpoint
            if prepared_topology_records is not None
            else None
        ),
        identity_is_new=identity_is_new,
        defer_flush=defer_flush,
    )
    _persist_transition_state_endpoint(
        session,
        calculation_frame=calculation_frame,
        endpoint=inferred.positive_endpoint,
        direction=TransitionStateEndpointDirection.POSITIVE,
        displacement_ratio=inferred.positive_displacement_ratio,
        topology_context=topology_context,
        prepared_topology=(
            prepared_topology_records.positive_endpoint
            if prepared_topology_records is not None
            else None
        ),
        identity_is_new=identity_is_new,
        defer_flush=defer_flush,
    )


def persist_transition_state_endpoints_from_molop_frame(
    session: Session,
    *,
    calculation_frame: CalculationFrame,
    source_frame: BaseCalcFrame[Any],
    topology_context: GeometryPersistenceContext | None = None,
) -> None:
    """Persist MolOP's inferred pre/post-TS endpoints for a persisted TS frame."""

    vibrations = source_frame.vibrations
    if vibrations is None or len(vibrations.imaginary_idxs) != 1:
        raise ValueError("TS frame must contain exactly one imaginary mode")
    imaginary_position = vibrations.imaginary_idxs[0]
    negative, positive, negative_ratio, positive_ratio = _signed_ts_endpoints(
        source_frame,
        imaginary_position,
    )
    frequency = vibrations[imaginary_position].frequency
    if frequency is None:
        raise ValueError("TS imaginary mode is missing its frequency")
    file_frame_index = source_frame.file_frame_index
    if file_frame_index is None:
        raise ValueError("TS source frame is missing its stable file index")
    imaginary_mode_index = (
        vibrations.mode_indices[imaginary_position]
        if vibrations.mode_indices
        else imaginary_position
    )
    _persist_transition_state_endpoints(
        session,
        calculation_frame=calculation_frame,
        inferred=_SuccessfulInference(
            file_frame_index=file_frame_index,
            imaginary_mode_index=imaginary_mode_index,
            imaginary_frequency_cm1=float(magnitude_in(frequency, CM_INVERSE)),
            reaction_smiles="signed-mode-anchors-only",
            negative_endpoint=negative,
            positive_endpoint=positive,
            negative_displacement_ratio=negative_ratio,
            positive_displacement_ratio=positive_ratio,
            charge=int(source_frame.charge),
            multiplicity=int(source_frame.multiplicity),
        ),
        topology_context=topology_context,
    )


def _resolve_and_bind_transition_state_reaction(
    session: Session,
    *,
    inferred: _SuccessfulInference,
    calculation_frame: CalculationFrame,
    topology_context: GeometryPersistenceContext | None = None,
    prepared_topology_records: _PreparedInferenceTopologyRecords | None = None,
    refresh_thermodynamics: bool | None = None,
) -> tuple[UUID, UUID]:
    """Create the mapped endpoint reaction and bind its TS coordinate evidence."""

    effective_refresh_thermodynamics = (
        topology_context is None if refresh_thermodynamics is None else refresh_thermodynamics
    )

    legacy_bulk_import = bool(session.info.get(LEGACY_BULK_IMPORT_SESSION_INFO_KEY, False))
    prepared_records = prepared_topology_records
    cache_key: str | None
    cached_reaction_ids: tuple[UUID, UUID] | None
    if topology_context is not None and legacy_bulk_import:
        # The previous importer keyed repeated TS inferences by their mapped
        # reaction string. Preserve that cache, while reusing the per-inference
        # normalized records prepared by batch preload when available.
        cache_key = inferred.reaction_smiles
        cached_reaction_ids = topology_context.inferred_reaction_ids_by_key.get(cache_key)
        cached_participant_records = topology_context.inferred_reaction_topology_records_by_key.get(
            cache_key
        )
        if prepared_records is None:
            prepared_records = _inference_topology_records_for_context(
                inferred,
                topology_context,
                reaction_records=cached_participant_records,
                include_participants=(
                    cached_reaction_ids is None and cached_participant_records is None
                ),
            )
    elif topology_context is not None:
        if prepared_records is None:
            prepared_records = _inference_topology_records_for_context(
                inferred,
                topology_context,
            )
        strict_records = prepared_records.all_records
        # A mapped reaction string alone is insufficient: distinct endpoint
        # stereochemistry must remain distinct even when the TS frame has a
        # different E/Z assignment.
        cache_key = _inference_reaction_cache_key(inferred, strict_records)
        cached_reaction_ids = topology_context.inferred_reaction_ids_by_key.get(cache_key)
    else:
        if prepared_records is None:
            prepared_records = _prepare_inference_topology_records(inferred)
        cache_key = None
        cached_reaction_ids = None
    assert prepared_records is not None
    if cached_reaction_ids is None:
        if topology_context is not None:
            assert cache_key is not None
            precomputed_topology_records: tuple[NormalizedTopologyRecord, ...] = (
                prepared_records.participant_records
            )
        else:
            # The endpoint fragments are authoritative MolGR graphs. Reusing
            # their records here avoids sanitizing RDKit reaction templates,
            # which can mutate electronic state and cannot safely inspect some
            # multicoordinate metal structures.
            precomputed_topology_records = prepared_records.participant_records
        reaction_result = create_reaction_in_session(
            session,
            CreateReactionCommand(
                reaction=inferred.reaction_smiles,
                mapped_reaction_kind=MappedReactionKind.OTHER,
            ),
            defer_thermodynamic_refresh=is_transition_state_frame_eligible(
                calculation_frame.frame_role
            ),
            defer_geometry_reconciliation=(
                topology_context is not None and topology_context.reconciliation_cache is not None
            ),
            topology_context=topology_context,
            include_creation_metadata=topology_context is None,
            precomputed_topology_records=precomputed_topology_records,
            reconciliation_cache=(
                topology_context.reconciliation_cache if topology_context is not None else None
            ),
        )
        if reaction_result.mapped_reaction_id is None:
            raise ValueError("MolOP TS endpoint reaction did not produce a complete atom mapping")
        logical_reaction_id = reaction_result.logical_reaction_id
        mapped_reaction_id = reaction_result.mapped_reaction_id
        if topology_context is not None:
            assert cache_key is not None
            topology_context.inferred_reaction_ids_by_key[cache_key] = (
                logical_reaction_id,
                mapped_reaction_id,
            )
            if prepared_records.participant_records:
                topology_context.inferred_reaction_topology_records_by_key.setdefault(
                    cache_key,
                    prepared_records.participant_records,
                )
    else:
        logical_reaction_id, mapped_reaction_id = cached_reaction_ids
        if topology_context is not None:
            topology_context.inferred_reaction_cache_hits += 1
    mapped_reaction = (
        topology_context.mapped_reactions_by_id.get(mapped_reaction_id)
        if topology_context is not None
        else None
    )
    if mapped_reaction is None:
        mapped_reaction = session.get(MappedReaction, mapped_reaction_id)
    if mapped_reaction is None:
        mapped_reaction = next(
            (
                candidate
                for candidate in (
                    *session.new,
                    *session.info.get("_fast_pending_entities", ()),
                )
                if isinstance(candidate, MappedReaction) and candidate.id == mapped_reaction_id
            ),
            None,
        )
    if mapped_reaction is None:
        raise RuntimeError("MolOP TS inference created a missing MappedReaction")
    # Reaction reconciliation may flush internally; register all deferred
    # reaction rows before that happens so relationship backrefs stay intact.
    _attach_pending_entities(session)
    if is_transition_state_frame_eligible(calculation_frame.frame_role):
        bind_transition_state_frame(
            session,
            mapped_reaction=mapped_reaction,
            calculation_frame=calculation_frame,
            cache=(topology_context.reconciliation_cache if topology_context is not None else None),
            refresh_thermodynamics=effective_refresh_thermodynamics,
        )
    else:
        ensure_transition_state_path(
            session,
            mapped_reaction=mapped_reaction,
            cache=(topology_context.reconciliation_cache if topology_context is not None else None),
        )
    return logical_reaction_id, mapped_reaction_id


def _persist_successful_inference(
    session: Session,
    *,
    ingestion: ArtifactIngestion,
    parse_revision: ParseRevision,
    inferred: _SuccessfulInference,
    calculation_frame: CalculationFrame,
    topology_context: GeometryPersistenceContext | None = None,
    identity_is_new: bool = False,
    defer_flush: bool = False,
    defer_thermodynamic_refresh: bool = False,
) -> TransitionStateInference:
    prepared_topology_records = (
        _inference_topology_records_for_context(inferred, topology_context)
        if topology_context is not None
        else _prepare_inference_topology_records(inferred)
    )
    logical_reaction_id, mapped_reaction_id = _resolve_and_bind_transition_state_reaction(
        session,
        inferred=inferred,
        calculation_frame=calculation_frame,
        topology_context=topology_context,
        prepared_topology_records=prepared_topology_records,
        refresh_thermodynamics=(topology_context is None and not defer_thermodynamic_refresh),
    )
    inference_values = {
        "artifact_ingestion_id": _require_id(ingestion, label="ArtifactIngestion"),
        "artifact_ingestion": ingestion,
        "parse_revision_id": _require_id(parse_revision, label="ParseRevision"),
        "parse_revision": parse_revision,
        "file_frame_index": inferred.file_frame_index,
        "imaginary_mode_index": inferred.imaginary_mode_index,
        "imaginary_frequency_cm1": inferred.imaginary_frequency_cm1,
        "status": TransitionStateInferenceStatus.SUCCEEDED,
        "inference_method": "molop/possible_pre_post_ts",
        "inference_settings": {
            "endpoint_selection": "molop.possible_pre_post_ts",
            "side_topology": "most frequent side topology per signed side",
            "reaction_side_semantics": "fragment-rich endpoint first",
            "direction_semantics": (
                "measured signed displacement along the imaginary mode; "
                "negative side displaces along +mode"
            ),
            "imaginary_mode_index": inferred.imaginary_mode_index,
        },
        "logical_reaction_id": logical_reaction_id,
        "mapped_reaction_id": mapped_reaction_id,
        "calculation_frame_id": _require_id(calculation_frame, label="CalculationFrame"),
    }
    inference = (
        _new_entity(session, TransitionStateInference, **inference_values)
        if _fast_insert_enabled(session)
        else cast(Any, TransitionStateInference)(**inference_values)
    )
    _flush_new_entity(session, inference, label="TransitionStateInference")
    if _fast_insert_enabled(session):
        _attach_or_reuse_entity(session, inference)
    if not defer_flush:
        _attach_pending_entities(session)
        session.flush()
    _persist_transition_state_endpoints(
        session,
        calculation_frame=calculation_frame,
        inferred=inferred,
        topology_context=topology_context,
        prepared_topology_records=prepared_topology_records,
        identity_is_new=identity_is_new,
        defer_flush=defer_flush,
    )
    return inference


def _add_failed_inference(
    session: Session,
    *,
    deferred: _DeferredArtifactInferences,
    inferred: _Inference,
    error_code: str,
    error_message: str | None = None,
) -> None:
    values = {
        "artifact_ingestion_id": _require_id(
            deferred.ingestion,
            label="ArtifactIngestion",
        ),
        "parse_revision_id": _require_id(deferred.parse_revision, label="ParseRevision"),
        "file_frame_index": inferred.file_frame_index,
        "imaginary_mode_index": inferred.imaginary_mode_index,
        "imaginary_frequency_cm1": inferred.imaginary_frequency_cm1,
        "status": TransitionStateInferenceStatus.FAILED,
        "inference_method": "molop/possible_pre_post_ts",
        "inference_settings": {
            "endpoint_selection": "molop.possible_pre_post_ts",
            **(
                {"failure": inferred.error_metadata}
                if isinstance(inferred, _FailedInference) and inferred.error_metadata is not None
                else {}
            ),
        },
        "error_code": error_code,
        "error_message": (
            error_message if error_message is not None else getattr(inferred, "error_message", None)
        ),
    }
    failed_inference = (
        _new_entity(session, TransitionStateInference, **values)
        if _fast_insert_enabled(session)
        else cast(Any, TransitionStateInference)(**values)
    )
    _flush_new_entity(session, failed_inference, label="TransitionStateInference")
    if _fast_insert_enabled(session):
        _attach_or_reuse_entity(session, failed_inference)


def _persist_one_new_inference(
    session: Session,
    task: _InferencePersistenceTask,
    *,
    topology_context: GeometryPersistenceContext | None,
    defer_thermodynamic_refresh: bool = False,
) -> None:
    context_snapshot = _snapshot_inference_context(topology_context)
    pending_snapshot = list(session.info.get("_fast_pending_entities", ()))
    try:
        with session.begin_nested():
            _persist_successful_inference(
                session,
                ingestion=task.deferred.ingestion,
                parse_revision=task.deferred.parse_revision,
                inferred=task.inferred,
                calculation_frame=task.calculation_frame,
                topology_context=topology_context,
                identity_is_new=task.deferred.revision_created,
                defer_flush=False,
                defer_thermodynamic_refresh=defer_thermodynamic_refresh,
            )
    except Exception as error:
        # The nested transaction rolls back database rows, but it cannot roll
        # back Python-side reconciliation indexes.  Restore both before the
        # failed inference is recorded so the next task cannot reuse a binding
        # that never committed.
        logger.exception(
            "failed to persist inferred TS reaction artifact=%s frame=%s",
            task.deferred.ingestion.artifact_file_id,
            task.inferred.file_frame_index,
        )
        _restore_inference_context(topology_context, context_snapshot)
        _set_fast_pending_entities(session, pending_snapshot)
        _add_failed_inference(
            session,
            deferred=task.deferred,
            inferred=task.inferred,
            error_code="inferred_reaction_persistence_failed",
            error_message=str(error) or type(error).__name__,
        )


_INFERENCE_CONTEXT_MUTABLE_FIELDS = (
    "topologies",
    "formulas_by_hash",
    "topologies_by_identity",
    "topology_derivations_by_key",
    "geometries_by_hash",
    "exact_geometry_keys_loaded",
    "equivalent_geometry_by_key",
    "equivalent_geometry_candidates",
    "equivalent_geometry_keys_loaded",
    "in_memory_geometries_by_identity",
    "geometries_to_reconcile",
    "topologies_to_resolve_reactions",
    "reaction_participants_by_topology",
    "mapped_reactions_by_id",
    "mapped_reactions_by_logical_reaction",
    "mapped_reaction_participants_by_reaction",
    "memberships_by_concrete_topology",
    "logical_participants_by_logical_reaction",
    "logical_participants_by_id",
    "molecular_topologies_by_id",
    "mapped_reactions_to_reconcile",
    "inferred_reaction_ids_by_key",
    "inferred_reaction_topology_records_by_key",
)
_RECONCILIATION_CACHE_MUTABLE_FIELDS = (
    "nodes_by_reaction",
    "nodes_by_key",
    "loaded_reaction_nodes",
    "node_geometries_by_node",
    "loaded_node_geometries",
    "complete_node_geometries",
    "mappings_by_node_geometry_id",
    "loaded_mappings",
    "transition_state_paths_ready",
    "thermodynamics_refreshed_reactions",
    "new_node_geometry_ids",
    "thermodynamic_property_geometry_ids",
    "affected_reactions_by_id",
    "logical_member_reactions_by_topology",
    "endpoint_compatible_reactions_by_topology",
    "reaction_lookup_topologies_loaded",
    "new_mapped_reaction_ids",
)


def _copy_inference_snapshot_value(value: object) -> object:
    """Copy mutable containers while retaining ORM identity holders."""

    if isinstance(value, dict):
        return {key: _copy_inference_snapshot_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_inference_snapshot_value(item) for item in value]
    if isinstance(value, set):
        return set(value)
    if isinstance(value, tuple):
        return tuple(_copy_inference_snapshot_value(item) for item in value)
    return value


def _snapshot_inference_context(
    topology_context: GeometryPersistenceContext | None,
) -> tuple[dict[str, object], dict[str, object] | None, int] | None:
    if topology_context is None:
        return None
    context_state = {
        name: _copy_inference_snapshot_value(getattr(topology_context, name))
        for name in _INFERENCE_CONTEXT_MUTABLE_FIELDS
    }
    cache = topology_context.reconciliation_cache
    cache_state = (
        {
            name: _copy_inference_snapshot_value(getattr(cache, name))
            for name in _RECONCILIATION_CACHE_MUTABLE_FIELDS
        }
        if isinstance(cache, ReconciliationBatchCache)
        else None
    )
    return context_state, cache_state, topology_context.inferred_reaction_cache_hits


def _restore_inference_context(
    topology_context: GeometryPersistenceContext | None,
    snapshot: tuple[dict[str, object], dict[str, object] | None, int] | None,
) -> None:
    if topology_context is None or snapshot is None:
        return
    context_state, cache_state, cache_hits = snapshot
    for name, saved in context_state.items():
        current = getattr(topology_context, name)
        current.clear()
        current.update(saved)
    topology_context.inferred_reaction_cache_hits = cache_hits
    cache = topology_context.reconciliation_cache
    if cache_state is None or not isinstance(cache, ReconciliationBatchCache):
        return
    for name, saved in cache_state.items():
        current = getattr(cache, name)
        current.clear()
        current.update(saved)


def _persist_inference_batch(
    session: Session,
    tasks: list[_InferencePersistenceTask],
    *,
    topology_context: GeometryPersistenceContext | None,
    defer_thermodynamic_refresh: bool = False,
) -> None:
    """Flush several new TS inferences together, with per-row fallback."""

    if not tasks:
        return
    previous_bulk_insert_disabled = session.info.get("tricycle_bulk_insert_disabled", False)
    session.info["tricycle_bulk_insert_disabled"] = True
    try:
        if len(tasks) == 1:
            _persist_one_new_inference(
                session,
                tasks[0],
                topology_context=topology_context,
                defer_thermodynamic_refresh=defer_thermodynamic_refresh,
            )
            if not defer_thermodynamic_refresh:
                _refresh_inference_reaction_profiles(
                    session,
                    topology_context=topology_context,
                )
            return
        context_snapshot = _snapshot_inference_context(topology_context)
        pending_snapshot = list(session.info.get("_fast_pending_entities", ()))
        try:
            with session.begin_nested():
                for task in tasks:
                    _persist_successful_inference(
                        session,
                        ingestion=task.deferred.ingestion,
                        parse_revision=task.deferred.parse_revision,
                        inferred=task.inferred,
                        calculation_frame=task.calculation_frame,
                        topology_context=topology_context,
                        identity_is_new=task.deferred.revision_created,
                        defer_flush=True,
                        defer_thermodynamic_refresh=defer_thermodynamic_refresh,
                    )
                _attach_pending_entities(session)
                session.flush()
                if not defer_thermodynamic_refresh:
                    _refresh_inference_reaction_profiles(
                        session,
                        topology_context=topology_context,
                    )
        except Exception:
            _restore_inference_context(topology_context, context_snapshot)
            _set_fast_pending_entities(session, pending_snapshot)
            for task in tasks:
                _persist_one_new_inference(
                    session,
                    task,
                    topology_context=topology_context,
                    defer_thermodynamic_refresh=defer_thermodynamic_refresh,
                )
            if not defer_thermodynamic_refresh:
                _refresh_inference_reaction_profiles(
                    session,
                    topology_context=topology_context,
                )
    finally:
        session.info["tricycle_bulk_insert_disabled"] = previous_bulk_insert_disabled


def _refresh_inference_reaction_profiles(
    session: Session,
    *,
    topology_context: GeometryPersistenceContext | None,
) -> None:
    """Refresh TS reaction profiles once after a persistence microbatch.

    Binding a TS frame used to refresh and flush the whole Session for every
    inference.  The batch cache already tracks affected reactions, so refresh
    them after all evidence rows in the microbatch have been attached.
    """

    if topology_context is None or topology_context.reconciliation_cache is None:
        return
    cache = topology_context.reconciliation_cache
    reactions = tuple(cache.affected_reactions_by_id.values())
    refresh_mapped_reactions_thermodynamics(session, reactions)
    for mapped_reaction in reactions:
        mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
        cache.thermodynamics_refreshed_reactions.add(mapped_reaction_id)
    # New TS frames can add the same reaction in a later inference
    # microbatch, so retain only the dirty set for the current flush window.
    cache.affected_reactions_by_id.clear()


def _refresh_cleared_reaction_profiles(
    session: Session,
    *,
    cleanup: ParseCleanupSummary,
    defer_thermodynamic_refresh: bool = False,
) -> None:
    """Refresh or defer profiles after stale revision-owned bindings are removed."""

    if not cleanup.affected_mapped_reaction_ids:
        return
    session.expire_all()
    reactions = session.exec(
        select(MappedReaction).where(
            col(MappedReaction.id).in_(cleanup.affected_mapped_reaction_ids)
        )
    ).all()
    if defer_thermodynamic_refresh:
        mark_mapped_reactions_thermodynamics_dirty(session, reactions)
    else:
        refresh_mapped_reactions_thermodynamics(session, reactions)


def _persist_artifact_inferences(
    session: Session,
    deferred: _DeferredArtifactInferences,
    *,
    topology_context: GeometryPersistenceContext | None = None,
    defer_thermodynamic_refresh: bool = False,
) -> None:
    _persist_artifact_inferences_batch(
        session,
        [deferred],
        topology_context=topology_context,
        defer_thermodynamic_refresh=defer_thermodynamic_refresh,
    )


def _persist_artifact_inferences_batch(
    session: Session,
    deferred_items: list[_DeferredArtifactInferences],
    *,
    topology_context: GeometryPersistenceContext | None = None,
    defer_thermodynamic_refresh: bool = False,
) -> None:
    pending_tasks: list[_InferencePersistenceTask] = []

    def flush_pending() -> None:
        nonlocal pending_tasks
        if pending_tasks:
            _persist_inference_batch(
                session,
                pending_tasks,
                topology_context=topology_context,
                defer_thermodynamic_refresh=defer_thermodynamic_refresh,
            )
            pending_tasks = []

    for deferred in deferred_items:
        parse_revision_id = _require_id(deferred.parse_revision, label="ParseRevision")
        for inferred in deferred.parsed.inferences:
            existing = None
            if not deferred.revision_created:
                existing = session.exec(
                    select(TransitionStateInference).where(
                        TransitionStateInference.parse_revision_id == parse_revision_id,
                        TransitionStateInference.file_frame_index == inferred.file_frame_index,
                    )
                ).first()
            if existing is not None:
                flush_pending()
                if (
                    existing.status is TransitionStateInferenceStatus.SUCCEEDED
                    and existing.mapped_reaction_id is not None
                    and existing.calculation_frame_id is not None
                ):
                    mapped_reaction = session.get(MappedReaction, existing.mapped_reaction_id)
                    calculation_frame = session.get(
                        CalculationFrame,
                        existing.calculation_frame_id,
                    )
                    if mapped_reaction is None or calculation_frame is None:
                        raise RuntimeError(
                            "successful TS inference references missing reaction evidence"
                        )
                    ensure_transition_state_path(
                        session,
                        mapped_reaction=mapped_reaction,
                        cache=(
                            topology_context.reconciliation_cache
                            if topology_context is not None
                            else None
                        ),
                    )
                    if is_transition_state_frame_eligible(calculation_frame.frame_role):
                        bind_transition_state_frame(
                            session,
                            mapped_reaction=mapped_reaction,
                            calculation_frame=calculation_frame,
                            cache=(
                                topology_context.reconciliation_cache
                                if topology_context is not None
                                else None
                            ),
                            refresh_thermodynamics=(
                                topology_context is None and not defer_thermodynamic_refresh
                            ),
                        )
                    if isinstance(inferred, _SuccessfulInference):
                        _persist_transition_state_endpoints(
                            session,
                            calculation_frame=calculation_frame,
                            inferred=inferred,
                            topology_context=topology_context,
                        )
                continue
            if isinstance(inferred, _FailedInference):
                flush_pending()
                _add_failed_inference(
                    session,
                    deferred=deferred,
                    inferred=inferred,
                    error_code=inferred.error_code,
                )
                continue
            calculation_frame = deferred.frames_by_file_index.get(inferred.file_frame_index)
            if calculation_frame is None:
                flush_pending()
                _add_failed_inference(
                    session,
                    deferred=deferred,
                    inferred=inferred,
                    error_code="inferred_reaction_persistence_failed",
                    error_message="persisted calculation is missing the MolOP TS frame",
                )
                continue
            pending_tasks.append(
                _InferencePersistenceTask(
                    deferred=deferred,
                    inferred=inferred,
                    calculation_frame=calculation_frame,
                )
            )
            if len(pending_tasks) >= INFERENCE_PERSIST_BATCH_SIZE:
                flush_pending()
    flush_pending()


def _persist_parsed_artifact(
    session: Session,
    *,
    ingestion_id: UUID,
    parsed: _ParsedArtifact,
    started_at: datetime,
    completed_at: datetime,
    force_new_revision: bool = False,
    geometry_context: GeometryPersistenceContext | None = None,
    preload_geometry_context: bool = True,
    ingestion: ArtifactIngestion | None = None,
    existing_revision_ids: set[UUID] | None = None,
    defer_ingestion_completion: bool = False,
    defer_reconciliation: bool = False,
    defer_thermodynamic_refresh: bool = False,
    deferred_inferences: list[_DeferredArtifactInferences] | None = None,
) -> tuple[UUID, bool]:
    ingestion = ingestion or session.get(ArtifactIngestion, ingestion_id)
    if ingestion is None:
        raise RuntimeError("artifact ingestion disappeared during parsing")
    # Fast MolOP parsing keeps coordinate-only frames so the expensive graph
    # work can be shared across the persistence batch.  Single-file uploads
    # reach this path directly, while batch uploads materialize their files in
    # ``persist_parsed_files`` before calling us.
    if not parsed.frame_records and parsed.source_frame_count:
        parsed = _materialize_parsed_artifacts([parsed])[0]
    has_parser_diagnostics = bool(parsed.parse_diagnostics) or any(
        record.frame.parse_completeness is ParseCompleteness.PARTIAL
        or bool(record.frame.parse_diagnostics)
        for record in parsed.frame_records
    )
    isolated_frame_persistence = _parsed_artifact_requires_isolated_frame_persistence(parsed) or (
        has_parser_diagnostics and not defer_reconciliation
    )
    artifact = ingestion.artifact_file
    if existing_revision_ids is None:
        existing_revision_ids = {
            revision_id
            for revision_id in session.exec(
                select(ParseRevision.id).where(ParseRevision.artifact_file_id == artifact.id)
            ).all()
            if isinstance(revision_id, UUID)
        }
    if force_new_revision and existing_revision_ids:
        # The persistence primitive is also used directly by import/recovery
        # code. Keep the replacement invariant here as a final guard: a
        # forced revision can never append to an obsolete materialization.
        cleanup = clear_previous_parse_results_batch(
            session,
            artifact_file_ids=(_require_id(artifact, label="ArtifactFile"),),
        )
        _refresh_cleared_reaction_profiles(
            session,
            cleanup=cleanup,
            defer_thermodynamic_refresh=defer_thermodynamic_refresh,
        )
        existing_revision_ids.clear()
    # Single-file uploads and reparses do not arrive through the batch
    # persistence coordinator, so they have no caller-owned geometry context.
    # Reuse one project-bound context for both Formula/Topology/Geometry
    # persistence and the deferred TS reaction inference.  Without this,
    # reparsing a historical file rebuilt its frames but called
    # ``create_reaction_in_session`` with ``project_id=None``; the new
    # inference then reused an unowned fallback reaction instead of
    # materializing a project-owned reaction.
    active_geometry_context = geometry_context or GeometryPersistenceContext(
        project_id=artifact.project_id
    )
    if active_geometry_context.project_id != artifact.project_id:
        raise ValueError("GeometryPersistenceContext project does not match ArtifactFile project")
    persisted_artifact = persist_molop_calculation_artifact(
        session,
        artifact=artifact,
        chem_file=parsed.chem_file,
        records=list(parsed.frame_records),
        source_compression=parsed.source_compression,
        record_sha256=parsed.record_sha256,
        started_at=started_at,
        completed_at=completed_at,
        force_new_revision=force_new_revision,
        # A forced retry still creates a brand-new ParseRevision and all of its
        # revision-local rows.  It is therefore safe to use the same deferred
        # frame queue as a first parse; ``force_new_revision`` only changes the
        # revision identity policy, not the batching policy.  Disabling this
        # path for retries silently changed the durable worker to one-frame-at-
        # a-time ORM writes.
        fast_insert=(
            _fast_molop_ingestion_enabled()
            and (not existing_revision_ids or force_new_revision)
            and not isolated_frame_persistence
        ),
        parallel_frame_persistence=(
            _fast_molop_ingestion_enabled()
            and (not existing_revision_ids or force_new_revision)
            and not isolated_frame_persistence
        ),
        geometry_context=active_geometry_context,
        preload_geometry_context=preload_geometry_context,
        defer_reconciliation=defer_reconciliation,
        parse_diagnostics=list(parsed.parse_diagnostics),
    )
    session.info.setdefault("_molop_artifact_diagnostics", {})[ingestion_id] = (
        persisted_artifact.parse_diagnostics,
        persisted_artifact.failed_frame_count,
        persisted_artifact.parse_completeness,
    )
    parse_revision = persisted_artifact.parse_revision
    parse_revision_id = _require_id(parse_revision, label="ParseRevision")
    revision_created = parse_revision_id not in existing_revision_ids
    inference_work = _DeferredArtifactInferences(
        ingestion=ingestion,
        parse_revision=parse_revision,
        parsed=parsed,
        frames_by_file_index=persisted_artifact.frames_by_file_index,
        revision_created=revision_created,
        defer_revision_local_flush=deferred_inferences is not None,
    )
    if deferred_inferences is None:
        _persist_artifact_inferences(
            session,
            inference_work,
            topology_context=active_geometry_context,
            defer_thermodynamic_refresh=defer_thermodynamic_refresh,
        )
    else:
        deferred_inferences.append(inference_work)

    if defer_ingestion_completion:
        return parse_revision_id, revision_created

    _attach_pending_entities(session)
    session.flush()
    outcomes = session.exec(
        select(TransitionStateInference).where(
            TransitionStateInference.parse_revision_id == parse_revision_id
        )
    ).all()
    successes = sum(
        outcome.status is TransitionStateInferenceStatus.SUCCEEDED for outcome in outcomes
    )
    failures = len(outcomes) - successes
    status = (
        ArtifactIngestionStatus.PARTIAL
        if failures or persisted_artifact.parse_completeness is ParseCompleteness.PARTIAL
        else ArtifactIngestionStatus.SUCCEEDED
    )
    ingestion.status = status
    ingestion.source_frame_count = parsed.source_frame_count
    ingestion.transition_state_frame_count = len(parsed.inferences)
    ingestion.completed_at = completed_at
    ingestion.worker_lease_id = None
    ingestion.worker_lease_expires_at = None
    ingestion.error_code = None
    ingestion.error_message = None
    ingestion.parser_metadata = {
        "source_format": parsed.source_format,
        "latest_parse_revision_id": str(parse_revision_id),
        "latest_parse_revision_created": revision_created,
        "ts_selection": "frame.is_TS is True",
        "inferred_reaction_identity": "shared topology-and-atom-mapping identity",
        "parse_completeness": (
            ParseCompleteness.PARTIAL.value
            if status is ArtifactIngestionStatus.PARTIAL
            else ParseCompleteness.COMPLETE.value
        ),
        "parse_diagnostics": list(persisted_artifact.parse_diagnostics),
    }
    session.add(ingestion)
    return parse_revision_id, revision_created


def _mark_ingestion_failed(
    session: Session,
    *,
    ingestion_id: UUID,
    error: Exception,
    error_code: str,
    completed_at: datetime,
    ingestion: ArtifactIngestion | None = None,
    source_frame_count: int | None = None,
    transition_state_frame_count: int | None = None,
    error_metadata: dict[str, Any] | None = None,
    expected_worker_lease_id: UUID | None = None,
) -> bool:
    ingestion = ingestion or session.get(ArtifactIngestion, ingestion_id)
    if ingestion is None:
        raise RuntimeError("artifact ingestion disappeared during parsing")
    if expected_worker_lease_id is not None and (
        ingestion.worker_lease_id != expected_worker_lease_id
        or ingestion.worker_lease_expires_at is None
        or ingestion.worker_lease_expires_at <= datetime.now(UTC)
    ):
        return False
    ingestion.status = ArtifactIngestionStatus.FAILED
    ingestion.completed_at = completed_at
    ingestion.worker_lease_id = None
    ingestion.worker_lease_expires_at = None
    if isinstance(error, GeometryAssignmentAmbiguityError):
        error_code = error.error_code
        ingestion.parser_metadata = {
            **ingestion.parser_metadata,
            "qc_rejection": error.evidence(),
        }
    if error_metadata is None:
        evidence = getattr(error, "evidence", None)
        if callable(evidence):
            candidate = evidence()
            if isinstance(candidate, dict):
                error_metadata = candidate
    if error_metadata:
        ingestion.parser_metadata = {
            **ingestion.parser_metadata,
            "failure": error_metadata,
        }
    ingestion.error_code = error_code
    ingestion.error_message = str(error) or type(error).__name__
    if source_frame_count is not None:
        ingestion.source_frame_count = source_frame_count
    if transition_state_frame_count is not None:
        ingestion.transition_state_frame_count = transition_state_frame_count
    session.add(ingestion)
    return True


def _reset_ingestion_for_clean_reparse(
    ingestion: ArtifactIngestion,
    *,
    started_at: datetime,
    status: ArtifactIngestionStatus = ArtifactIngestionStatus.PENDING,
    worker_lease_id: UUID | None = None,
    worker_lease_expires_at: datetime | None = None,
    cleanup: ParseCleanupSummary | None = None,
) -> None:
    """Make an ingestion represent an empty, retryable parse reservation."""

    ingestion.status = status
    ingestion.source_frame_count = None
    ingestion.transition_state_frame_count = None
    ingestion.started_at = started_at
    ingestion.completed_at = None
    ingestion.worker_lease_id = worker_lease_id
    ingestion.worker_lease_expires_at = worker_lease_expires_at
    ingestion.error_code = None
    ingestion.error_message = None
    ingestion.parser_metadata = {
        "reparse_reset": True,
        "deleted_parse_revision_count": cleanup.deleted_revision_count if cleanup else 0,
        "deleted_frame_count": cleanup.deleted_frame_count if cleanup else 0,
        "deleted_segment_count": cleanup.deleted_segment_count if cleanup else 0,
        "deleted_inference_count": cleanup.deleted_inference_count if cleanup else 0,
    }


def _mark_ingestion_filtered(
    session: Session,
    *,
    ingestion_id: UUID,
    error: Exception,
    error_code: str,
    completed_at: datetime,
    ingestion: ArtifactIngestion | None = None,
    source_frame_count: int = 0,
    transition_state_frame_count: int = 0,
    error_metadata: dict[str, Any] | None = None,
    expected_worker_lease_id: UUID | None = None,
) -> bool:
    marked = _mark_ingestion_failed(
        session,
        ingestion_id=ingestion_id,
        error=error,
        error_code=error_code,
        completed_at=completed_at,
        ingestion=ingestion,
        source_frame_count=source_frame_count,
        transition_state_frame_count=transition_state_frame_count,
        error_metadata=error_metadata,
        expected_worker_lease_id=expected_worker_lease_id,
    )
    if not marked:
        return False
    resolved = ingestion or session.get(ArtifactIngestion, ingestion_id)
    if resolved is None:
        raise RuntimeError("artifact ingestion disappeared while marking it filtered")
    resolved.status = ArtifactIngestionStatus.FILTERED
    session.add(resolved)
    return True


def _result(
    session: Session,
    ingestion_id: UUID,
    *,
    parse_revision_id: UUID | None = None,
    parse_revision_created: bool | None = None,
) -> ArtifactUploadResult:
    ingestion = session.get(ArtifactIngestion, ingestion_id)
    if ingestion is None:
        raise RuntimeError("artifact ingestion not found")
    if parse_revision_id is None:
        parse_revision_id = session.exec(
            select(ParseRevision.id)
            .where(ParseRevision.artifact_file_id == ingestion.artifact_file_id)
            .order_by(col(ParseRevision.created_at).desc(), col(ParseRevision.id).desc())
        ).first()
    predicates = [TransitionStateInference.artifact_ingestion_id == ingestion_id]
    if parse_revision_id is not None:
        predicates.append(TransitionStateInference.parse_revision_id == parse_revision_id)
    rows = session.exec(
        select(TransitionStateInference)
        .where(*predicates)
        .order_by(col(TransitionStateInference.file_frame_index))
    ).all()
    views = [
        TransitionStateInferenceView(
            id=_require_id(row, label="TransitionStateInference"),
            parse_revision_id=row.parse_revision_id,
            file_frame_index=row.file_frame_index,
            imaginary_mode_index=row.imaginary_mode_index,
            imaginary_frequency_cm1=row.imaginary_frequency_cm1,
            status=row.status,
            logical_reaction_id=row.logical_reaction_id,
            mapped_reaction_id=row.mapped_reaction_id,
            calculation_frame_id=row.calculation_frame_id,
            error_code=row.error_code,
            error_message=row.error_message,
            error_metadata_json=(
                json.dumps(row.inference_settings.get("failure"), sort_keys=True)
                if isinstance(row.inference_settings.get("failure"), dict)
                else None
            ),
        )
        for row in rows
    ]
    artifact = ingestion.artifact_file
    return ArtifactUploadResult(
        artifact_id=_require_id(artifact, label="ArtifactFile"),
        artifact_kind=artifact.artifact_kind,
        storage_status=artifact.storage_status,
        ingestion_id=ingestion_id,
        parse_revision_id=parse_revision_id,
        parse_revision_created=parse_revision_created,
        ingestion_status=ingestion.status,
        source_frame_count=ingestion.source_frame_count,
        transition_state_frame_count=ingestion.transition_state_frame_count,
        inferred_reaction_count=sum(
            item.status is TransitionStateInferenceStatus.SUCCEEDED for item in views
        ),
        inferences=views,
    )


def _batch_results(
    session: Session,
    *,
    parse_revision_by_ingestion_id: Mapping[UUID, UUID | None],
    parse_revision_created_by_ingestion_id: Mapping[UUID, bool | None],
    inferences_by_ingestion_id: Mapping[UUID, list[TransitionStateInference]] | None = None,
) -> dict[UUID, ArtifactUploadResult]:
    """Build completed upload views with two set-based reads.

    Batch persistence already has every parse revision identity in memory.  Do
    not turn that into a get-plus-inference query pair for every upload merely
    to produce the response DTO.
    """

    ingestion_ids = list(parse_revision_by_ingestion_id)
    if not ingestion_ids:
        return {}
    ingestions = session.exec(
        select(ArtifactIngestion)
        .where(col(ArtifactIngestion.id).in_(ingestion_ids))
        .options(joinedload(cast(Any, ArtifactIngestion.artifact_file)))
    ).all()
    if inferences_by_ingestion_id is None:
        inferences_by_ingestion_id = {ingestion_id: [] for ingestion_id in ingestion_ids}
        for inference in session.exec(
            select(TransitionStateInference)
            .where(col(TransitionStateInference.artifact_ingestion_id).in_(ingestion_ids))
            .order_by(col(TransitionStateInference.file_frame_index))
        ).all():
            inferences_by_ingestion_id.setdefault(
                inference.artifact_ingestion_id,
                [],
            ).append(inference)

    results: dict[UUID, ArtifactUploadResult] = {}
    for ingestion in ingestions:
        ingestion_id = _require_id(ingestion, label="ArtifactIngestion")
        parse_revision_id = parse_revision_by_ingestion_id[ingestion_id]
        rows = [
            row
            for row in inferences_by_ingestion_id[ingestion_id]
            if parse_revision_id is not None and row.parse_revision_id == parse_revision_id
        ]
        views = [
            TransitionStateInferenceView(
                id=_require_id(row, label="TransitionStateInference"),
                parse_revision_id=row.parse_revision_id,
                file_frame_index=row.file_frame_index,
                imaginary_mode_index=row.imaginary_mode_index,
                imaginary_frequency_cm1=row.imaginary_frequency_cm1,
                status=row.status,
                logical_reaction_id=row.logical_reaction_id,
                mapped_reaction_id=row.mapped_reaction_id,
                calculation_frame_id=row.calculation_frame_id,
                error_code=row.error_code,
                error_message=row.error_message,
                error_metadata_json=(
                    json.dumps(row.inference_settings.get("failure"), sort_keys=True)
                    if isinstance(row.inference_settings.get("failure"), dict)
                    else None
                ),
            )
            for row in rows
        ]
        artifact = ingestion.artifact_file
        results[ingestion_id] = ArtifactUploadResult(
            artifact_id=_require_id(artifact, label="ArtifactFile"),
            artifact_kind=artifact.artifact_kind,
            storage_status=artifact.storage_status,
            ingestion_id=ingestion_id,
            parse_revision_id=parse_revision_id,
            parse_revision_created=parse_revision_created_by_ingestion_id[ingestion_id],
            ingestion_status=ingestion.status,
            source_frame_count=ingestion.source_frame_count,
            transition_state_frame_count=ingestion.transition_state_frame_count,
            inferred_reaction_count=sum(
                item.status is TransitionStateInferenceStatus.SUCCEEDED for item in views
            ),
            inferences=views,
        )
    return results


def _finalize_batch_ingestions(
    session: Session,
    *,
    ingestions_by_id: Mapping[UUID, ArtifactIngestion],
    parse_revision_by_ingestion_id: Mapping[UUID, UUID | None],
    completion_by_ingestion_id: Mapping[UUID, _IngestionCompletion],
) -> dict[UUID, list[TransitionStateInference]]:
    """Publish successful parse state before any dependent reconciliation.

    A persistence microbatch deliberately defers its ingestion completion
    update until the shared transaction is ready to reconcile geometry.  The
    thermodynamic loader uses that status as part of its source visibility
    predicate, so completion must be durable before reconciliation refreshes
    mapped-reaction profiles.  Keep this as one set-based read and one state
    update pass so the result builder remains read-only.
    """

    ingestion_ids = tuple(parse_revision_by_ingestion_id)
    inferences_by_ingestion_id: dict[UUID, list[TransitionStateInference]] = {
        ingestion_id: [] for ingestion_id in ingestion_ids
    }
    for inference in session.exec(
        select(TransitionStateInference)
        .where(col(TransitionStateInference.artifact_ingestion_id).in_(ingestion_ids))
        .order_by(col(TransitionStateInference.file_frame_index))
    ).all():
        inferences_by_ingestion_id.setdefault(
            inference.artifact_ingestion_id,
            [],
        ).append(inference)

    completed: dict[UUID, tuple[UUID, _IngestionCompletion]] = {}
    for ingestion_id, parse_revision_id in parse_revision_by_ingestion_id.items():
        completion = completion_by_ingestion_id.get(ingestion_id)
        if parse_revision_id is not None and completion is not None:
            completed[ingestion_id] = (parse_revision_id, completion)
    if not completed:
        return inferences_by_ingestion_id

    failed_revision_pairs = {
        (inference.artifact_ingestion_id, inference.parse_revision_id)
        for ingestion_id in completed
        for inference in inferences_by_ingestion_id[ingestion_id]
        if inference.status is TransitionStateInferenceStatus.FAILED
    }

    statuses_by_id: dict[UUID, ArtifactIngestionStatus] = {}
    source_counts_by_id: dict[UUID, int] = {}
    transition_counts_by_id: dict[UUID, int] = {}
    completed_at_by_id: dict[UUID, datetime] = {}
    parser_metadata_by_id: dict[UUID, dict[str, object]] = {}
    for ingestion_id, (parse_revision_id, completion) in completed.items():
        ingestion = ingestions_by_id.get(ingestion_id)
        if ingestion is None:
            raise RuntimeError("artifact ingestion disappeared during batch finalization")
        has_failed_inference = (ingestion_id, parse_revision_id) in failed_revision_pairs
        status = (
            ArtifactIngestionStatus.PARTIAL
            if has_failed_inference or completion.parse_completeness is ParseCompleteness.PARTIAL
            else ArtifactIngestionStatus.SUCCEEDED
        )
        metadata: dict[str, object] = {
            "source_format": completion.source_format,
            "latest_parse_revision_id": str(completion.parse_revision_id),
            "latest_parse_revision_created": completion.parse_revision_created,
            "ts_selection": "frame.is_TS is True",
            "inferred_reaction_identity": "shared topology-and-atom-mapping identity",
            "parse_completeness": completion.parse_completeness.value,
            "parse_diagnostics": list(completion.parse_diagnostics),
        }
        statuses_by_id[ingestion_id] = status
        source_counts_by_id[ingestion_id] = completion.source_frame_count
        transition_counts_by_id[ingestion_id] = completion.transition_state_frame_count
        completed_at_by_id[ingestion_id] = completion.completed_at
        parser_metadata_by_id[ingestion_id] = metadata

    if completed:
        ingestion_id_column = col(ArtifactIngestion.id)
        session.exec(
            update(ArtifactIngestion)
            .where(ingestion_id_column.in_(completed))
            .values(
                status=case(
                    statuses_by_id,
                    value=ingestion_id_column,
                    else_=col(ArtifactIngestion.status),
                ),
                source_frame_count=case(
                    source_counts_by_id,
                    value=ingestion_id_column,
                    else_=col(ArtifactIngestion.source_frame_count),
                ),
                transition_state_frame_count=case(
                    transition_counts_by_id,
                    value=ingestion_id_column,
                    else_=col(ArtifactIngestion.transition_state_frame_count),
                ),
                completed_at=case(
                    completed_at_by_id,
                    value=ingestion_id_column,
                    else_=col(ArtifactIngestion.completed_at),
                ),
                worker_lease_id=None,
                worker_lease_expires_at=None,
                error_code=None,
                error_message=None,
                parser_metadata=case(
                    {
                        ingestion_id: sa_cast(metadata, JSONB)
                        for ingestion_id, metadata in parser_metadata_by_id.items()
                    },
                    value=ingestion_id_column,
                    else_=col(ArtifactIngestion.parser_metadata),
                ),
            )
        )
        # The result builder reuses the preloaded ORM instances.  Mark the
        # values as committed so the set-based UPDATE does not get overwritten
        # by a stale unit-of-work flush and no refresh SELECT is needed.
        for ingestion_id, ingestion in ingestions_by_id.items():
            if ingestion_id not in statuses_by_id:
                continue
            set_committed_value(ingestion, "status", statuses_by_id[ingestion_id])
            set_committed_value(
                ingestion,
                "source_frame_count",
                source_counts_by_id[ingestion_id],
            )
            set_committed_value(
                ingestion,
                "transition_state_frame_count",
                transition_counts_by_id[ingestion_id],
            )
            set_committed_value(ingestion, "completed_at", completed_at_by_id[ingestion_id])
            set_committed_value(ingestion, "worker_lease_id", None)
            set_committed_value(ingestion, "worker_lease_expires_at", None)
            set_committed_value(ingestion, "error_code", None)
            set_committed_value(ingestion, "error_message", None)
            set_committed_value(ingestion, "parser_metadata", parser_metadata_by_id[ingestion_id])
    return inferences_by_ingestion_id


def _preload_batch_persistence_state(
    session: Session,
    *,
    ingestion_ids: list[UUID],
) -> tuple[dict[UUID, ArtifactIngestion], dict[UUID, set[UUID]]]:
    """Load batch-owned ingestions and revision identities in two reads."""

    if not ingestion_ids:
        return {}, {}
    ingestions = session.exec(
        select(ArtifactIngestion)
        .where(col(ArtifactIngestion.id).in_(ingestion_ids))
        .options(joinedload(cast(Any, ArtifactIngestion.artifact_file)))
    ).all()
    ingestions_by_id = {
        _require_id(ingestion, label="ArtifactIngestion"): ingestion for ingestion in ingestions
    }
    missing_ingestion_ids = set(ingestion_ids) - set(ingestions_by_id)
    if missing_ingestion_ids:
        raise RuntimeError("artifact ingestion disappeared during batch persistence")
    artifact_ids = [ingestion.artifact_file_id for ingestion in ingestions]
    revision_ids_by_artifact_id: dict[UUID, set[UUID]] = {
        artifact_id: set() for artifact_id in artifact_ids
    }
    for artifact_id, revision_id in session.exec(
        select(ParseRevision.artifact_file_id, ParseRevision.id)
        .where(col(ParseRevision.artifact_file_id).in_(artifact_ids))
        .order_by(
            col(ParseRevision.artifact_file_id),
            col(ParseRevision.revision_number).desc(),
        )
    ).all():
        if not isinstance(artifact_id, UUID) or not isinstance(revision_id, UUID):
            raise RuntimeError("persisted ParseRevision is missing an identity")
        revision_ids_by_artifact_id[artifact_id].add(revision_id)
    return ingestions_by_id, revision_ids_by_artifact_id


def _run_mark_ingestion_failed(
    session: SQLAlchemySession,
    *,
    ingestion_id: UUID,
    error: Exception,
    error_code: str,
    completed_at: datetime,
    ingestion: ArtifactIngestion | None = None,
    source_frame_count: int | None = None,
    transition_state_frame_count: int | None = None,
    error_metadata: dict[str, Any] | None = None,
    expected_worker_lease_id: UUID | None = None,
) -> None:
    _mark_ingestion_failed(
        cast(Session, session),
        ingestion_id=ingestion_id,
        error=error,
        error_code=error_code,
        completed_at=completed_at,
        ingestion=ingestion,
        source_frame_count=source_frame_count,
        transition_state_frame_count=transition_state_frame_count,
        error_metadata=error_metadata,
        expected_worker_lease_id=expected_worker_lease_id,
    )


def _run_mark_ingestion_filtered(
    session: SQLAlchemySession,
    *,
    ingestion_id: UUID,
    error: Exception,
    error_code: str,
    completed_at: datetime,
    ingestion: ArtifactIngestion | None = None,
    source_frame_count: int = 0,
    transition_state_frame_count: int = 0,
    error_metadata: dict[str, Any] | None = None,
    expected_worker_lease_id: UUID | None = None,
) -> None:
    _mark_ingestion_filtered(
        cast(Session, session),
        ingestion_id=ingestion_id,
        error=error,
        error_code=error_code,
        completed_at=completed_at,
        ingestion=ingestion,
        source_frame_count=source_frame_count,
        transition_state_frame_count=transition_state_frame_count,
        error_metadata=error_metadata,
        expected_worker_lease_id=expected_worker_lease_id,
    )


def _run_prepare_pending_uploads(
    session: SQLAlchemySession,
    *,
    records: list[ArtifactFileRecord],
) -> dict[str, tuple[ArtifactFile, _RetiredArtifactReservation | None, bool]]:
    return _prepare_pending_uploads(cast(Session, session), records=records)


def _run_create_pending_ingestions(
    session: SQLAlchemySession,
    *,
    artifacts: list[ArtifactFile],
    started_by_artifact_id: dict[UUID, datetime],
) -> dict[UUID, tuple[ArtifactIngestion, bool]]:
    return _create_pending_ingestions(
        cast(Session, session),
        artifacts=artifacts,
        started_by_artifact_id=started_by_artifact_id,
    )


def _run_mark_uploads_available(
    session: SQLAlchemySession,
    *,
    stored_by_artifact_id: dict[UUID, tuple[str, Any]],
) -> None:
    _mark_uploads_available(
        cast(Session, session),
        stored_by_artifact_id=stored_by_artifact_id,
    )


def _run_flush(session: SQLAlchemySession) -> dict[str, object]:
    typed_session = cast(Session, session)
    previous_fast_insert = typed_session.info.get("tricycle_fast_insert", False)
    typed_session.info["tricycle_fast_insert"] = True
    try:
        _attach_pending_entities(typed_session)
        _flush_if_needed(typed_session)
        diagnostics = typed_session.info.get("_fast_bulk_insert_diagnostics")
        return dict(diagnostics) if isinstance(diagnostics, dict) else {}
    finally:
        typed_session.info["tricycle_fast_insert"] = previous_fast_insert


def _run_disable_autoflush(session: SQLAlchemySession) -> None:
    cast(Session, session).autoflush = False


def _run_persist_parsed_artifact_savepoint(
    session: SQLAlchemySession,
    **kwargs: Any,
) -> tuple[UUID, bool]:
    """Persist one batch file with a local failure boundary.

    The fast path keeps revision-local rows out of SQLAlchemy's unit of work
    until the whole persistence microbatch is flushed.  ``begin_nested()``
    unconditionally flushes ORM-owned rows at its boundary, which can write a
    child before a deferred parent shared with another file.  Use the pending
    queue/context snapshots as the isolation boundary there; the regular path
    retains a real database savepoint.
    """

    typed_session = cast(Session, session)
    # ``_persist_parsed_artifact`` enables fast insertion internally and
    # restores the session flag before returning.  Batch callers identify this
    # mode by deferring reconciliation, so inspect the configured fast-path
    # switch here as well; otherwise ``begin_nested`` would still flush the
    # deferred queue at every file boundary.
    parsed = kwargs.get("parsed")
    requires_isolated_frame_persistence = isinstance(
        parsed, _ParsedArtifact
    ) and _parsed_artifact_requires_isolated_frame_persistence(parsed)
    fast_mode = not requires_isolated_frame_persistence and (
        typed_session.info.get("tricycle_fast_insert", False)
        or (kwargs.get("defer_reconciliation", False) and _fast_molop_ingestion_enabled())
    )
    if fast_mode:
        pending_checkpoint = _fast_pending_entity_count(typed_session)
        try:
            return _persist_parsed_artifact(typed_session, **kwargs)
        except Exception:
            # Fast rows are appended to a Python queue until the persistence
            # microbatch flush.  A file-level failure must remove its queued
            # revision-local rows or a later file can flush them as if they
            # were valid.  This is the deferred equivalent of the regular
            # path's database savepoint.
            _truncate_fast_pending_entities(typed_session, pending_checkpoint)
            raise
    # Keep the previous high-throughput path allocation-free at the file
    # boundary.  The snapshots are only needed when a real savepoint is used;
    # taking them in fast mode copies the growing geometry/inference context
    # for every parsed file and reintroduces the regression this helper avoids.
    context = kwargs.get("geometry_context")
    context_snapshot = _snapshot_inference_context(context)
    pending_snapshot = list(typed_session.info.get("_fast_pending_entities", ()))
    try:
        with typed_session.begin_nested():
            return _persist_parsed_artifact(typed_session, **kwargs)
    except Exception:
        _restore_inference_context(context, context_snapshot)
        # A regular partial-file boundary can follow clean files whose rows
        # are still in the deferred queue.  Restore that queue regardless of
        # the current mode; the nested transaction rolls back any rows that
        # were attached from it before the failure.
        _set_fast_pending_entities(typed_session, pending_snapshot)
        raise


def _run_persist_deferred_inferences(
    session: SQLAlchemySession,
    *,
    deferred_inferences: list[_DeferredArtifactInferences],
    topology_context: GeometryPersistenceContext,
    defer_thermodynamic_refresh: bool = False,
) -> None:
    typed_session = cast(Session, session)
    # In a persistence microbatch, reactions are created after Geometry rows
    # have been flushed.  Keep one cache alive while reactions and TS evidence
    # are attached; the final Geometry reconciliation will preload the
    # participant rows and reuse these path identities.
    if topology_context.reconciliation_cache is None:
        topology_context.reconciliation_cache = ReconciliationBatchCache()
    previous_fast_insert = typed_session.info.get("tricycle_fast_insert", False)
    typed_session.info["tricycle_fast_insert"] = True
    try:
        _persist_artifact_inferences_batch(
            typed_session,
            deferred_inferences,
            topology_context=topology_context,
            defer_thermodynamic_refresh=defer_thermodynamic_refresh,
        )
    finally:
        typed_session.info["tricycle_fast_insert"] = previous_fast_insert


def _run_reconcile_molop_geometry_context(
    session: SQLAlchemySession,
    *,
    context: GeometryPersistenceContext,
    defer_thermodynamic_refresh: bool = False,
) -> set[UUID]:
    return reconcile_molop_geometry_context(
        cast(Session, session),
        context,
        refresh_thermodynamics=not defer_thermodynamic_refresh,
    )


def _run_clear_and_reset_parse_state(
    session: SQLAlchemySession,
    *,
    artifact_file_ids: Sequence[UUID],
    started_at: datetime,
    worker_lease_by_artifact_id: Mapping[UUID, tuple[UUID | None, datetime | None]] | None = None,
    defer_thermodynamic_refresh: bool = False,
) -> ParseCleanupSummary:
    """Delete old materialization and reset ingestion rows in one transaction."""

    typed_session = cast(Session, session)
    ordered_ids = tuple(dict.fromkeys(artifact_file_ids))
    if not ordered_ids:
        return ParseCleanupSummary()
    _acquire_identity_locks(
        typed_session,
        *(("artifact_ingestion", artifact_id) for artifact_id in ordered_ids),
    )
    cleanup = clear_previous_parse_results_batch(
        typed_session,
        artifact_file_ids=ordered_ids,
    )
    _refresh_cleared_reaction_profiles(
        typed_session,
        cleanup=cleanup,
        defer_thermodynamic_refresh=defer_thermodynamic_refresh,
    )
    ingestions = typed_session.exec(
        select(ArtifactIngestion)
        .where(col(ArtifactIngestion.artifact_file_id).in_(ordered_ids))
        .with_for_update()
    ).all()
    for ingestion in ingestions:
        lease = (
            worker_lease_by_artifact_id.get(ingestion.artifact_file_id)
            if worker_lease_by_artifact_id is not None
            else None
        )
        _reset_ingestion_for_clean_reparse(
            ingestion,
            started_at=started_at,
            status=(
                ArtifactIngestionStatus.PROCESSING
                if lease is not None and lease[0] is not None
                else ArtifactIngestionStatus.PENDING
            ),
            worker_lease_id=lease[0] if lease is not None else None,
            worker_lease_expires_at=lease[1] if lease is not None else None,
            cleanup=cleanup,
        )
        typed_session.add(ingestion)
    return cleanup


def _run_preload_molecular_geometry_context(
    session: SQLAlchemySession,
    *,
    parsed_artifacts: list[_ParsedArtifact],
    context: GeometryPersistenceContext,
    topology_records: list[Any] | None = None,
) -> None:
    typed_session = cast(Session, session)
    previous_fast_insert = typed_session.info.get("tricycle_fast_insert", False)
    # Preload creates the shared Formula/Topology/Derivation identities used by
    # the frame loop. Give it the same client-ID/deferred-attach mode as the
    # actual artifact writer so it does not flush one shared row at a time.
    typed_session.info["tricycle_fast_insert"] = True
    try:
        preload_molecular_geometry_context(
            typed_session,
            [
                (record.molecule, record.frame.coordinate_decimal_places)
                for parsed in parsed_artifacts
                for record in parsed.frame_records
            ],
            context=context,
            topology_records=topology_records or (),
        )
    finally:
        typed_session.info["tricycle_fast_insert"] = previous_fast_insert


def _prepare_inference_topology_records(
    inferred: _SuccessfulInference,
    *,
    reaction_records: tuple[NormalizedTopologyRecord, ...] | None = None,
    include_participants: bool = True,
) -> _PreparedInferenceTopologyRecords:
    """Normalize inference graphs once, retaining endpoint atom mappings.

    Participant records come directly from the source-order MolGR endpoint
    fragments.  This keeps radical, charge, and stereo annotations under the
    same normalization/serialization validation as every other MolGR graph.
    """

    # The endpoints already contain the coordinate-authoritative stereo
    # snapshot. Topology normalization consumes that snapshot; it does not
    # perform a second coordinate inference here.
    negative_endpoint = inferred.negative_endpoint
    positive_endpoint = inferred.positive_endpoint
    normalized_endpoints: list[_PreparedEndpointTopology] = []
    for endpoint, direction in (
        (negative_endpoint, TransitionStateEndpointDirection.NEGATIVE),
        (positive_endpoint, TransitionStateEndpointDirection.POSITIVE),
    ):
        record, source_to_topology = _normalize_transition_state_endpoint_topology(
            endpoint, direction
        )
        normalized_endpoints.append(
            _PreparedEndpointTopology(
                record=record,
                source_to_topology_atom_indices=tuple(source_to_topology),
            )
        )
    if reaction_records is None and include_participants:
        endpoints = sorted(
            (negative_endpoint, positive_endpoint),
            key=lambda endpoint: len(Chem.GetMolFrags(endpoint)),
            reverse=True,
        )
        normalized_participants: list[NormalizedTopologyRecord] = []
        for side, endpoint in zip(("reactant", "product"), endpoints, strict=True):
            source = Chem.Mol(endpoint)
            for atom_index, atom in enumerate(
                source.GetAtoms()  # type: ignore[no-untyped-call]
            ):
                atom.SetAtomMapNum(atom_index + 1)
            # Split before discarding the endpoint conformer. Each fragment
            # must see the same coordinate evidence used by the complete
            # endpoint when its E/Z writer metadata is repaired.
            fragments = Chem.GetMolFrags(
                source,
                asMols=True,
                sanitizeFrags=False,
            )
            # Every source atom already has a unique map tied to its endpoint
            # index. Use that identity to order fragments; serializing solely
            # for sorting used to canonicalize a stereo projection before the
            # fragment reached topology normalization.
            ordered_fragments = sorted(
                fragments,
                key=lambda fragment: tuple(
                    sorted(atom.GetAtomMapNum() for atom in fragment.GetAtoms())
                ),
            )
            for template_index, fragment in enumerate(ordered_fragments):
                normalized_participants.append(
                    normalize_topology(
                        fragment,
                        add_hydrogens=False,
                        reconstruction_method="molgr/possible_pre_post_ts",
                        reconstruction_version=MOLOP_VERSION,
                        reconstruction_metadata={
                            "coordinate_frame": "calculation_frame.observed_coordinates",
                            "topology_source_trusted": True,
                            "source_fragment": True,
                            "source_atom_map_numbers": [
                                atom.GetAtomMapNum() for atom in fragment.GetAtoms()
                            ],
                            "side": side,
                            "template_index": template_index,
                        },
                    )
                )
        reaction_records = tuple(normalized_participants)
    return _PreparedInferenceTopologyRecords(
        negative_endpoint=normalized_endpoints[0],
        positive_endpoint=normalized_endpoints[1],
        participant_records=reaction_records or (),
    )


def _inference_topology_records_for_context(
    inferred: _SuccessfulInference,
    context: GeometryPersistenceContext,
    *,
    reaction_records: tuple[NormalizedTopologyRecord, ...] | None = None,
    include_participants: bool = True,
) -> _PreparedInferenceTopologyRecords:
    """Reuse the exact normalized records throughout one persistence batch."""

    cached = context.inference_topology_records_by_object_id.get(id(inferred))
    if cached is not None and cached[0] is inferred:
        return cast(_PreparedInferenceTopologyRecords, cached[1])
    prepared = _prepare_inference_topology_records(
        inferred,
        reaction_records=reaction_records,
        include_participants=include_participants,
    )
    # Retaining ``inferred`` in the value prevents id reuse while the context
    # is alive. Parsed artifacts retain every inference through persistence.
    context.inference_topology_records_by_object_id[id(inferred)] = (inferred, prepared)
    return prepared


def _inference_molecule_cache_signature(molecule: Chem.Mol) -> object:
    """Return a deterministic strict graph signature for an inference endpoint.

    MolOP endpoint molecules are coordinate-authoritative and can contain
    unsanitized graphs. Isomeric SMILES is the compact path; the explicit
    graph fallback keeps cache entries distinct when RDKit cannot serialize a
    partially sanitized endpoint.
    """

    try:
        atom_maps = [
            atom.GetAtomMapNum()
            for atom in molecule.GetAtoms()  # type: ignore[no-untyped-call]
        ]
        has_unique_maps = (
            bool(atom_maps)
            and all(number > 0 for number in atom_maps)
            and len(set(atom_maps)) == len(atom_maps)
        )
        return {
            "encoding": "rdkit-isomeric-smiles-v1",
            "value": serialize_molecule_smiles(
                molecule,
                preserve_atom_maps=has_unique_maps,
                retain_atom_maps=True,
                all_hs_explicit=True,
            ),
        }
    except Exception:
        atoms = [
            {
                "index": atom.GetIdx(),
                "atomic_number": atom.GetAtomicNum(),
                "isotope": atom.GetIsotope(),
                "formal_charge": atom.GetFormalCharge(),
                "radical_electrons": atom.GetNumRadicalElectrons(),
                "explicit_hydrogens": atom.GetNumExplicitHs(),
                "no_implicit": atom.GetNoImplicit(),
                "aromatic": atom.GetIsAromatic(),
                "chiral_tag": str(atom.GetChiralTag()),
                "map_number": atom.GetAtomMapNum(),
            }
            for atom in molecule.GetAtoms()  # type: ignore[no-untyped-call]
        ]
        bonds = [
            {
                "begin": bond.GetBeginAtomIdx(),
                "end": bond.GetEndAtomIdx(),
                "type": str(bond.GetBondType()),
                "aromatic": bond.GetIsAromatic(),
                "stereo": str(bond.GetStereo()),
                "stereo_atoms": list(bond.GetStereoAtoms()),
                "direction": str(bond.GetBondDir()),
            }
            for bond in molecule.GetBonds()  # type: ignore[no-untyped-call]
        ]
        return {
            "encoding": "rdkit-explicit-graph-v1",
            "atoms": atoms,
            "bonds": bonds,
        }


def _inference_topology_record_cache_signature(record: Any) -> dict[str, object]:
    """Extract the immutable strict identity from a normalized record."""

    topology = record.topology
    formula = record.formula
    derivation = record.topology_derivation
    return {
        "formula_composition_hash": formula.composition_hash,
        "identity_schema_version": topology.identity_schema_version,
        "graph_hash": topology.graph_hash,
        "canonical_isomeric_smiles": topology.canonical_isomeric_smiles,
        "stereo_status": getattr(topology.stereo_status, "value", topology.stereo_status),
        "provenance_schema_version": derivation.provenance_schema_version,
        "provenance_hash": derivation.provenance_hash,
    }


def _inference_reaction_cache_key(
    inferred: _SuccessfulInference,
    records: Sequence[Any] = (),
) -> str:
    """Hash all strict inference inputs used to create a mapped reaction.

    ``reaction_smiles`` is retained as a field for reaction identity, but it
    is deliberately not used as the cache key by itself. The endpoint
    signatures cover the coordinate-derived strict state and the normalized
    records cover MolGR graph identities/provenance.
    """

    payload = {
        "schema_version": "molop-inference-reaction-cache-v2",
        "reaction_smiles": inferred.reaction_smiles,
        "negative_endpoint": _inference_molecule_cache_signature(inferred.negative_endpoint),
        "positive_endpoint": _inference_molecule_cache_signature(inferred.positive_endpoint),
        "topology_records": [
            _inference_topology_record_cache_signature(record) for record in records
        ],
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _run_batch_results(
    session: SQLAlchemySession,
    *,
    parse_revision_by_ingestion_id: Mapping[UUID, UUID | None],
    parse_revision_created_by_ingestion_id: Mapping[UUID, bool | None],
    inferences_by_ingestion_id: Mapping[UUID, list[TransitionStateInference]] | None = None,
) -> dict[UUID, ArtifactUploadResult]:
    return _batch_results(
        cast(Session, session),
        parse_revision_by_ingestion_id=parse_revision_by_ingestion_id,
        parse_revision_created_by_ingestion_id=parse_revision_created_by_ingestion_id,
        inferences_by_ingestion_id=inferences_by_ingestion_id,
    )


def _run_finalize_batch_ingestions(
    session: SQLAlchemySession,
    *,
    ingestions_by_id: Mapping[UUID, ArtifactIngestion],
    parse_revision_by_ingestion_id: Mapping[UUID, UUID | None],
    completion_by_ingestion_id: Mapping[UUID, _IngestionCompletion],
) -> dict[UUID, list[TransitionStateInference]]:
    return _finalize_batch_ingestions(
        cast(Session, session),
        ingestions_by_id=ingestions_by_id,
        parse_revision_by_ingestion_id=parse_revision_by_ingestion_id,
        completion_by_ingestion_id=completion_by_ingestion_id,
    )


def _run_preload_batch_persistence_state(
    session: SQLAlchemySession,
    *,
    ingestion_ids: list[UUID],
) -> tuple[dict[UUID, ArtifactIngestion], dict[UUID, set[UUID]]]:
    # The unified upload path must retain the normal topology/DAG,
    # logical-reaction, and membership hooks.  Only defer trigger-side source
    # visibility recomputation inside this transaction; the durable worker
    # marks profiles dirty after the topology/reconciliation barrier below and
    # the separate profile worker refreshes them after this transaction commits.
    typed_session = cast(Session, session)
    # Source visibility is materialized by the same batch's reconciliation or
    # by the worker's queue-drain profile refresh.  The row-level PostgreSQL
    # triggers otherwise recompute the profile graph once for every frame,
    # ingestion, and profile-source row while the transaction is still
    # building the graph.  The LOCAL setting is scoped to this one database
    # transaction and the trigger functions fail back to their old behavior
    # for every other write path.
    typed_session.connection().execute(
        text("SET LOCAL tricycle.defer_profile_source_visibility = 'on'")
    )
    return _preload_batch_persistence_state(typed_session, ingestion_ids=ingestion_ids)


def _stored_result(artifact: ArtifactFile) -> ArtifactUploadResult:
    return ArtifactUploadResult(
        artifact_id=_require_id(artifact, label="ArtifactFile"),
        artifact_kind=artifact.artifact_kind,
        storage_status=artifact.storage_status,
        inferred_reaction_count=0,
        inferences=[],
    )


class ArtifactUploadService:
    """Authenticated content-addressed upload with optional calculation ingestion."""

    @classmethod
    async def _prepare_upload(
        cls,
        *,
        payload: bytes | Path,
        filename: str,
        media_type: str,
        artifact_kind: ArtifactKind,
        project_id: UUID,
        user_id: UUID,
        relative_path: str | None = None,
        expected_sha256: str | None = None,
        expected_size_bytes: int | None = None,
        inspected: _InspectedUploadSource | None = None,
    ) -> _PreparedCalculationUpload | ArtifactUploadResult:
        """Reserve and store an upload, leaving calculation parsing to the worker."""

        settings = RustFSSettings()
        started_at = datetime.now(UTC)
        if inspected is None:
            inspected = _inspect_upload_source(
                ArtifactUploadPayload(
                    filename=filename,
                    media_type=media_type,
                    payload=payload if isinstance(payload, bytes) else None,
                    spool_path=payload if isinstance(payload, Path) else None,
                ),
                maximum_size=get_settings().max_upload_bytes,
            )
        source = inspected.source
        digest = inspected.content_sha256
        size_bytes = inspected.size_bytes
        if not size_bytes:
            raise ArtifactUploadError("uploaded artifact is empty")
        if expected_size_bytes is not None and size_bytes != expected_size_bytes:
            raise ArtifactUploadConflictError("uploaded artifact does not match the manifest size")
        if expected_sha256 is not None and digest != expected_sha256.casefold():
            raise ArtifactUploadConflictError(
                "uploaded artifact does not match the manifest SHA-256"
            )
        source_relative_path = (
            normalize_relative_path(relative_path)
            if relative_path is not None
            else Path(filename).name
        )
        object_key = time_partitioned_content_addressed_key_for_sha256(
            digest,
            uploaded_at=started_at,
            prefix="uploads",
        )
        resolved_media_type = detect_artifact_media_type(
            filename,
            media_type,
            inspected.media_probe,
        )
        record = ArtifactFileRecord(
            project_id=project_id,
            created_by_user_id=user_id,
            visibility=ArtifactVisibility.PROJECT,
            bucket=settings.bucket,
            object_key=object_key,
            content_sha256=digest,
            size_bytes=size_bytes,
            original_filename=Path(filename).name,
            source_relative_path=source_relative_path,
            media_type=resolved_media_type,
            artifact_kind=artifact_kind,
            storage_status=StorageStatus.PENDING,
        )
        async with session_factory() as session:
            artifact, retired_reservation, check_existing_object = await session.run_sync(
                lambda sync_session: _prepare_pending_upload(
                    cast(Session, sync_session),
                    record=record,
                )
            )
            artifact_id = _require_id(artifact, label="ArtifactFile")
            object_key = artifact.object_key
            await session.commit()

        try:
            store_function = getattr(cls._store_payload, "__func__", cls._store_payload)
            if store_function is not _ORIGINAL_STORE_PAYLOAD:
                # Preserve the test/extension seam for callers that replace
                # the legacy four-argument store hook.
                stored = await asyncio.to_thread(
                    cls._store_payload,
                    settings,
                    object_key,
                    source,
                    resolved_media_type,
                    content_sha256=digest,
                    size_bytes=size_bytes,
                    check_existing_object=check_existing_object,
                )
            else:
                # Single-file and batch staging must share the same persistent
                # RustFS process pool. Creating a boto3 client and bucket
                # session per HTTP upload makes many small files look serial;
                # the child-local store keeps the connection warm across all
                # upload sessions.
                storage_pool = _get_storage_process_pool(get_settings().upload_max_concurrency)
                loop = asyncio.get_running_loop()
                stored = await _await_cancellation_safe(
                    loop.run_in_executor(
                        storage_pool,
                        _store_payload_worker,
                        settings,
                        object_key,
                        source,
                        resolved_media_type,
                        digest if isinstance(source, Path) else None,
                        size_bytes if isinstance(source, Path) else None,
                        check_existing_object,
                    )
                )
            if stored.size != size_bytes or stored.sha256 != digest:
                raise ArtifactUploadError(
                    f"RustFS metadata mismatch for s3://{stored.bucket}/{stored.key}"
                )
            async with session_factory() as session:
                artifact = await session.run_sync(
                    lambda sync_session: _mark_upload_available(
                        cast(Session, sync_session),
                        artifact_id=artifact_id,
                        object_key=object_key,
                        stored=stored,
                    )
                )
                if artifact_kind is not ArtifactKind.CALCULATION_OUTPUT:
                    await session.commit()
                    return _stored_result(artifact)
                ingestion, created = await session.run_sync(
                    lambda sync_session: _create_pending_ingestion(
                        cast(Session, sync_session),
                        artifact=artifact,
                        started_at=started_at,
                    )
                )
                await session.commit()
                ingestion_id = _require_id(ingestion, label="ArtifactIngestion")
                if not created and ingestion.status is not ArtifactIngestionStatus.PENDING:
                    return await session.run_sync(
                        lambda sync_session: _result(
                            cast(Session, sync_session),
                            ingestion_id,
                            parse_revision_created=False,
                        )
                    )
        except Exception:
            await _compensate_upload(
                settings=settings,
                artifact_id=artifact_id,
                object_key=object_key,
                content_sha256=digest,
                retired_reservation=retired_reservation,
            )
            raise
        return _PreparedCalculationUpload(
            settings=settings,
            artifact_id=artifact_id,
            object_key=object_key,
            ingestion_id=ingestion_id,
            started_at=started_at,
            source=source,
            size_bytes=size_bytes,
            media_type=resolved_media_type,
            content_sha256=digest,
            retired_reservation=retired_reservation,
            needs_storage=True,
            check_existing_object=check_existing_object,
        )

    @classmethod
    async def stage_source(
        cls,
        *,
        source: bytes | Path,
        filename: str,
        media_type: str,
        artifact_kind: ArtifactKind,
        project_id: UUID,
        user_id: UUID,
        relative_path: str | None = None,
        expected_sha256: str | None = None,
        expected_size_bytes: int | None = None,
    ) -> ArtifactUploadResult:
        """Stage bytes or an on-disk source without starting MolOP.

        Local importers and multipart handlers may already own a spool file.
        Inspect its identity once, then stream that same path directly to
        RustFS.  The path is never sent to MolOP from this request; the durable
        queue worker downloads the stored object and owns the later parse.
        """

        await AuthorizationService.require_project_permission(
            user_id,
            project_id,
            ProjectPermission.ARTIFACT_UPLOAD,
        )
        inspected = await asyncio.to_thread(
            _inspect_upload_source,
            ArtifactUploadPayload(
                filename=filename,
                media_type=media_type,
                payload=source if isinstance(source, bytes) else None,
                spool_path=source if isinstance(source, Path) else None,
            ),
            maximum_size=get_settings().max_upload_bytes,
        )
        prepared = await cls._prepare_upload(
            payload=source,
            filename=filename,
            media_type=media_type,
            artifact_kind=artifact_kind,
            project_id=project_id,
            user_id=user_id,
            relative_path=relative_path,
            expected_sha256=expected_sha256,
            expected_size_bytes=expected_size_bytes,
            inspected=inspected,
        )
        if isinstance(prepared, ArtifactUploadResult):
            return prepared
        ingestion_id = _require_prepared_ingestion_id(prepared)
        async with session_factory() as session:
            return await session.run_sync(
                lambda sync_session: _result(
                    cast(Session, sync_session),
                    ingestion_id,
                    parse_revision_created=False,
                )
            )

    @classmethod
    async def parse_staged_artifact(cls, artifact_id: UUID) -> ParsedArtifactTask:
        """Download and parse one claimed RustFS object without touching SQL rows."""

        started_at = datetime.now(UTC)
        try:
            async with session_factory() as session:
                artifact = await session.get(ArtifactFile, artifact_id)
            if artifact is None:
                raise ArtifactUploadError("artifact not found")
            if artifact.artifact_kind is not ArtifactKind.CALCULATION_OUTPUT:
                raise ArtifactUploadError("only calculation output artifacts can be parsed")
            if artifact.storage_status is not StorageStatus.AVAILABLE:
                raise ArtifactUploadError("artifact bytes are not available for parsing")
            with tempfile.TemporaryDirectory(prefix="tricycle-staged-parse-") as directory:
                parser_source = Path(directory) / _safe_parser_suffix(artifact.original_filename)
                async with _rustfs_download_submission_slots():
                    storage_pool = _get_storage_process_pool(get_settings().upload_max_concurrency)
                    loop = asyncio.get_running_loop()
                    await _await_cancellation_safe(
                        loop.run_in_executor(
                            storage_pool,
                            _download_payload_worker,
                            RustFSSettings().model_copy(update={"bucket": artifact.bucket}),
                            artifact.object_key,
                            parser_source,
                            artifact.size_bytes,
                            artifact.content_sha256,
                        )
                    )
                parsed = await _run_molop_file_pipeline(
                    parser_source,
                    artifact.original_filename,
                    artifact_sha256=artifact.content_sha256,
                )
            return ParsedArtifactTask(artifact_id=artifact_id, started_at=started_at, parsed=parsed)
        except Exception as error:
            return ParsedArtifactTask(artifact_id=artifact_id, started_at=started_at, parsed=error)

    @classmethod
    async def _prepare_preparsed_batch(
        cls,
        *,
        parsed_tasks: Sequence[ParsedArtifactTask],
        project_id: UUID,
        worker_lease_by_artifact_id: Mapping[UUID, UUID] | None,
    ) -> tuple[
        list[ArtifactUploadPayload],
        dict[int, _PreparedCalculationUpload],
        dict[int, ParsedArtifactTask],
        dict[UUID, Exception],
    ]:
        """Build upload reservations around parser results already in memory."""

        ordered_tasks = list({task.artifact_id: task for task in parsed_tasks}.values())
        if not ordered_tasks:
            return [], {}, {}, {}
        artifact_ids = tuple(task.artifact_id for task in ordered_tasks)
        async with session_factory() as session:
            artifacts = (
                await session.exec(
                    select(ArtifactFile).where(col(ArtifactFile.id).in_(artifact_ids))
                )
            ).all()
            ingestions = (
                await session.exec(
                    select(ArtifactIngestion).where(
                        col(ArtifactIngestion.artifact_file_id).in_(artifact_ids)
                    )
                )
            ).all()
        artifacts_by_id = {
            artifact.id: artifact for artifact in artifacts if artifact.id is not None
        }
        ingestions_by_artifact_id = {
            ingestion.artifact_file_id: ingestion for ingestion in ingestions
        }
        files: list[ArtifactUploadPayload] = []
        prepared: dict[int, _PreparedCalculationUpload] = {}
        preparsed_by_index: dict[int, ParsedArtifactTask] = {}
        errors: dict[UUID, Exception] = {}
        for task in ordered_tasks:
            artifact = artifacts_by_id.get(task.artifact_id)
            if artifact is None:
                errors[task.artifact_id] = ArtifactUploadError("artifact not found")
                continue
            if artifact.project_id != project_id:
                errors[task.artifact_id] = ArtifactUploadError(
                    "parsed artifact belongs to a different project"
                )
                continue
            if artifact.artifact_kind is not ArtifactKind.CALCULATION_OUTPUT:
                errors[task.artifact_id] = ArtifactUploadError(
                    "only calculation output artifacts can be persisted"
                )
                continue
            ingestion = ingestions_by_artifact_id.get(task.artifact_id)
            if ingestion is None or ingestion.id is None:
                errors[task.artifact_id] = ArtifactUploadError("artifact ingestion not found")
                continue
            expected_lease = (
                worker_lease_by_artifact_id.get(task.artifact_id)
                if worker_lease_by_artifact_id is not None
                else None
            )
            if expected_lease is not None and (
                ingestion.status is not ArtifactIngestionStatus.PROCESSING
                or ingestion.worker_lease_id != expected_lease
            ):
                errors[task.artifact_id] = ArtifactUploadError(
                    "artifact processing lease is no longer current"
                )
                continue
            index = len(files)
            files.append(
                ArtifactUploadPayload(
                    filename=artifact.original_filename,
                    media_type=artifact.media_type,
                    payload=None,
                )
            )
            prepared[index] = _PreparedCalculationUpload(
                settings=RustFSSettings().model_copy(update={"bucket": artifact.bucket}),
                artifact_id=task.artifact_id,
                object_key=artifact.object_key,
                ingestion_id=ingestion.id,
                started_at=task.started_at,
                source=b"",
                size_bytes=artifact.size_bytes,
                media_type=artifact.media_type,
                content_sha256=artifact.content_sha256,
                needs_storage=False,
                check_existing_object=False,
                force_new_revision=True,
                ingestion_status=ingestion.status,
            )
            preparsed_by_index[index] = task
        return files, prepared, preparsed_by_index, errors

    @classmethod
    async def persist_parsed_microbatch(
        cls,
        parsed_tasks: Sequence[ParsedArtifactTask],
        *,
        project_id: UUID,
        user_id: UUID,
        worker_lease_by_artifact_id: Mapping[UUID, UUID] | None = None,
        persistence_batch_files: int | None = None,
        persistence_frame_limit: int | None = None,
        defer_thermodynamic_refresh: bool = True,
    ) -> dict[UUID, ArtifactUploadResult | Exception]:
        """Persist parser results through one bounded SQLAlchemy consumer."""

        (
            files,
            prepared,
            preparsed_by_index,
            preparation_errors,
        ) = await cls._prepare_preparsed_batch(
            parsed_tasks=parsed_tasks,
            project_id=project_id,
            worker_lease_by_artifact_id=worker_lease_by_artifact_id,
        )
        results: dict[UUID, ArtifactUploadResult | Exception] = dict(preparation_errors)
        if not files:
            return results
        resolved_batch_files = (
            persistence_batch_files or get_settings().upload_worker_persistence_batch_files
        )
        batch_result = await cls.upload_batch(
            files=files,
            artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
            project_id=project_id,
            user_id=user_id,
            persistence_batch_files=min(resolved_batch_files, len(files)),
            persistence_frame_limit=(
                persistence_frame_limit or get_settings().upload_worker_persistence_frame_limit
            ),
            defer_thermodynamic_refresh=defer_thermodynamic_refresh,
            _preparsed_tasks=preparsed_by_index,
            _prepared_uploads=prepared,
        )
        timings = batch_result.timings_ms
        logger.info(
            "upload worker persistence microbatch project=%s user=%s files=%d bytes=%d "
            "frames=%d succeeded=%d failed=%d total_ms=%.1f persist_wall_ms=%.1f "
            "persist_db_ms=%.1f preload_ms=%.1f write_ms=%.1f flush_ms=%.1f "
            "inference_ms=%.1f reconcile_ms=%.1f result_ms=%.1f commit_ms=%.1f",
            project_id,
            user_id,
            batch_result.total_count,
            sum(item.size_bytes for item in prepared.values()),
            batch_result.source_frame_count or 0,
            batch_result.succeeded_count,
            batch_result.failed_count,
            timings.get("total_ms", 0.0),
            timings.get("persist_pipeline_wall_ms", 0.0),
            timings.get("persist_db_ms", 0.0),
            timings.get("persist_preload_db_ms", 0.0),
            timings.get("persist_write_db_ms", 0.0),
            timings.get("persist_flush_initial_ms", 0.0),
            timings.get("persist_deferred_inferences_ms", 0.0),
            timings.get("persist_reconcile_geometry_ms", 0.0),
            timings.get("persist_result_db_ms", 0.0),
            timings.get("persist_commit_db_ms", 0.0),
        )
        for item, prepared_item in zip(batch_result.items, prepared.values(), strict=True):
            if item.result is not None:
                results[prepared_item.artifact_id] = item.result
            else:
                results[prepared_item.artifact_id] = ArtifactUploadError(
                    item.error_message or item.error_code or "artifact persistence failed"
                )
        return results

    @classmethod
    async def clear_previous_parse_results(
        cls,
        *,
        artifact_ids: Sequence[UUID],
        user_id: UUID,
        worker_lease_by_artifact_id: Mapping[UUID, tuple[UUID | None, datetime | None]]
        | None = None,
        refresh_statistics: bool = True,
        defer_thermodynamic_refresh: bool = True,
    ) -> ParseCleanupSummary:
        """Delete all materialized parse state before a clean reparse.

        The source artifact and its RustFS object remain intact. Every old
        ``ParseRevision`` and all revision-owned rows are removed in the same
        transaction, so a subsequent parse starts at revision number one.
        """

        ordered_ids = tuple(dict.fromkeys(artifact_ids))
        if not ordered_ids:
            return ParseCleanupSummary()
        started_at = datetime.now(UTC)
        async with session_factory() as session:
            artifacts = (
                await session.exec(
                    select(ArtifactFile).where(col(ArtifactFile.id).in_(ordered_ids))
                )
            ).all()
            artifacts_by_id = {
                artifact_id: artifact
                for artifact in artifacts
                if (artifact_id := artifact.id) is not None
            }
            missing = [
                artifact_id for artifact_id in ordered_ids if artifact_id not in artifacts_by_id
            ]
            if missing:
                raise ArtifactUploadError(f"artifact not found: {missing[0]}")
            project_ids: set[UUID] = set()
            for artifact in artifacts_by_id.values():
                if artifact.artifact_kind is not ArtifactKind.CALCULATION_OUTPUT:
                    raise ArtifactUploadError("only calculation output artifacts can be reparsed")
                if artifact.storage_status is not StorageStatus.AVAILABLE:
                    raise ArtifactUploadError("artifact bytes are not available for parse cleanup")
                project_ids.add(artifact.project_id)
            for project_id in project_ids:
                await AuthorizationService.require_project_permission(
                    user_id,
                    project_id,
                    ProjectPermission.ARTIFACT_UPLOAD,
                )
            cleanup = await session.run_sync(
                partial(
                    _run_clear_and_reset_parse_state,
                    artifact_file_ids=ordered_ids,
                    started_at=started_at,
                    worker_lease_by_artifact_id=worker_lease_by_artifact_id,
                    defer_thermodynamic_refresh=defer_thermodynamic_refresh,
                )
            )
            await session.commit()
        if refresh_statistics:
            await refresh_project_statistics(
                project_ids,
                reason="parse-materialization-clear",
            )
        return cleanup

    @classmethod
    async def _mark_cleared_reparse_failures(
        cls,
        failures: Mapping[UUID, Exception],
    ) -> None:
        """Mark artifacts failed after their old materialization was cleared."""

        if not failures:
            return
        async with session_factory() as session:
            ingestion_ids: dict[UUID, UUID] = {}
            for artifact_id in failures:
                ingestion_id = (
                    await session.exec(
                        select(ArtifactIngestion.id).where(
                            col(ArtifactIngestion.artifact_file_id) == artifact_id
                        )
                    )
                ).first()
                if isinstance(ingestion_id, UUID):
                    ingestion_ids[artifact_id] = ingestion_id
            for artifact_id, error in failures.items():
                ingestion_id = ingestion_ids.get(artifact_id)
                if ingestion_id is None:
                    continue
                await session.run_sync(
                    partial(
                        _run_mark_ingestion_failed,
                        ingestion_id=ingestion_id,
                        error=error,
                        error_code=getattr(error, "error_code", "artifact_storage_failed"),
                        completed_at=datetime.now(UTC),
                        error_metadata=_parse_failure_metadata(error),
                    )
                )
            await session.commit()

    @classmethod
    async def reparse_batch(
        cls,
        *,
        artifact_ids: Sequence[UUID],
        user_id: UUID,
        force_reparse: bool = False,
        previous_results_cleared: bool = False,
        refresh_statistics: bool = False,
        defer_thermodynamic_refresh: bool = True,
    ) -> dict[UUID, ArtifactUploadResult | Exception]:
        """Reparse staged objects through the compatibility batch pipeline.

        The normal durable worker uses ``parse_staged_artifact`` for continuous
        dispatch and ``persist_parsed_microbatch`` for the shared persistence
        consumer. This method remains for explicit administrative callers and
        older tests/scripts: it only downloads and verifies existing RustFS
        objects, then delegates MolOP, MolGR, and database work to
        ``upload_batch``. It does not create a parser process per file or
        upload session. ``defer_thermodynamic_refresh`` is used by the durable
        worker so the queue-level profile refresher, rather than each
        persistence microbatch, owns the expensive derived-profile rebuild.
        """

        ordered_ids = tuple(dict.fromkeys(artifact_ids))
        if not ordered_ids:
            return {}

        async with session_factory() as session:
            artifacts = (
                await session.exec(
                    select(ArtifactFile).where(col(ArtifactFile.id).in_(ordered_ids))
                )
            ).all()
        artifacts_by_id = {
            artifact_id: artifact
            for artifact in artifacts
            if (artifact_id := artifact.id) is not None
        }
        processing_lease_by_artifact_id: dict[UUID, tuple[UUID, datetime]] = {}
        results: dict[UUID, ArtifactUploadResult | Exception] = {}
        valid_artifacts: list[ArtifactFile] = []
        project_id: UUID | None = None
        for artifact_id in ordered_ids:
            artifact = artifacts_by_id.get(artifact_id)
            if artifact is None:
                results[artifact_id] = ArtifactUploadError("artifact not found")
                continue
            if artifact.artifact_kind is not ArtifactKind.CALCULATION_OUTPUT:
                results[artifact_id] = ArtifactUploadError(
                    "only calculation output artifacts can be reparsed"
                )
                continue
            if artifact.storage_status is not StorageStatus.AVAILABLE:
                results[artifact_id] = ArtifactUploadError(
                    "artifact bytes are not available for reparse"
                )
                continue
            if project_id is None:
                project_id = artifact.project_id
            elif artifact.project_id != project_id:
                results[artifact_id] = ArtifactUploadError(
                    "a reparse batch cannot span multiple projects"
                )
                continue
            valid_artifacts.append(artifact)

        if not valid_artifacts or project_id is None:
            return results

        settings = get_settings()
        artifact_chunks: list[list[ArtifactFile]] = []
        current_chunk: list[ArtifactFile] = []
        current_bytes = 0
        for artifact in valid_artifacts:
            if artifact.size_bytes > settings.max_batch_bytes:
                artifact_id = _require_id(artifact, label="ArtifactFile")
                results[artifact_id] = ArtifactUploadError(
                    "artifact exceeds the configured reparse batch byte budget"
                )
                continue
            if current_chunk and (
                len(current_chunk) >= settings.max_batch_files
                or current_bytes + artifact.size_bytes > settings.max_batch_bytes
            ):
                artifact_chunks.append(current_chunk)
                current_chunk = []
                current_bytes = 0
            current_chunk.append(artifact)
            current_bytes += artifact.size_bytes
        if current_chunk:
            artifact_chunks.append(current_chunk)

        if force_reparse and not previous_results_cleared:
            # Complete the destructive phase for every file before starting
            # any RustFS read or parser work. This gives a batch a real
            # clean-start boundary even when a later object is unavailable.
            active_lease_cutoff = datetime.now(UTC)
            async with session_factory() as session:
                ingestions = (
                    await session.exec(
                        select(ArtifactIngestion).where(
                            col(ArtifactIngestion.artifact_file_id).in_(ordered_ids)
                        )
                    )
                ).all()
            for ingestion in ingestions:
                if (
                    ingestion.status is ArtifactIngestionStatus.PROCESSING
                    and ingestion.worker_lease_id is not None
                    and ingestion.worker_lease_expires_at is not None
                    and ingestion.worker_lease_expires_at > active_lease_cutoff
                ):
                    processing_lease_by_artifact_id[ingestion.artifact_file_id] = (
                        ingestion.worker_lease_id,
                        ingestion.worker_lease_expires_at,
                    )
            for artifact_chunk in artifact_chunks:
                try:
                    await cls.clear_previous_parse_results(
                        artifact_ids=[
                            _require_id(artifact, label="ArtifactFile")
                            for artifact in artifact_chunk
                        ],
                        user_id=user_id,
                        worker_lease_by_artifact_id=processing_lease_by_artifact_id,
                        refresh_statistics=False,
                        defer_thermodynamic_refresh=defer_thermodynamic_refresh,
                    )
                except Exception as error:
                    for artifact in artifact_chunk:
                        results[_require_id(artifact, label="ArtifactFile")] = error
                    return results
            previous_results_cleared = True

        # The worker serializes project/user microbatches, but keep one
        # loop-wide gate here so RustFS reads remain bounded if this service is
        # also called by another durable consumer or an explicit batch retry.
        download_slots = _rustfs_download_submission_slots()

        async def load_payload(artifact: ArtifactFile) -> ArtifactUploadPayload | Exception:
            artifact_id = _require_id(artifact, label="ArtifactFile")
            try:
                async with download_slots:
                    payload = await asyncio.to_thread(
                        cls._load_payload,
                        RustFSSettings().model_copy(update={"bucket": artifact.bucket}),
                        artifact.object_key,
                    )
                if len(payload) != artifact.size_bytes:
                    raise ArtifactUploadError(
                        "stored artifact bytes do not match database identity"
                    )
                if sha256(payload).hexdigest() != artifact.content_sha256:
                    raise ArtifactUploadError(
                        "stored artifact bytes do not match database identity"
                    )
                _require_upload_size(payload)
                return ArtifactUploadPayload(
                    filename=artifact.original_filename,
                    media_type=artifact.media_type,
                    payload=payload,
                    relative_path=artifact.source_relative_path,
                    expected_sha256=artifact.content_sha256,
                    expected_size_bytes=artifact.size_bytes,
                )
            except Exception as error:
                results[artifact_id] = error
                return error

        for artifact_chunk in artifact_chunks:
            loaded = await asyncio.gather(*(load_payload(artifact) for artifact in artifact_chunk))
            failed_loads = {
                _require_id(artifact, label="ArtifactFile"): payload_or_error
                for artifact, payload_or_error in zip(artifact_chunk, loaded, strict=True)
                if isinstance(payload_or_error, Exception)
            }
            if previous_results_cleared and failed_loads:
                await cls._mark_cleared_reparse_failures(failed_loads)
            upload_files: list[tuple[UUID, ArtifactUploadPayload]] = []
            upload_artifacts: list[ArtifactFile] = []
            for artifact, payload_or_error in zip(artifact_chunk, loaded, strict=True):
                if isinstance(payload_or_error, Exception):
                    continue
                upload_artifacts.append(artifact)
                upload_files.append((_require_id(artifact, label="ArtifactFile"), payload_or_error))
            if not upload_files:
                continue
            try:
                batch_result = await cls.upload_batch(
                    files=[payload for _, payload in upload_files],
                    artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                    project_id=project_id,
                    user_id=user_id,
                    # Keep the compatibility path on the same bounded
                    # persistence boundary as the streaming worker. The
                    # production worker itself already hands this method one
                    # parsed microbatch at a time.
                    persistence_batch_files=PERSISTENCE_PRELOAD_BATCH_SIZE,
                    reparse_failed_ingestions=True,
                    force_reparse=force_reparse,
                    previous_results_cleared=previous_results_cleared,
                    defer_thermodynamic_refresh=defer_thermodynamic_refresh,
                )
            except Exception as error:
                for artifact in upload_artifacts:
                    results[_require_id(artifact, label="ArtifactFile")] = error
                continue
            logger.warning(
                "reparse batch completed project=%s files=%d bytes=%d succeeded=%d failed=%d "
                "total_ms=%.1f parse_ms=%.1f molgr_ms=%.1f persist_wall_ms=%.1f "
                "persist_db_ms=%.1f persist_parts_ms=preload:%.1f write:%.1f "
                "flush:%.1f deferred:%.1f reconcile:%.1f result:%.1f commit:%.1f "
                "locks=%d/%d/%d",
                project_id,
                batch_result.total_count,
                sum(artifact.size_bytes for artifact in upload_artifacts),
                batch_result.succeeded_count,
                batch_result.failed_count,
                batch_result.timings_ms.get("total_ms", 0.0),
                batch_result.timings_ms.get("molop_parse_ms", 0.0),
                batch_result.timings_ms.get("molgr_frame_reconstruction_ms", 0.0),
                batch_result.timings_ms.get("persist_pipeline_wall_ms", 0.0),
                batch_result.timings_ms.get("persist_db_ms", 0.0),
                batch_result.timings_ms.get("persist_preload_db_ms", 0.0),
                batch_result.timings_ms.get("persist_write_db_ms", 0.0),
                batch_result.timings_ms.get("persist_flush_initial_ms", 0.0),
                batch_result.timings_ms.get("persist_deferred_inferences_ms", 0.0),
                batch_result.timings_ms.get("persist_reconcile_geometry_ms", 0.0),
                batch_result.timings_ms.get("persist_result_db_ms", 0.0),
                batch_result.timings_ms.get("persist_commit_db_ms", 0.0),
                int(batch_result.timings_ms.get("advisory_lock_calls", 0.0)),
                int(batch_result.timings_ms.get("advisory_lock_requested_ids", 0.0)),
                int(batch_result.timings_ms.get("advisory_lock_uncached_ids", 0.0)),
            )
            for artifact, item in zip(upload_artifacts, batch_result.items, strict=True):
                artifact_id = _require_id(artifact, label="ArtifactFile")
                if item.result is not None:
                    results[artifact_id] = item.result
                else:
                    results[artifact_id] = ArtifactUploadError(
                        item.error_message or item.error_code or "artifact reparse failed"
                    )
        if refresh_statistics:
            await refresh_project_statistics(
                (project_id,),
                reason="artifact-reparse-complete",
            )
        return results

    @classmethod
    async def fail_pending_ingestion(
        cls,
        *,
        ingestion_id: UUID,
        lease_id: UUID,
        error: Exception,
    ) -> None:
        """Record a recovery precondition failure without stealing a new lease."""

        async with session_factory() as session:
            await session.run_sync(
                lambda sync_session: _mark_ingestion_failed(
                    cast(Session, sync_session),
                    ingestion_id=ingestion_id,
                    error=error,
                    error_code=getattr(error, "error_code", "pending_ingestion_recovery_failed"),
                    completed_at=datetime.now(UTC),
                    expected_worker_lease_id=lease_id,
                )
            )
            await session.commit()

    @classmethod
    async def validate(
        cls,
        *,
        payload: bytes,
        filename: str,
        project_id: UUID,
        user_id: UUID,
    ) -> ArtifactValidationResult:
        """Probe and normalize a calculation artifact without storing any data."""

        if not payload:
            raise ArtifactUploadError("uploaded artifact is empty")
        _require_upload_size(payload)
        await AuthorizationService.require_project_permission(
            user_id,
            project_id,
            ProjectPermission.ARTIFACT_UPLOAD,
        )
        parsed = await _run_molop_file_pipeline(payload, filename)
        inferences = [
            ArtifactValidationInferenceView(
                file_frame_index=inference.file_frame_index,
                imaginary_mode_index=inference.imaginary_mode_index,
                imaginary_frequency_cm1=inference.imaginary_frequency_cm1,
                succeeded=isinstance(inference, _SuccessfulInference),
                reaction_smiles=(
                    inference.reaction_smiles
                    if isinstance(inference, _SuccessfulInference)
                    else None
                ),
                error_code=(
                    inference.error_code if isinstance(inference, _FailedInference) else None
                ),
                error_message=(
                    inference.error_message if isinstance(inference, _FailedInference) else None
                ),
                error_metadata_json=(
                    json.dumps(inference.error_metadata, sort_keys=True)
                    if isinstance(inference, _FailedInference)
                    and inference.error_metadata is not None
                    else None
                ),
            )
            for inference in parsed.inferences
        ]
        successful_count = sum(inference.succeeded for inference in inferences)
        return ArtifactValidationResult(
            filename=Path(filename).name,
            source_format=parsed.source_format,
            source_compression=parsed.source_compression,
            source_frame_count=parsed.source_frame_count,
            transition_state_frame_count=len(inferences),
            successful_inference_count=successful_count,
            failed_inference_count=len(inferences) - successful_count,
            inferences=inferences,
        )

    @classmethod
    async def _prepare_upload_batch(
        cls,
        *,
        files: list[ArtifactUploadPayload],
        artifact_kind: ArtifactKind,
        project_id: UUID,
        user_id: UUID,
        source_inspections: Mapping[int, _InspectedUploadSource] | None = None,
        reparse_failed_ingestions: bool = False,
        force_reparse: bool = False,
        previous_results_cleared: bool = False,
        defer_thermodynamic_refresh: bool = True,
    ) -> tuple[
        dict[int, _PreparedCalculationUpload],
        dict[int, ArtifactBatchUploadItem],
    ]:
        """Create all durable upload reservations in one PostgreSQL transaction.

        The object store is deliberately outside this transaction. PostgreSQL
        records the complete pending set first, then one follow-up transaction
        advances every verified object and creates/refreshes all ingestion rows.
        This keeps retry ownership durable without paying a commit per file.
        """

        settings = RustFSSettings()
        candidates: list[
            tuple[int, ArtifactUploadPayload, _InspectedUploadSource, ArtifactFileRecord, datetime]
        ] = []
        candidate_by_digest: dict[str, int] = {}
        duplicate_of: dict[int, int] = {}
        items: dict[int, ArtifactBatchUploadItem] = {}
        for index, file in enumerate(files):
            if file.payload is None and file.spool_path is None:
                items[index] = ArtifactBatchUploadItem(
                    filename=file.filename,
                    succeeded=False,
                    error_code=file.error_code or "invalid_upload",
                    error_message=file.error_message or "uploaded file is invalid",
                )
                continue
            try:
                inspected = (
                    source_inspections[index]
                    if source_inspections is not None
                    else _inspect_upload_source(
                        file,
                        maximum_size=get_settings().max_upload_bytes,
                    )
                )
                if not inspected.size_bytes:
                    raise ArtifactUploadError("uploaded artifact is empty")
                if (
                    file.expected_size_bytes is not None
                    and inspected.size_bytes != file.expected_size_bytes
                ):
                    raise ArtifactUploadConflictError(
                        "uploaded artifact does not match the manifest size"
                    )
                if (
                    file.expected_sha256 is not None
                    and inspected.content_sha256 != file.expected_sha256.casefold()
                ):
                    raise ArtifactUploadConflictError(
                        "uploaded artifact does not match the manifest SHA-256"
                    )
                started_at = datetime.now(UTC)
                resolved_media_type = detect_artifact_media_type(
                    file.filename,
                    file.media_type,
                    inspected.media_probe,
                )
                record = ArtifactFileRecord(
                    project_id=project_id,
                    created_by_user_id=user_id,
                    visibility=ArtifactVisibility.PROJECT,
                    bucket=settings.bucket,
                    object_key=time_partitioned_content_addressed_key_for_sha256(
                        inspected.content_sha256,
                        uploaded_at=started_at,
                        prefix="uploads",
                    ),
                    content_sha256=inspected.content_sha256,
                    size_bytes=inspected.size_bytes,
                    original_filename=Path(file.filename).name,
                    source_relative_path=(
                        normalize_relative_path(file.relative_path)
                        if file.relative_path is not None
                        else Path(file.filename).name
                    ),
                    media_type=resolved_media_type,
                    artifact_kind=artifact_kind,
                    storage_status=StorageStatus.PENDING,
                )
                first_index = candidate_by_digest.setdefault(inspected.content_sha256, index)
                if first_index != index:
                    duplicate_of[index] = first_index
                else:
                    candidates.append((index, file, inspected, record, started_at))
            except Exception as error:
                items[index] = ArtifactBatchUploadItem(
                    filename=file.filename,
                    succeeded=False,
                    error_code="artifact_upload_failed",
                    error_message=str(error) or type(error).__name__,
                )

        reservations: dict[int, _PreparedCalculationUpload] = {}
        if not candidates:
            return reservations, items

        # Preparation acquires one content lock per unique source and, for
        # calculation outputs, one ingestion lock per artifact.  The durable
        # worker reparses an already-materialized claim, so it should retain
        # the previous single preparation transaction for that claim.  New
        # upload requests can still be split into small transactions before
        # the object-store/parser pipeline starts.
        preparation_batch_size = (
            len(candidates) if reparse_failed_ingestions else PERSISTENCE_PRELOAD_BATCH_SIZE
        )
        for offset in range(0, len(candidates), preparation_batch_size):
            candidate_batch = candidates[offset : offset + preparation_batch_size]
            async with session_factory() as session:
                previous_fast_insert = session.info.get("tricycle_fast_insert", False)
                previous_autoflush = session.autoflush
                session.info["tricycle_fast_insert"] = True
                session.autoflush = False
                try:
                    reservations_by_digest = await session.run_sync(
                        partial(
                            _run_prepare_pending_uploads,
                            records=[record for _, _, _, record, _ in candidate_batch],
                        )
                    )
                    artifacts_by_digest = {
                        digest: artifact
                        for digest, (artifact, _retired, _check_existing) in (
                            reservations_by_digest.items()
                        )
                    }
                    clean_reparse = artifact_kind is ArtifactKind.CALCULATION_OUTPUT and (
                        reparse_failed_ingestions or force_reparse
                    )
                    cleanup: ParseCleanupSummary | None = None
                    if clean_reparse and not previous_results_cleared:
                        # ``clear_previous_parse_results_batch`` expires the
                        # session after its set-based deletes.  Attach and
                        # flush newly reserved ArtifactFile rows first;
                        # otherwise expire_all() can discard their pending
                        # fast-insert state before the reparse path has a
                        # chance to create their ingestion rows.
                        await session.run_sync(_run_flush)
                        artifact_ids_by_digest = {
                            digest: _require_id(artifact, label="ArtifactFile")
                            for digest, artifact in artifacts_by_digest.items()
                        }
                        cleanup = await session.run_sync(
                            partial(
                                _run_clear_and_reset_parse_state,
                                artifact_file_ids=tuple(artifact_ids_by_digest.values()),
                                started_at=datetime.now(UTC),
                                defer_thermodynamic_refresh=defer_thermodynamic_refresh,
                            )
                        )
                        if cleanup.deleted_revision_count:
                            # The set-based cleanup expires all ORM state after
                            # deleting old revisions. Rehydrate the artifact
                            # holders before the rest of this preparation
                            # transaction reads their IDs/status; otherwise an
                            # async attribute access attempts a lazy load
                            # outside ``greenlet_spawn``.
                            refreshed_artifacts = (
                                await session.exec(
                                    select(ArtifactFile).where(
                                        col(ArtifactFile.id).in_(
                                            tuple(artifact_ids_by_digest.values())
                                        )
                                    )
                                )
                            ).all()
                            refreshed_by_id = {
                                artifact.id: artifact
                                for artifact in refreshed_artifacts
                                if isinstance(artifact.id, UUID)
                            }
                            if len(refreshed_by_id) != len(artifact_ids_by_digest):
                                raise ArtifactUploadError(
                                    "artifact reservation disappeared after parse cleanup"
                                )
                            artifacts_by_digest = {
                                digest: refreshed_by_id[artifact_id]
                                for digest, artifact_id in artifact_ids_by_digest.items()
                            }
                            reservations_by_digest = {
                                digest: (
                                    artifacts_by_digest[digest],
                                    retired_reservation,
                                    check_existing_object,
                                )
                                for digest, (
                                    _artifact,
                                    retired_reservation,
                                    check_existing_object,
                                ) in reservations_by_digest.items()
                            }
                    ingestions_by_artifact_id: dict[UUID, tuple[ArtifactIngestion, bool]] = {}
                    if artifact_kind is ArtifactKind.CALCULATION_OUTPUT:
                        started_by_artifact_id = {
                            _require_id(
                                artifacts_by_digest[record.content_sha256],
                                label="ArtifactFile",
                            ): started_at
                            for _, _, _, record, started_at in candidate_batch
                        }
                        ingestions_by_artifact_id = await session.run_sync(
                            partial(
                                _run_create_pending_ingestions,
                                artifacts=list(artifacts_by_digest.values()),
                                started_by_artifact_id=started_by_artifact_id,
                            )
                        )
                    for index, _file, inspected, record, started_at in candidate_batch:
                        artifact, retired_reservation, check_existing_object = (
                            reservations_by_digest[record.content_sha256]
                        )
                        artifact_id = _require_id(artifact, label="ArtifactFile")
                        ingestion_id: UUID | None = None
                        skip_parse = False
                        force_new_revision = False
                        ingestion_status: ArtifactIngestionStatus | None = None
                        if artifact_kind is ArtifactKind.CALCULATION_OUTPUT:
                            ingestion, created = ingestions_by_artifact_id[artifact_id]
                            ingestion_id = _require_id(ingestion, label="ArtifactIngestion")
                            ingestion_status = ingestion.status
                            retry_failed = (
                                (reparse_failed_ingestions or force_reparse)
                                and not created
                                and ingestion.status is not ArtifactIngestionStatus.PROCESSING
                                and (
                                    force_reparse
                                    or ingestion.status
                                    in {
                                        ArtifactIngestionStatus.FAILED,
                                        ArtifactIngestionStatus.PARTIAL,
                                    }
                                )
                            )
                            if retry_failed:
                                ingestion.status = ArtifactIngestionStatus.PENDING
                                ingestion.started_at = started_at
                                ingestion.completed_at = None
                                ingestion.source_frame_count = None
                                ingestion.transition_state_frame_count = None
                                ingestion.error_code = None
                                ingestion.error_message = None
                                ingestion.worker_lease_id = uuid4()
                                ingestion.worker_lease_expires_at = started_at + timedelta(
                                    seconds=get_settings().upload_worker_lease_seconds
                                )
                                session.add(ingestion)
                                ingestion_status = ArtifactIngestionStatus.PENDING
                                force_new_revision = True
                            if clean_reparse and not previous_results_cleared:
                                _reset_ingestion_for_clean_reparse(
                                    ingestion,
                                    started_at=started_at,
                                    status=(
                                        ArtifactIngestionStatus.PROCESSING
                                        if ingestion.status is ArtifactIngestionStatus.PROCESSING
                                        else ArtifactIngestionStatus.PENDING
                                    ),
                                    worker_lease_id=ingestion.worker_lease_id,
                                    worker_lease_expires_at=ingestion.worker_lease_expires_at,
                                    cleanup=cleanup,
                                )
                                session.add(ingestion)
                                ingestion_status = ingestion.status
                                force_new_revision = True
                            skip_parse = (
                                not created
                                and not retry_failed
                                and ingestion.status
                                not in {
                                    ArtifactIngestionStatus.PENDING,
                                    ArtifactIngestionStatus.PROCESSING,
                                }
                            )
                        reservations[index] = _PreparedCalculationUpload(
                            settings=settings,
                            artifact_id=artifact_id,
                            object_key=artifact.object_key,
                            ingestion_id=ingestion_id,
                            started_at=started_at,
                            source=inspected.source,
                            size_bytes=inspected.size_bytes,
                            media_type=record.media_type,
                            content_sha256=record.content_sha256,
                            retired_reservation=retired_reservation,
                            needs_storage=artifact.storage_status is not StorageStatus.AVAILABLE,
                            check_existing_object=check_existing_object,
                            skip_parse=skip_parse,
                            force_new_revision=force_new_revision,
                            ingestion_status=ingestion_status,
                            duplicate_of=None,
                        )
                    await session.run_sync(_run_flush)
                    await session.commit()
                finally:
                    session.autoflush = previous_autoflush
                    session.info["tricycle_fast_insert"] = previous_fast_insert

        # Duplicate content identities reuse the first reservation and object
        # key; no extra INSERT/UPDATE or identity lock is needed for a sibling.
        for index, first_index in duplicate_of.items():
            source = reservations[first_index]
            reservations[index] = _PreparedCalculationUpload(
                settings=source.settings,
                artifact_id=source.artifact_id,
                object_key=source.object_key,
                ingestion_id=source.ingestion_id,
                started_at=source.started_at,
                source=source.source,
                size_bytes=source.size_bytes,
                media_type=source.media_type,
                content_sha256=source.content_sha256,
                retired_reservation=None,
                needs_storage=False,
                check_existing_object=False,
                skip_parse=True,
                force_new_revision=False,
                ingestion_status=source.ingestion_status,
                duplicate_of=first_index,
            )
        return reservations, items

    @staticmethod
    def _batch_result_for_stored_artifact(
        reservation: _PreparedCalculationUpload,
        *,
        artifact_kind: ArtifactKind,
    ) -> ArtifactUploadResult:
        return ArtifactUploadResult(
            artifact_id=reservation.artifact_id,
            artifact_kind=artifact_kind,
            storage_status=StorageStatus.AVAILABLE,
            ingestion_id=reservation.ingestion_id,
            ingestion_status=reservation.ingestion_status,
            inferred_reaction_count=0,
            inferences=[],
        )

    @classmethod
    async def upload_batch(
        cls,
        *,
        files: list[ArtifactUploadPayload],
        artifact_kind: ArtifactKind,
        project_id: UUID,
        user_id: UUID,
        on_file_parsed: Callable[[int, bool], Awaitable[None]] | None = None,
        on_file_committed: Callable[[int, ArtifactBatchUploadItem], Awaitable[None]] | None = None,
        streaming: bool = False,
        persistence_batch_files: int = PERSISTENCE_PRELOAD_BATCH_SIZE,
        persistence_frame_limit: int = PERSISTENCE_BATCH_FRAME_LIMIT,
        enforce_batch_file_limit: bool = True,
        reparse_failed_ingestions: bool = False,
        force_reparse: bool = False,
        previous_results_cleared: bool = False,
        defer_thermodynamic_refresh: bool = True,
        _preparsed_tasks: Mapping[int, ParsedArtifactTask] | None = None,
        _prepared_uploads: Mapping[int, _PreparedCalculationUpload] | None = None,
    ) -> ArtifactBatchUploadResult:
        """Prepare once, then advance files through an asynchronous pipeline.

        Each RustFS completion queues that file behind the shared file-worker
        limit and submits it to the reusable MolOP process pool. A single
        bounded consumer writes parse results to the database; the parser
        queue stays shared while database commits are bounded to release
        project-scoped advisory locks. Geometry/reaction reconciliation runs
        once per claim-sized context, after its deferred reaction rows have
        been flushed. ``reparse_failed_ingestions`` reopens existing failed or
        partial parse records so a retry runs MolOP instead of only confirming
        the stored content-addressed object. Set
        ``defer_thermodynamic_refresh`` when a caller owns a later queue-drain
        profile refresh.
        """

        if persistence_batch_files < 1:
            raise ValueError("persistence_batch_files must be positive")
        if persistence_frame_limit < 1:
            raise ValueError("persistence_frame_limit must be positive")
        timings: dict[str, float] = {}
        started = perf_counter()
        if streaming and any(file.payload is not None for file in files):
            raise ArtifactUploadError(
                "streaming upload mode requires on-disk spool paths, not in-memory payloads"
            )
        if (_preparsed_tasks is None) != (_prepared_uploads is None):
            raise ValueError("preparsed tasks and prepared uploads must be supplied together")
        if _preparsed_tasks is not None:
            source_inspections: Mapping[int, _InspectedUploadSource] = {}
        else:
            # Local CLI imports pass on-disk paths and use the bounded pipeline
            # as the resource limit. Their files must not be split merely
            # because the aggregate source size crosses the HTTP request budget.
            source_inspections = _require_batch_upload_budget(
                files,
                enforce_batch_files=enforce_batch_file_limit,
                enforce_batch_bytes=not streaming,
            )
        timings["validate_budget_ms"] = (perf_counter() - started) * 1000
        phase_started = perf_counter()
        await AuthorizationService.require_project_permission(
            user_id,
            project_id,
            ProjectPermission.ARTIFACT_UPLOAD,
        )
        timings["authorize_ms"] = (perf_counter() - phase_started) * 1000

        phase_started = perf_counter()
        if _prepared_uploads is not None:
            prepared = dict(_prepared_uploads)
            item_by_index: dict[int, ArtifactBatchUploadItem] = {}
        else:
            prepared, item_by_index = await cls._prepare_upload_batch(
                files=files,
                artifact_kind=artifact_kind,
                project_id=project_id,
                user_id=user_id,
                source_inspections=source_inspections,
                reparse_failed_ingestions=reparse_failed_ingestions,
                force_reparse=force_reparse,
                previous_results_cleared=previous_results_cleared,
                defer_thermodynamic_refresh=defer_thermodynamic_refresh,
            )
        timings["prepare_db_ms"] = (perf_counter() - phase_started) * 1000

        stored: dict[int, Any] = {}

        async def recover_aborted_batch(error: BaseException) -> None:
            await _recover_aborted_batch(
                prepared=prepared,
                stored=stored,
                error=error,
            )

        storage_errors: dict[int, Exception] = {}
        phase_started = perf_counter()
        try:
            # Preparation commits the pending reservations before this stage.
            # Keep pool startup inside the recovery boundary so an executor
            # initialization failure cannot strand those rows indefinitely.
            # A worker microbatch supplies already staged/parser-owned files;
            # it must not start an unused RustFS process pool.
            storage_pool = (
                _get_storage_process_pool(get_settings().upload_max_concurrency)
                if any(reservation.needs_storage for reservation in prepared.values())
                else None
            )
        except BaseException as error:
            await _await_cancellation_safe(recover_aborted_batch(error))
            raise
        frame_submission_slots = _frame_worker_submission_slots()
        storage_phase_finished_at: float | None = None
        storage_completed_count = 0
        storage_total_count = sum(
            1 for reservation in prepared.values() if reservation.needs_storage
        )
        storage_completion_lock = asyncio.Lock()
        parse_phase_started_at: float | None = None
        parse_phase_finished_at: float | None = None
        molop_file_parse_phase_started_at: float | None = None
        molop_file_parse_phase_finished_at: float | None = None
        molop_file_parse_elapsed_ms = 0.0
        molgr_reconstruction_phase_started_at: float | None = None
        molgr_reconstruction_phase_finished_at: float | None = None
        molgr_reconstruction_elapsed_ms = 0.0

        async def process_one(
            index: int,
            reservation: _PreparedCalculationUpload,
        ) -> tuple[int, Exception | None, _ParsedArtifact | Exception | None]:
            """Advance one file through RustFS and MolOP without batch barriers."""

            nonlocal storage_completed_count, storage_phase_finished_at
            nonlocal parse_phase_started_at, parse_phase_finished_at
            nonlocal molop_file_parse_phase_started_at, molop_file_parse_phase_finished_at
            nonlocal molop_file_parse_elapsed_ms
            nonlocal molgr_reconstruction_phase_started_at
            nonlocal molgr_reconstruction_phase_finished_at, molgr_reconstruction_elapsed_ms
            storage_error: Exception | None = None
            try:
                if reservation.needs_storage:
                    loop = asyncio.get_running_loop()
                    store_function = getattr(cls._store_payload, "__func__", cls._store_payload)
                    if store_function is not _ORIGINAL_STORE_PAYLOAD:
                        if isinstance(reservation.source, Path):
                            value = await _await_cancellation_safe(
                                asyncio.to_thread(
                                    cls._store_payload,
                                    reservation.settings,
                                    reservation.object_key,
                                    reservation.source,
                                    reservation.media_type,
                                    content_sha256=reservation.content_sha256,
                                    size_bytes=reservation.size_bytes,
                                    check_existing_object=reservation.check_existing_object,
                                )
                            )
                        else:
                            # Test and extension overrides historically receive
                            # the original four-argument bytes contract.
                            value = await _await_cancellation_safe(
                                asyncio.to_thread(
                                    cls._store_payload,
                                    reservation.settings,
                                    reservation.object_key,
                                    reservation.source,
                                    reservation.media_type,
                                )
                            )
                    else:
                        if storage_pool is None:
                            raise RuntimeError("storage pool is unavailable for a storage task")
                        value = await _await_cancellation_safe(
                            loop.run_in_executor(
                                storage_pool,
                                _store_payload_worker,
                                reservation.settings,
                                reservation.object_key,
                                reservation.source,
                                reservation.media_type,
                                reservation.content_sha256
                                if isinstance(reservation.source, Path)
                                else None,
                                reservation.size_bytes
                                if isinstance(reservation.source, Path)
                                else None,
                                reservation.check_existing_object,
                            )
                        )
                    if (
                        value.size != reservation.size_bytes
                        or value.sha256 != reservation.content_sha256
                    ):
                        raise ArtifactUploadError("RustFS metadata mismatch for uploaded artifact")
                    stored[index] = value
            except Exception as error:
                storage_error = error

            async with storage_completion_lock:
                if reservation.needs_storage:
                    storage_completed_count += 1
                    if storage_completed_count == storage_total_count:
                        storage_phase_finished_at = perf_counter()
            if (
                storage_error is not None
                or reservation.ingestion_id is None
                or reservation.skip_parse
            ):
                return index, storage_error, None
            preparsed_task = (_preparsed_tasks or {}).get(index)
            if preparsed_task is not None:
                return index, None, preparsed_task.parsed
            parse_started_at = perf_counter()
            parsed: _ParsedArtifact | Exception
            molop_started_at = perf_counter()
            try:
                parsed = await _run_molop_file_pipeline(
                    reservation.source,
                    files[index].filename,
                    artifact_sha256=reservation.content_sha256,
                    submission_slots=frame_submission_slots,
                )
            except Exception as error:
                parsed = error
            molop_finished_at = perf_counter()
            async with storage_completion_lock:
                if (
                    molop_file_parse_phase_started_at is None
                    or molop_started_at < molop_file_parse_phase_started_at
                ):
                    molop_file_parse_phase_started_at = molop_started_at
                molop_file_parse_phase_finished_at = max(
                    molop_file_parse_phase_finished_at or molop_finished_at,
                    molop_finished_at,
                )
                molop_file_parse_elapsed_ms += (molop_finished_at - molop_started_at) * 1000
            if isinstance(parsed, _ParsedArtifact):
                # The helper above includes deferred MolGR frame conversion.
                # Keep the aggregate timing compatible with existing metrics.
                async with storage_completion_lock:
                    molgr_reconstruction_phase_started_at = min(
                        molgr_reconstruction_phase_started_at or molop_started_at,
                        molop_started_at,
                    )
                    molgr_reconstruction_phase_finished_at = max(
                        molgr_reconstruction_phase_finished_at or molop_finished_at,
                        molop_finished_at,
                    )
                    molgr_reconstruction_elapsed_ms += (molop_finished_at - molop_started_at) * 1000
            nonlocal_parse_finished_at = perf_counter()
            # The consumer records the first/last parser completion to expose
            # MolOP wall time separately from database persistence time.
            async with storage_completion_lock:
                if parse_phase_started_at is None or parse_started_at < parse_phase_started_at:
                    parse_phase_started_at = parse_started_at
                parse_phase_finished_at = max(
                    parse_phase_finished_at or nonlocal_parse_finished_at,
                    nonlocal_parse_finished_at,
                )
            return index, None, parsed

        pipeline_result_queue: asyncio.Queue[
            tuple[int, Exception | None, _ParsedArtifact | Exception | None]
        ] = asyncio.Queue(maxsize=persistence_batch_files)

        async def enqueue_pipeline_result(
            index: int,
            reservation: _PreparedCalculationUpload,
        ) -> None:
            # The bounded queue is the backpressure point between CPU parsing
            # and the single SQLAlchemy persistence consumer.
            try:
                result = await process_one(index, reservation)
            except Exception as error:  # pragma: no cover - defensive task boundary
                result = (index, error, None)
            await pipeline_result_queue.put(result)

        pipeline_tasks = [
            # Tasks waiting on ``_file_worker_slots`` form the file queue. The
            # shared parser pool consumes the admitted work while the next
            # queued file waits for a released admission slot.
            asyncio.create_task(enqueue_pipeline_result(index, reservation))
            for index, reservation in prepared.items()
        ]

        parse_indices: list[int] = []
        for index, reservation in prepared.items():
            if reservation.ingestion_id is not None and not reservation.skip_parse:
                parse_indices.append(index)

        # Keep the old importer execution shape: one claim window owns one
        # shared parser queue and one SQLAlchemy persistence consumer. The
        # database transaction is bounded by ``persistence_batch_files`` so
        # project-scoped advisory locks are released regularly; each committed
        # window gets a fresh GeometryPersistenceContext as well, so ORM
        # objects and reconciliation caches never cross a transaction boundary.
        persistence_pipeline_started = perf_counter()
        parse_errors_by_index: dict[int, Exception] = {}
        parse_failure_metadata_by_index: dict[int, dict[str, Any]] = {}
        parse_source_counts_by_index: dict[int, tuple[int, int]] = {}
        persisted_revisions_by_index: dict[int, tuple[UUID, bool]] = {}
        completion_by_ingestion_id: dict[UUID, _IngestionCompletion] = {}
        pending_preload: list[tuple[int, _ParsedArtifact]] = []
        pending_completed_indices: list[int] = []
        pending_persistence_frame_count = 0
        committed_callback_indices: set[int] = set()
        no_frame_indices: set[int] = set()
        persistence_ingestions_by_id: dict[UUID, ArtifactIngestion] = {}
        persistence_revision_ids_by_artifact_id: dict[UUID, set[UUID]] = {}
        persist_preload_elapsed_ms = 0.0
        persist_write_elapsed_ms = 0.0
        persist_inferred_reaction_cache_hits = 0
        advisory_lock_stats: dict[str, Any] = {
            "calls": 0,
            "requested_ids": 0,
            "uncached_ids": 0,
            "prefixes": {},
        }
        geometry_context: GeometryPersistenceContext = GeometryPersistenceContext(
            project_id=project_id
        )

        def normalize_parser_result(parser_result: Any) -> _ParsedArtifact | Exception:
            if isinstance(parser_result, (_ParsedArtifact, Exception)):
                return parser_result
            if isinstance(parser_result, tuple) and len(parser_result) == 2:
                parsed, error_message = parser_result
                if isinstance(parsed, _ParsedArtifact):
                    return parsed
                return ArtifactUploadError(
                    error_message or "MolOP did not return a result for this input file"
                )
            return ArtifactUploadError("MolOP returned an invalid parser result")

        async def persist_completed_file(local_index: int, parser_result: Any) -> None:
            nonlocal pending_persistence_frame_count
            parsed = normalize_parser_result(parser_result)
            original_index = parse_indices[local_index]
            if on_file_parsed is not None:
                await on_file_parsed(
                    original_index,
                    isinstance(parsed, _ParsedArtifact) and parsed.source_frame_count > 0,
                )
            if isinstance(parsed, Exception):
                parse_errors_by_index[original_index] = parsed
                if isinstance(parsed, NoCalculationFramesError):
                    no_frame_indices.add(original_index)
                return
            if parsed.source_frame_count == 0:
                no_frame_error = NoCalculationFramesError(
                    "source contains no QM calculation frames; artifact was filtered"
                )
                no_frame_indices.add(original_index)
                parse_errors_by_index[original_index] = no_frame_error
                return
            pending_preload.append((local_index, parsed))
            pending_persistence_frame_count += max(
                1,
                len(parsed.frame_records) or parsed.source_frame_count,
            )

        parse_pipeline_started = persistence_pipeline_started
        local_index_by_original = {
            original_index: local_index for local_index, original_index in enumerate(parse_indices)
        }
        async with (
            _pipeline_task_lifecycle(
                pipeline_tasks,
                on_abort=recover_aborted_batch,
            ),
            session_factory() as session,
        ):
            preload_started = perf_counter()
            ingestion_ids = [
                _require_prepared_ingestion_id(prepared[original_index])
                for original_index in parse_indices
            ]
            (
                persistence_ingestions_by_id,
                persistence_revision_ids_by_artifact_id,
            ) = await session.run_sync(
                partial(
                    _run_preload_batch_persistence_state,
                    ingestion_ids=ingestion_ids,
                )
            )
            await session.run_sync(_run_disable_autoflush)
            geometry_context = GeometryPersistenceContext(project_id=project_id)
            deferred_inferences: list[_DeferredArtifactInferences] = []
            persist_preload_elapsed_ms = (perf_counter() - preload_started) * 1000

            async def persist_parsed_files(
                parsed_files: list[tuple[int, _ParsedArtifact]],
            ) -> None:
                nonlocal persist_preload_elapsed_ms, persist_write_elapsed_ms
                if not parsed_files:
                    return
                inference_topology_records: list[Any] = []
                legacy_bulk_import = bool(
                    cast(Any, session).sync_session.info.get(
                        LEGACY_BULK_IMPORT_SESSION_INFO_KEY,
                        False,
                    )
                )
                for _, parsed in parsed_files:
                    for inferred in parsed.inferences:
                        if not isinstance(inferred, _SuccessfulInference):
                            continue
                        cache_key: str | None = None
                        cached: tuple[Any, ...] | None = None
                        try:
                            if legacy_bulk_import:
                                cache_key = inferred.reaction_smiles
                                cached = (
                                    geometry_context.inferred_reaction_topology_records_by_key.get(
                                        cache_key
                                    )
                                )
                                prepared_inference = _inference_topology_records_for_context(
                                    inferred,
                                    geometry_context,
                                    reaction_records=cached,
                                    include_participants=cached is None,
                                )
                                records = list(prepared_inference.all_records)
                            else:
                                prepared_inference = _inference_topology_records_for_context(
                                    inferred,
                                    geometry_context,
                                )
                                base_records = prepared_inference.all_records
                                cache_key = _inference_reaction_cache_key(
                                    inferred,
                                    base_records,
                                )
                                cached = (
                                    geometry_context.inferred_reaction_topology_records_by_key.get(
                                        cache_key
                                    )
                                )
                                records = list(base_records)
                            inference_topology_records.extend(records)
                            if cached is None and len(records) > 2:
                                geometry_context.inferred_reaction_topology_records_by_key.setdefault(
                                    cache_key, tuple(records[2:])
                                )
                        except Exception:
                            logger.warning(
                                "failed to preload inferred reaction topology for frame %s",
                                inferred.file_frame_index,
                                exc_info=True,
                            )
                            # Do not silently replace a MolGR topology with
                            # an RDKit-normalized participant. Persistence
                            # will record the inference failure explicitly.
                            if cached is None:
                                if cache_key is None:
                                    cache_key = _inference_reaction_cache_key(inferred)
                                geometry_context.inferred_reaction_topology_records_by_key[
                                    cache_key
                                ] = ()
                preload_started = perf_counter()
                await session.run_sync(
                    partial(
                        _run_preload_molecular_geometry_context,
                        parsed_artifacts=[parsed for _, parsed in parsed_files],
                        context=geometry_context,
                        topology_records=inference_topology_records,
                    )
                )
                persist_preload_elapsed_ms += (perf_counter() - preload_started) * 1000
                for local_index, parsed in parsed_files:
                    original_index = parse_indices[local_index]
                    reservation = prepared[original_index]
                    ingestion_id = _require_prepared_ingestion_id(reservation)
                    ingestion = persistence_ingestions_by_id[ingestion_id]
                    write_started = perf_counter()
                    try:
                        revision_id, revision_created = await session.run_sync(
                            partial(
                                _run_persist_parsed_artifact_savepoint,
                                ingestion_id=ingestion_id,
                                parsed=parsed,
                                started_at=reservation.started_at,
                                completed_at=datetime.now(UTC),
                                geometry_context=geometry_context,
                                force_new_revision=reservation.force_new_revision,
                                preload_geometry_context=False,
                                ingestion=ingestion,
                                existing_revision_ids=(
                                    persistence_revision_ids_by_artifact_id[
                                        ingestion.artifact_file_id
                                    ]
                                ),
                                defer_ingestion_completion=True,
                                defer_reconciliation=True,
                                defer_thermodynamic_refresh=defer_thermodynamic_refresh,
                                deferred_inferences=deferred_inferences,
                            )
                        )
                    except Exception as error:
                        parse_errors_by_index[original_index] = error
                        parse_failure_metadata_by_index[original_index] = _parse_failure_metadata(
                            error, parsed=parsed
                        )
                        parse_source_counts_by_index[original_index] = (
                            parsed.source_frame_count,
                            len(parsed.inferences),
                        )
                        persist_write_elapsed_ms += (perf_counter() - write_started) * 1000
                        continue
                    persist_write_elapsed_ms += (perf_counter() - write_started) * 1000
                    persisted_revisions_by_index[original_index] = (
                        revision_id,
                        revision_created,
                    )
                    (
                        artifact_diagnostics,
                        _failed_frame_count,
                        parse_completeness,
                    ) = session.info.get("_molop_artifact_diagnostics", {}).get(
                        ingestion_id,
                        ((), 0, ParseCompleteness.COMPLETE),
                    )
                    completion_by_ingestion_id[ingestion_id] = _IngestionCompletion(
                        parse_revision_id=revision_id,
                        parse_revision_created=revision_created,
                        source_frame_count=parsed.source_frame_count,
                        transition_state_frame_count=len(parsed.inferences),
                        source_format=parsed.source_format,
                        completed_at=datetime.now(UTC),
                        parse_completeness=(
                            ParseCompleteness.PARTIAL
                            if parse_completeness is ParseCompleteness.PARTIAL
                            else ParseCompleteness.COMPLETE
                        ),
                        parse_diagnostics=tuple(artifact_diagnostics),
                    )

            async def commit_persistence_window(
                completed_indices: list[int],
            ) -> None:
                """Flush one persistence microbatch and release its locks.

                The parser queue and process pool remain shared for the
                whole claim window. Only the database transaction is
                bounded: project-scoped identity locks are PostgreSQL
                transaction locks, so retaining one transaction for all
                claimed files can exhaust ``max_locks_per_transaction``.
                A fresh Geometry context per committed microbatch keeps the
                reconciliation graph and transaction-local candidate index
                bounded after the commit.
                """

                nonlocal geometry_context, deferred_inferences
                nonlocal pending_persistence_frame_count
                nonlocal persist_inferred_reaction_cache_hits, persist_write_elapsed_ms
                if not completed_indices:
                    return

                window_write_started = perf_counter()
                for original_index in completed_indices:
                    parse_error = parse_errors_by_index.get(original_index)
                    if parse_error is None:
                        continue
                    reservation = prepared[original_index]
                    if reservation.ingestion_id is None:
                        continue
                    ingestion_id = _require_prepared_ingestion_id(reservation)
                    ingestion = persistence_ingestions_by_id[ingestion_id]
                    await session.run_sync(
                        partial(
                            _run_mark_ingestion_failed,
                            ingestion_id=ingestion_id,
                            error=parse_error,
                            error_code=(
                                "artifact_storage_failed"
                                if original_index in storage_errors
                                else "no_calculation_frames"
                                if original_index in no_frame_indices
                                else getattr(parse_error, "error_code", "molop_parse_failed")
                            ),
                            completed_at=datetime.now(UTC),
                            ingestion=ingestion,
                            source_frame_count=(
                                0
                                if original_index in no_frame_indices
                                else parse_source_counts_by_index.get(
                                    original_index,
                                    (None, None),
                                )[0]
                            ),
                            transition_state_frame_count=(
                                0
                                if original_index in no_frame_indices
                                else parse_source_counts_by_index.get(
                                    original_index,
                                    (None, None),
                                )[1]
                            ),
                            error_metadata=(
                                None
                                if original_index in no_frame_indices
                                else parse_failure_metadata_by_index.get(original_index)
                            ),
                        )
                    )

                window_stored = {
                    prepared[index].artifact_id: (prepared[index].object_key, stored[index])
                    for index in completed_indices
                    if index in stored
                }
                if window_stored:
                    storage_db_started = perf_counter()
                    await session.run_sync(
                        partial(
                            _run_mark_uploads_available,
                            stored_by_artifact_id=window_stored,
                        )
                    )
                    timings["storage_db_ms"] = timings.get("storage_db_ms", 0.0) + (
                        (perf_counter() - storage_db_started) * 1000
                    )

                flush_started = perf_counter()
                bulk_diagnostics = await session.run_sync(_run_flush)
                timings["persist_flush_initial_ms"] = (
                    timings.get("persist_flush_initial_ms", 0.0)
                    + (perf_counter() - flush_started) * 1000
                )
                if isinstance(bulk_diagnostics, dict):
                    for key in ("pending", "transient", "prepare_ms", "execute_ms"):
                        metric_key = (
                            f"persist_bulk_{key}_rows"
                            if key in {"pending", "transient"}
                            else f"persist_bulk_{key}"
                        )
                        timings[metric_key] = timings.get(metric_key, 0.0) + float(
                            cast(Any, bulk_diagnostics.get(key, 0))
                        )

                if deferred_inferences:
                    inference_started = perf_counter()
                    await session.run_sync(
                        partial(
                            _run_persist_deferred_inferences,
                            deferred_inferences=deferred_inferences,
                            topology_context=geometry_context,
                            defer_thermodynamic_refresh=True,
                        )
                    )
                    timings["persist_deferred_inferences_ms"] = (
                        timings.get("persist_deferred_inferences_ms", 0.0)
                        + (perf_counter() - inference_started) * 1000
                    )
                    # Inference persistence can queue additional rows after
                    # the initial revision-local flush. Make them visible
                    # before the reconciliation barrier.
                    await session.run_sync(_run_flush)

                window_parse_indices = [
                    index for index in completed_indices if index in local_index_by_original
                ]
                parse_revision_by_ingestion_id: dict[UUID, UUID | None] = {}
                parse_revision_created_by_ingestion_id: dict[UUID, bool | None] = {}
                batch_inferences_by_ingestion_id: dict[UUID, list[TransitionStateInference]] = {}
                for original_index in window_parse_indices:
                    ingestion_id = _require_prepared_ingestion_id(prepared[original_index])
                    persisted_revision = persisted_revisions_by_index.get(original_index)
                    if persisted_revision is not None:
                        parse_revision_by_ingestion_id[ingestion_id] = persisted_revision[0]
                        parse_revision_created_by_ingestion_id[ingestion_id] = persisted_revision[1]
                    else:
                        parse_revision_by_ingestion_id[ingestion_id] = None
                        parse_revision_created_by_ingestion_id[ingestion_id] = False

                if parse_revision_by_ingestion_id:
                    batch_inferences_by_ingestion_id = await session.run_sync(
                        partial(
                            _run_finalize_batch_ingestions,
                            ingestions_by_id=persistence_ingestions_by_id,
                            parse_revision_by_ingestion_id=parse_revision_by_ingestion_id,
                            completion_by_ingestion_id=completion_by_ingestion_id,
                        )
                    )
                    # The thermodynamic source predicate reads ingestion
                    # status, so publish completion before reconciliation.
                    await session.run_sync(_run_flush)

                reconcile_started = perf_counter()
                await session.run_sync(
                    partial(
                        _run_reconcile_molop_geometry_context,
                        context=geometry_context,
                        defer_thermodynamic_refresh=defer_thermodynamic_refresh,
                    )
                )
                timings["persist_reconcile_geometry_ms"] = (
                    timings.get("persist_reconcile_geometry_ms", 0.0)
                    + (perf_counter() - reconcile_started) * 1000
                )

                if parse_revision_by_ingestion_id:
                    result_started = perf_counter()
                    await session.run_sync(_run_flush)
                    results_by_ingestion_id = await session.run_sync(
                        partial(
                            _run_batch_results,
                            parse_revision_by_ingestion_id=parse_revision_by_ingestion_id,
                            parse_revision_created_by_ingestion_id=(
                                parse_revision_created_by_ingestion_id
                            ),
                            inferences_by_ingestion_id=batch_inferences_by_ingestion_id,
                        )
                    )
                    timings["persist_result_db_ms"] = (
                        timings.get("persist_result_db_ms", 0.0)
                        + (perf_counter() - result_started) * 1000
                    )
                    for original_index in window_parse_indices:
                        reservation = prepared[original_index]
                        ingestion_id = _require_prepared_ingestion_id(reservation)
                        result = results_by_ingestion_id[ingestion_id]
                        parse_error = parse_errors_by_index.get(original_index)
                        error_code = (
                            "no_calculation_frames"
                            if original_index in no_frame_indices
                            else "artifact_storage_failed"
                            if original_index in storage_errors
                            else getattr(parse_error, "error_code", "molop_parse_failed")
                            if parse_error is not None
                            else None
                        )
                        item_by_index[original_index] = ArtifactBatchUploadItem(
                            filename=files[original_index].filename,
                            succeeded=result.ingestion_status
                            not in {
                                ArtifactIngestionStatus.FAILED,
                                ArtifactIngestionStatus.FILTERED,
                            },
                            result=result,
                            error_code=error_code,
                            error_message=(
                                (str(parse_error) or type(parse_error).__name__)
                                if parse_error is not None
                                else None
                            ),
                        )

                commit_started = perf_counter()
                await session.commit()
                timings["persist_commit_db_ms"] = (
                    timings.get("persist_commit_db_ms", 0.0)
                    + (perf_counter() - commit_started) * 1000
                )
                persist_inferred_reaction_cache_hits += (
                    geometry_context.inferred_reaction_cache_hits
                )
                lock_stats = await session.run_sync(
                    lambda sync_session: dict(
                        cast(Session, sync_session).info.get("_identity_lock_stats", {})
                    )
                )
                advisory_lock_stats["calls"] = int(lock_stats.get("calls", 0))
                advisory_lock_stats["requested_ids"] = int(lock_stats.get("requested_ids", 0))
                advisory_lock_stats["uncached_ids"] = int(lock_stats.get("uncached_ids", 0))
                advisory_lock_stats["prefixes"] = dict(lock_stats.get("prefixes", {}))
                if on_file_committed is not None:
                    for original_index in window_parse_indices:
                        item = item_by_index.get(original_index)
                        if item is not None:
                            await on_file_committed(original_index, item)
                            committed_callback_indices.add(original_index)
                persist_write_elapsed_ms += (perf_counter() - window_write_started) * 1000

                # Deferred inference work has been persisted in this
                # transaction. Do not carry ORM objects or reconciliation
                # caches across the commit; the next window preloads its
                # own bounded project context.
                deferred_inferences = []
                geometry_context = GeometryPersistenceContext(project_id=project_id)
                pending_persistence_frame_count = 0

            persistence_preload_limit = min(
                persistence_batch_files
                if _preparsed_tasks is not None
                else PERSISTENCE_PRELOAD_BATCH_SIZE,
                persistence_batch_files,
            )

            for _ in pipeline_tasks:
                index, storage_error, parsed = await pipeline_result_queue.get()
                pending_completed_indices.append(index)
                if storage_error is not None:
                    storage_errors[index] = storage_error
                    local_index = local_index_by_original.get(index)
                    if local_index is not None:
                        parse_errors_by_index[index] = storage_error
                else:
                    local_index = local_index_by_original.get(index)
                if storage_error is None and local_index is not None:
                    await persist_completed_file(local_index, parsed)
                    # Keep parsing and database work overlapped inside the
                    # same claim window. The preload hand-off cannot contain
                    # more files than the transaction boundary; otherwise a
                    # nominal eight-file commit could still accumulate the
                    # old 32-file write set in the same SQLAlchemy Session.
                    if pending_preload and (
                        len(pending_preload) >= persistence_preload_limit
                        or (pipeline_result_queue.empty() and _preparsed_tasks is None)
                    ):
                        parsed_batch = pending_preload.copy()
                        pending_preload.clear()
                        await persist_parsed_files(parsed_batch)
                if (
                    len(pending_completed_indices) >= persistence_batch_files
                    or pending_persistence_frame_count >= persistence_frame_limit
                ):
                    completed_batch = pending_completed_indices.copy()
                    pending_completed_indices.clear()
                    parsed_batch = pending_preload.copy()
                    pending_preload.clear()
                    await persist_parsed_files(parsed_batch)
                    await commit_persistence_window(completed_batch)

            await asyncio.gather(*pipeline_tasks)
            if pending_completed_indices:
                completed_batch = pending_completed_indices.copy()
                pending_completed_indices.clear()
                parsed_batch = pending_preload.copy()
                pending_preload.clear()
                await persist_parsed_files(parsed_batch)
                await commit_persistence_window(completed_batch)

        timings["molop_parse_ms"] = (
            (parse_phase_finished_at - parse_phase_started_at) * 1000
            if parse_phase_started_at is not None and parse_phase_finished_at is not None
            else 0.0
        )
        timings["molop_file_parse_ms"] = (
            (molop_file_parse_phase_finished_at - molop_file_parse_phase_started_at) * 1000
            if (
                molop_file_parse_phase_started_at is not None
                and molop_file_parse_phase_finished_at is not None
            )
            else 0.0
        )
        timings["molop_file_parse_sum_ms"] = molop_file_parse_elapsed_ms
        timings["molgr_frame_reconstruction_ms"] = (
            (molgr_reconstruction_phase_finished_at - molgr_reconstruction_phase_started_at) * 1000
            if (
                molgr_reconstruction_phase_started_at is not None
                and molgr_reconstruction_phase_finished_at is not None
            )
            else 0.0
        )
        timings["molgr_frame_reconstruction_sum_ms"] = molgr_reconstruction_elapsed_ms
        timings["parse_ms"] = timings["molop_parse_ms"]
        timings["parse_persistence_pipeline_ms"] = (perf_counter() - parse_pipeline_started) * 1000

        storage_phase_finished = storage_phase_finished_at or perf_counter()
        timings["storage_ms"] = (storage_phase_finished - phase_started) * 1000
        timings["persist_preload_db_ms"] = persist_preload_elapsed_ms
        timings["persist_write_db_ms"] = persist_write_elapsed_ms
        timings["persist_inferred_reaction_cache_hits"] = float(
            persist_inferred_reaction_cache_hits
        )
        timings["advisory_lock_calls"] = float(advisory_lock_stats["calls"])
        timings["advisory_lock_requested_ids"] = float(advisory_lock_stats["requested_ids"])
        timings["advisory_lock_uncached_ids"] = float(advisory_lock_stats["uncached_ids"])
        for prefix, count in sorted(advisory_lock_stats["prefixes"].items()):
            timings[f"advisory_lock_{prefix}_calls"] = float(count)
        timings["persist_commit_db_ms"] = timings.get("persist_commit_db_ms", 0.0)
        timings["persist_pipeline_wall_ms"] = (perf_counter() - persistence_pipeline_started) * 1000
        timings["persist_db_ms"] = sum(
            timings.get(key, 0.0)
            for key in (
                "persist_preload_db_ms",
                "persist_write_db_ms",
                "persist_result_db_ms",
                "persist_commit_db_ms",
            )
        )

        # Already-available/idempotently completed artifacts can be returned without parsing.
        for index, reservation in prepared.items():
            if index in item_by_index:
                continue
            if reservation.duplicate_of is not None:
                source_item = item_by_index.get(reservation.duplicate_of)
                if source_item is None:
                    raise RuntimeError("duplicate artifact completed before its source artifact")
                item_by_index[index] = source_item.model_copy(
                    update={"filename": files[index].filename}
                )
                if on_file_committed is not None:
                    await on_file_committed(index, item_by_index[index])
                continue
            item_by_index[index] = ArtifactBatchUploadItem(
                filename=files[index].filename,
                succeeded=(
                    index not in storage_errors
                    and reservation.ingestion_status
                    not in {
                        ArtifactIngestionStatus.FAILED,
                        ArtifactIngestionStatus.FILTERED,
                    }
                ),
                result=(
                    cls._batch_result_for_stored_artifact(reservation, artifact_kind=artifact_kind)
                    if index not in storage_errors
                    else None
                ),
                error_code=("artifact_storage_failed" if index in storage_errors else None),
                error_message=(str(storage_errors[index]) if index in storage_errors else None),
            )

        complete_items = [item_by_index[index] for index in range(len(files))]
        succeeded_count = sum(item.succeeded for item in complete_items)
        results = [item.result for item in complete_items if item.result is not None]
        return ArtifactBatchUploadResult(
            total_count=len(complete_items),
            succeeded_count=succeeded_count,
            failed_count=len(complete_items) - succeeded_count,
            source_frame_count=sum(result.source_frame_count or 0 for result in results),
            transition_state_frame_count=sum(
                result.transition_state_frame_count or 0 for result in results
            ),
            inferred_reaction_count=sum(result.inferred_reaction_count for result in results),
            timings_ms={**timings, "total_ms": (perf_counter() - started) * 1000},
            items=complete_items,
        )

    @staticmethod
    def _store_payload(
        settings: RustFSSettings,
        object_key: str,
        source: bytes | Path,
        media_type: str,
        content_sha256: str | None = None,
        size_bytes: int | None = None,
        *,
        check_existing_object: bool = True,
    ) -> Any:
        with RustFSObjectStore(settings) as store:
            store.ensure_bucket()
            if check_existing_object and store.exists(object_key):
                return store.head(object_key)
            if isinstance(source, Path):
                if content_sha256 is None or size_bytes is None:
                    raise ValueError("streamed uploads require precomputed source identity")
                return store.put_file(
                    key=object_key,
                    path=source,
                    content_sha256=content_sha256,
                    size_bytes=size_bytes,
                    content_type=media_type,
                    metadata={"ingestion": "artifact-upload"},
                )
            return store.put_bytes(
                key=object_key,
                payload=source,
                content_type=media_type,
                metadata={"ingestion": "artifact-upload"},
            )

    @staticmethod
    def _load_payload(settings: RustFSSettings, object_key: str) -> bytes:
        with RustFSObjectStore(settings) as store:
            return store.get_bytes(object_key)

    @staticmethod
    def _head_payload(settings: RustFSSettings, object_key: str) -> Any:
        """Read object metadata while recovering a pre-queue reservation."""

        with RustFSObjectStore(settings) as store:
            return store.head(object_key)


_ORIGINAL_STORE_PAYLOAD = cast(
    Any,
    ArtifactUploadService.__dict__["_store_payload"],
).__func__


__all__ = [
    "ArtifactUploadConflictError",
    "ArtifactUploadError",
    "ArtifactUploadLimitError",
    "ArtifactUploadPayload",
    "ArtifactUploadService",
    "MolOPFileParseTimeoutError",
    "molop_process_worker_count",
    "warm_molop_process_pool",
    "warm_storage_process_pool",
]
