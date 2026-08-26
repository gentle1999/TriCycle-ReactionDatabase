"""Store uploaded artifacts, ingest calculation outputs, and infer TS reactions."""

from __future__ import annotations

import asyncio
import copy
import gzip
import io
import logging
import multiprocessing
import os
import tempfile
import threading
import zlib
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from queue import Empty, Queue
from time import perf_counter
from typing import Any, cast
from uuid import UUID

import numpy as np
from molop import AutoFileParser
from molop.config import molopconfig
from molop.io.base_models.ChemFileFrame import BaseCalcFrame
from molop.unit import atom_ureg
from rdkit import Chem
from rdkit.Chem import rdChemReactions
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlalchemy.orm import joinedload
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.dtos import (
    ArtifactBatchUploadItem,
    ArtifactBatchUploadResult,
    ArtifactFileRecord,
    ArtifactUploadResult,
    ArtifactValidationInferenceView,
    ArtifactValidationResult,
    CreateReactionCommand,
    TransitionStateInferenceView,
)
from tricycle_reaction_db.application.services._persistence import (
    _acquire_identity_locks,
    _attach_pending_entities,
    _fast_insert_enabled,
    _flush_new_entity,
    _new_entity,
    _prepare_new_entity,
    _require_id,
)
from tricycle_reaction_db.application.services.artifact_content import (
    detect_artifact_media_type,
)
from tricycle_reaction_db.application.services.authorization import (
    AuthorizationService,
    ProjectPermission,
)
from tricycle_reaction_db.application.services.catalog import (
    persist_artifact_file,
)
from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence import (
    refresh_mapped_reaction_thermodynamics,
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
    reaction_topology_records,
)
from tricycle_reaction_db.application.services.reaction_geometry_reconciliation import (
    ReconciliationBatchCache,
    bind_transition_state_frame,
    ensure_transition_state_path,
)
from tricycle_reaction_db.application.services.reactions import (
    _canonical_mapped_reaction_smiles,
    _reaction_from_representation,
)
from tricycle_reaction_db.core.config import get_settings
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
    StorageStatus,
    TransitionStateEndpointDirection,
    TransitionStateInferenceStatus,
)
from tricycle_reaction_db.domain.reaction_frames import is_transition_state_frame_eligible
from tricycle_reaction_db.ingestion import (
    MolOPFrameRecords,
    configure_molecular_graph_reconstruction,
    frame_records_from_molop,
    normalize_topology_with_mapping,
)
from tricycle_reaction_db.storage.rustfs import (
    RustFSObjectStore,
    RustFSSettings,
    time_partitioned_content_addressed_key,
    time_partitioned_content_addressed_key_for_sha256,
)

MOLOP_VERSION = version("molop")
logger = logging.getLogger(__name__)
# Pre/post-TS endpoint selection is delegated to MolOP's
# ``BaseCalcFrame.possible_pre_post_ts``: it samples both signed sides across
# the amplitude range below and keeps the most frequent side topology.  These
# values mirror the MolOP defaults so the persisted inference settings always
# describe the actual sampling grid.
TS_PRE_POST_MIN_RATIO = 0.75
TS_PRE_POST_MAX_RATIO = 1.75
TS_PRE_POST_STEPS = 7
PERSISTENCE_PRELOAD_BATCH_SIZE = 32
# MolGR reconstruction is CPU-heavy and each frame crosses a process boundary.
# Larger chunks amortize pickle/future overhead while retaining enough tasks to
# keep all configured workers busy across a multi-file batch.
FRAME_CONVERSION_CHUNK_SIZE = 32
INFERENCE_PERSIST_BATCH_SIZE = 16


class ArtifactUploadError(RuntimeError):
    pass


class ArtifactUploadLimitError(ArtifactUploadError):
    """Upload bytes or file count exceed a configured hard resource budget."""


class ArtifactUploadConflictError(ArtifactUploadError):
    pass


class NoCalculationFramesError(ArtifactUploadError):
    """The source is not a QM calculation output accepted by the catalogue."""


@dataclass(frozen=True, slots=True)
class _SuccessfulInference:
    file_frame_index: int
    imaginary_mode_index: int
    imaginary_frequency_cm1: float
    reaction_smiles: str
    negative_endpoint: Chem.Mol
    positive_endpoint: Chem.Mol
    negative_displacement_ratio: float
    positive_displacement_ratio: float


@dataclass(frozen=True, slots=True)
class _FailedInference:
    file_frame_index: int
    imaginary_mode_index: int
    imaginary_frequency_cm1: float
    error_code: str
    error_message: str


_Inference = _SuccessfulInference | _FailedInference


@dataclass(frozen=True, slots=True)
class _ParsedArtifact:
    # Production parsing keeps the owning-process ChemFile so topology
    # reconstruction can be deferred until persistence.  The slim wrapper is
    # retained for the legacy process-pool parser, where the frame tree cannot
    # be sent back over IPC without a large copy.
    chem_file: Any
    frame_records: tuple[MolOPFrameRecords, ...]
    source_frame_count: int
    source_format: str | None
    source_compression: str | None
    inferences: tuple[_Inference, ...]
    record_sha256: str | None = None
    artifact_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class _ProcessedFrame:
    """One frame after MolGR reconstruction and ingestion-level validation."""

    file_frame_index: int
    record: MolOPFrameRecords
    inference: _Inference | None
    topology_reconstruction_status: str | None


@dataclass(frozen=True, slots=True)
class _ParsedChemFile:
    """File-level MolOP metadata retained after worker conversion.

    The original ChemFile contains every parsed frame.  Returning it together
    with ``frame_records`` duplicates the complete frame tree across the
    process boundary, so only the metadata and source segments cross IPC.
    """

    payload: dict[str, Any]
    source_segments: tuple[Any, ...]

    @property
    def schema_version(self) -> str:
        return str(self.payload["schema_version"])

    @property
    def artifact_sha256(self) -> str | None:
        value = self.payload.get("artifact_sha256")
        return value if isinstance(value, str) else None

    @property
    def artifact_size_bytes(self) -> int | None:
        value = self.payload.get("artifact_size_bytes")
        return value if isinstance(value, int) else None

    @property
    def source_diagnostics(self) -> list[Any]:
        value = self.payload.get("source_diagnostics", [])
        return value if isinstance(value, list) else []

    def model_dump(self, *, mode: str = "python", **_kwargs: Any) -> dict[str, Any]:
        return dict(self.payload)


@dataclass(frozen=True, slots=True)
class _IngestionCompletion:
    parse_revision_id: UUID
    parse_revision_created: bool
    source_frame_count: int
    transition_state_frame_count: int
    source_format: str | None
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class _DeferredArtifactInferences:
    ingestion: ArtifactIngestion
    parse_revision: ParseRevision
    parsed: _ParsedArtifact
    frames_by_file_index: dict[int, CalculationFrame]
    revision_created: bool
    defer_revision_local_flush: bool


@dataclass(frozen=True, slots=True)
class _InferencePersistenceTask:
    deferred: _DeferredArtifactInferences
    inferred: _SuccessfulInference
    calculation_frame: CalculationFrame


@dataclass(frozen=True, slots=True)
class ArtifactUploadPayload:
    filename: str
    media_type: str
    payload: bytes | None
    spool_path: Path | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class _RetiredArtifactReservation:
    bucket: str
    object_key: str
    version_id: str | None
    etag: str | None
    storage_verified_at: datetime | None


@dataclass(frozen=True, slots=True)
class _RetiredArtifactObject:
    bucket: str
    object_key: str
    version_id: str | None
    content_sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _PreparedCalculationUpload:
    settings: RustFSSettings
    artifact_id: UUID
    object_key: str
    ingestion_id: UUID | None
    started_at: datetime
    source: bytes | Path
    size_bytes: int
    media_type: str
    content_sha256: str
    retired_reservation: _RetiredArtifactReservation | None = None
    needs_storage: bool = True
    check_existing_object: bool = True
    skip_parse: bool = False
    duplicate_of: int | None = None


@dataclass(frozen=True, slots=True)
class _InspectedUploadSource:
    source: bytes | Path
    size_bytes: int
    content_sha256: str
    media_probe: bytes


class _InspectingReader:
    """Record raw source identity while a gzip reader consumes a stream."""

    def __init__(self, stream: io.BufferedReader, *, probe_size: int) -> None:
        self._stream = stream
        self._digest = sha256()
        self._probe = bytearray()
        self._probe_size = probe_size
        self.size_bytes = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size)
        if chunk:
            self._digest.update(chunk)
            self.size_bytes += len(chunk)
            if len(self._probe) < self._probe_size:
                self._probe.extend(chunk[: self._probe_size - len(self._probe)])
        return chunk

    @property
    def content_sha256(self) -> str:
        return self._digest.hexdigest()

    @property
    def media_probe(self) -> bytes:
        return bytes(self._probe)


def _require_prepared_ingestion_id(reservation: _PreparedCalculationUpload) -> UUID:
    if reservation.ingestion_id is None:
        raise RuntimeError("calculation upload is missing its ingestion reservation")
    return reservation.ingestion_id


def _safe_parser_suffix(filename: str) -> str:
    name = Path(filename).name.lower()
    if name.endswith(".gz"):
        name = name[:-3]
    suffix = Path(name).suffix
    return suffix if suffix in {".log", ".out", ".xyz"} else ".log"


_molop_process_pool: ProcessPoolExecutor | None = None
_molop_process_pool_workers: int | None = None
_molop_process_pool_pid: int | None = None
_molop_process_pool_lock = threading.Lock()
_frame_process_pool: ProcessPoolExecutor | None = None
_frame_process_pool_workers: int | None = None
_frame_process_pool_pid: int | None = None
_frame_process_pool_lock = threading.Lock()
_storage_process_pool: ProcessPoolExecutor | None = None
_storage_process_pool_workers: int | None = None
_storage_process_pool_pid: int | None = None
_storage_process_pool_lock = threading.Lock()

# A storage-pool child handles many files over its lifetime. Recreating a
# boto3 client and performing a bucket HEAD for every file adds a large fixed
# latency to small and medium calculation outputs. Keep one client per child
# and initialize the bucket lazily on its first task.
_storage_worker_store: RustFSObjectStore | None = None
_storage_worker_store_key: tuple[Any, ...] | None = None
_storage_worker_bucket_ready = False

# A parsed ChemFile is already resident in the MolOP worker. On Linux we can
# fork short-lived conversion workers from that process and share parsed frames
# copy-on-write, avoiding another large IPC transfer for every frame.
_frame_conversion_chem_file: Any = None
_frame_conversion_schema_version: str | None = None


def _initialize_frame_process_worker() -> None:
    """Configure MolGR once when a frame-pool child starts."""

    configure_molecular_graph_reconstruction()
    molopconfig.prewarm_topologies = False


def _frame_record_from_shared_chem_file(index: int) -> MolOPFrameRecords:
    chem_file = _frame_conversion_chem_file
    schema_version = _frame_conversion_schema_version
    if chem_file is None or schema_version is None:
        raise RuntimeError("frame conversion worker was not initialized")
    return frame_records_from_molop(
        chem_file[index],
        export_schema_version=schema_version,
        fallback_index=index,
    )


def _frame_records_from_chem_file(
    chem_file: Any,
    *,
    parallel: bool,
) -> tuple[MolOPFrameRecords, ...]:
    """Convert parsed frames without re-entering MolGR from worker children."""

    global _frame_conversion_chem_file, _frame_conversion_schema_version
    frame_count = len(chem_file)
    if not parallel or os.name != "posix" or frame_count < 32:
        return tuple(
            frame_records_from_molop(
                frame,
                export_schema_version=chem_file.schema_version,
                fallback_index=index,
            )
            for index, frame in enumerate(chem_file)
        )

    # ``fork`` shares the already-materialized RDKit/MolOP frame graph read-only.
    # The worker only serializes DTOs; it must never invoke MolGR itself.
    _frame_conversion_chem_file = chem_file
    _frame_conversion_schema_version = str(chem_file.schema_version)
    workers = min(_resolve_molop_process_workers(get_settings().molop_batch_n_jobs), frame_count)
    try:
        with ProcessPoolExecutor(
            max_workers=workers,
            mp_context=multiprocessing.get_context("fork"),
        ) as pool:
            return tuple(
                pool.map(
                    _frame_record_from_shared_chem_file,
                    range(frame_count),
                    chunksize=1,
                )
            )
    finally:
        _frame_conversion_chem_file = None
        _frame_conversion_schema_version = None


async def _run_molop_parser(function: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a synchronous parser dispatcher without a request-level gate.

    Concurrency is owned by the explicit stage process pools. Keeping an
    asyncio semaphore here made the effective parallelism opaque and forced
    unrelated upload batches to queue behind one another.
    """

    return await asyncio.to_thread(function, *args, **kwargs)


async def _run_molop_parser_with_progress(
    function: Any,
    *args: Any,
    progress_callback: Callable[[int, Any], Awaitable[None]],
    **kwargs: Any,
) -> Any:
    """Run a parser while forwarding completed-file events to the event loop."""

    progress_queue: Queue[tuple[int, Any]] = Queue()
    kwargs["progress_queue"] = progress_queue
    parser_task = asyncio.create_task(_run_molop_parser(function, *args, **kwargs))
    callback_error: Exception | None = None
    while not parser_task.done() or not progress_queue.empty():
        try:
            input_index, result = await asyncio.to_thread(progress_queue.get, True, 0.1)
        except Empty:
            continue
        if callback_error is None:
            try:
                await progress_callback(input_index, result)
            except Exception as error:
                # The parser owns temporary paths used by its worker processes.
                # Let it finish before unwinding the upload transaction.
                callback_error = error
    parsed = await parser_task
    if callback_error is not None:
        raise callback_error
    return parsed


def _resolve_molop_process_workers(n_jobs: int) -> int:
    return max(1, (os.cpu_count() or 1) if n_jobs == -1 else n_jobs)


def _frame_submission_limit() -> int:
    workers = _resolve_molop_process_workers(get_settings().molop_batch_n_jobs)
    return max(1, workers * 2)


def _fast_molop_ingestion_enabled() -> bool:
    """Return whether deferred MolGR work and batched frame writes are enabled."""

    settings = get_settings()
    return (
        not settings.molop_capture_source_evidence
        and settings.molop_parallel_frame_persistence
    )


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
            initializer=_initialize_frame_process_worker,
        )
        _molop_process_pool_workers = workers
        _molop_process_pool_pid = pid
        process_pool = _molop_process_pool
    if previous_pool is not None:
        previous_pool.shutdown(wait=True, cancel_futures=True)
    return process_pool


def _get_frame_process_pool(n_jobs: int) -> ProcessPoolExecutor:
    """Return the shared file/frame pool for MolOP-stage work."""

    # File parsing and frame reconstruction are different task granularities,
    # but they are both CPU-bound MolOP-stage work. Sharing workers prevents
    # two independent ``n_jobs`` process sets from duplicating RSS and startup
    # cost while the stages overlap in the batch pipeline.
    return _get_molop_process_pool(n_jobs)


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
            max_tasks_per_child=100,
        )
        _storage_process_pool_workers = workers
        _storage_process_pool_pid = pid
        process_pool = _storage_process_pool
    if previous_pool is not None:
        previous_pool.shutdown(wait=True, cancel_futures=True)
    return process_pool


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
    """Stop frame and RustFS pools owned by this API worker."""

    global _frame_process_pool, _frame_process_pool_workers, _frame_process_pool_pid
    global _storage_process_pool, _storage_process_pool_workers, _storage_process_pool_pid
    with _frame_process_pool_lock:
        frame_pool = _frame_process_pool
        _frame_process_pool = None
        _frame_process_pool_workers = None
        _frame_process_pool_pid = None
    with _storage_process_pool_lock:
        storage_pool = _storage_process_pool
        _storage_process_pool = None
        _storage_process_pool_workers = None
        _storage_process_pool_pid = None
    for pool in (frame_pool, storage_pool):
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=False)


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
                raise ArtifactUploadError(
                    error_message or "MolOP did not return a result for this input file"
                )
            return parsed
        except Exception as error:
            if isinstance(error, ArtifactUploadError):
                raise
            raise ArtifactUploadError(str(error) or type(error).__name__) from error


async def _run_molop_file_parser(payload: bytes, filename: str) -> _ParsedArtifact:
    """Backward-compatible bytes-only wrapper used by reparse callers."""

    return await _run_molop_source_parser(payload, filename)


def _require_upload_size(payload: bytes) -> None:
    maximum = get_settings().max_upload_bytes
    if len(payload) > maximum:
        raise ArtifactUploadError(f"uploaded artifact exceeds the {maximum}-byte limit")


def _inspect_upload_source(
    file: ArtifactUploadPayload,
    *,
    maximum_size: int,
) -> _InspectedUploadSource:
    """Inspect one source without materializing a spooled file in memory."""

    if file.payload is not None:
        payload = file.payload
        if len(payload) > maximum_size:
            raise ArtifactUploadLimitError(
                f"uploaded artifact exceeds the {maximum_size}-byte limit"
            )
        _require_decompressed_upload_size(payload, file.filename)
        return _InspectedUploadSource(
            source=payload,
            size_bytes=len(payload),
            content_sha256=sha256(payload).hexdigest(),
            media_probe=payload[: 64 * 1024],
        )

    if file.spool_path is None:
        raise ArtifactUploadError("uploaded artifact has no payload")
    expected_size = file.spool_path.stat().st_size
    if expected_size > maximum_size:
        raise ArtifactUploadLimitError(f"uploaded artifact exceeds the {maximum_size}-byte limit")

    with file.spool_path.open("rb") as stream:
        source = _InspectingReader(stream, probe_size=64 * 1024)
        is_gzip = file.filename.lower().endswith(".gz") or stream.peek(2)[:2] == b"\x1f\x8b"
        if is_gzip:
            try:
                with gzip.GzipFile(fileobj=cast(Any, source), mode="rb") as decompressed:
                    decompressed_size = 0
                    while chunk := decompressed.read(min(1024 * 1024, maximum_size + 1)):
                        decompressed_size += len(chunk)
                        if decompressed_size > maximum_size:
                            raise ArtifactUploadLimitError(
                                f"decompressed artifact exceeds the {maximum_size}-byte limit"
                            )
            except ArtifactUploadLimitError:
                raise
            except (EOFError, OSError, zlib.error):
                # Invalid gzip remains an isolated MolOP parse failure, matching
                # the bytes-upload path's validation behavior.
                pass
        while source.read(1024 * 1024):
            pass

    if source.size_bytes != expected_size:
        raise ArtifactUploadError("uploaded spool file changed while being inspected")
    return _InspectedUploadSource(
        source=file.spool_path,
        size_bytes=source.size_bytes,
        content_sha256=source.content_sha256,
        media_probe=source.media_probe,
    )


def _require_batch_upload_budget(
    files: list[ArtifactUploadPayload],
) -> dict[int, _InspectedUploadSource]:
    """Validate every batch dimension before authorization, storage, or parsing."""

    settings = get_settings()
    if len(files) > settings.max_batch_files:
        raise ArtifactUploadLimitError(
            f"upload batch exceeds the {settings.max_batch_files}-file limit"
        )
    total_bytes = 0
    inspected_by_index: dict[int, _InspectedUploadSource] = {}
    for index, file in enumerate(files):
        if file.payload is None and file.spool_path is None:
            continue
        inspected = _inspect_upload_source(file, maximum_size=settings.max_upload_bytes)
        total_bytes += inspected.size_bytes
        if total_bytes > settings.max_batch_bytes:
            raise ArtifactUploadLimitError(
                f"upload batch exceeds the {settings.max_batch_bytes}-byte limit"
            )
        inspected_by_index[index] = inspected
    return inspected_by_index


def _upload_payload_bytes(file: ArtifactUploadPayload) -> bytes:
    if file.payload is not None:
        return file.payload
    if file.spool_path is not None:
        return file.spool_path.read_bytes()
    raise ArtifactUploadError("uploaded artifact has no payload")


def _parser_payload(
    payload: bytes,
    filename: str,
    *,
    max_decompressed_bytes: int | None = None,
) -> tuple[bytes, str | None]:
    maximum = max_decompressed_bytes or get_settings().max_upload_bytes
    if len(payload) > maximum:
        raise ArtifactUploadError(f"uploaded artifact exceeds the {maximum}-byte limit")
    if payload.startswith(b"\x1f\x8b") or filename.lower().endswith(".gz"):
        try:
            output = bytearray()
            with gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb") as stream:
                while True:
                    chunk = stream.read(min(1024 * 1024, maximum + 1 - len(output)))
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > maximum:
                        raise ArtifactUploadError(
                            f"decompressed artifact exceeds the {maximum}-byte limit"
                        )
            return bytes(output), "gzip"
        except (EOFError, OSError, zlib.error) as error:
            raise ArtifactUploadError("uploaded gzip artifact is invalid") from error
    if len(payload) > maximum:
        raise ArtifactUploadError(f"decompressed artifact exceeds the {maximum}-byte limit")
    return payload, None


def _require_decompressed_upload_size(payload: bytes, filename: str) -> None:
    """Reject compressed resource bombs while preserving invalid-file isolation."""

    if not (payload.startswith(b"\x1f\x8b") or filename.lower().endswith(".gz")):
        return
    try:
        _parser_payload(payload, filename)
    except ArtifactUploadError as error:
        if "exceeds the" in str(error):
            raise ArtifactUploadLimitError(str(error)) from error


def _mapped_reaction_smiles(reactant: Chem.Mol, product: Chem.Mol) -> str:
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
        mapped.RemoveAllConformers()
        for atom_index, atom in enumerate(mapped.GetAtoms()):  # type: ignore[no-untyped-call]
            atom.SetAtomMapNum(atom_index + 1)
        fragments = Chem.GetMolFrags(mapped, asMols=True, sanitizeFrags=True)
        sides.append(
            ".".join(
                Chem.MolToSmiles(
                    fragment,
                    canonical=True,
                    isomericSmiles=True,
                    allHsExplicit=True,
                )
                for fragment in fragments
            )
        )
    reaction_smiles = f"{sides[0]}>>{sides[1]}"
    reaction = rdChemReactions.ReactionFromSmarts(reaction_smiles, useSmiles=True)
    if reaction is None:
        raise ValueError("MolOP TS endpoints did not produce a valid reaction")
    # Fragment order is not stable enough for persist_mapped_reaction's
    # canonical serialization check, so canonicalize the complete reaction.
    return _canonical_mapped_reaction_smiles(reaction)


def _signed_ts_endpoints(
    frame: BaseCalcFrame[Any],
    vibration_position: int,
) -> tuple[Chem.Mol, Chem.Mol, float, float]:
    """Return MolOP's inferred pre/post-TS endpoints with signed displacements.

    Endpoint *selection* is MolOP's ``possible_pre_post_ts``: it samples each
    signed side across ``TS_PRE_POST_MIN_RATIO..MAX_RATIO`` and keeps the most
    frequent side topology, so crowding cannot silently drop one side.  The
    project only *measures* the selected endpoints' actual displacement along
    the imaginary mode to restore the signed direction/ratio used by the
    persisted endpoint rows; it does not choose or rank the endpoints itself.
    """

    reactant, product = frame.possible_pre_post_ts(
        show_3D=True,
        min_ratio=TS_PRE_POST_MIN_RATIO,
        max_ratio=TS_PRE_POST_MAX_RATIO,
        steps=TS_PRE_POST_STEPS,
    )
    if frame.vibrations is None:
        raise ValueError("TS frame has no vibration mode")
    center = np.asarray(frame.coords.to(atom_ureg.angstrom).magnitude, dtype=np.float64)
    mode = np.asarray(
        frame.vibrations[vibration_position].vibration_mode.to(atom_ureg.angstrom).magnitude,
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
    return reactant, product, abs(negative_ratio), positive_ratio


def _infer_ts_frame(frame: BaseCalcFrame[Any], fallback_index: int) -> _Inference | None:
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
    frequency_cm1 = float(frequency.to(atom_ureg.cm_1).magnitude)
    if frame.topology_reconstruction_status == "suspicious_fallback":
        return _FailedInference(
            file_frame_index=file_frame_index,
            imaginary_mode_index=imaginary_mode_index,
            imaginary_frequency_cm1=frequency_cm1,
            error_code="ts_topology_untrusted",
            error_message=(
                "MolGR returned a suspicious fallback topology; "
                "TS endpoint inference was skipped"
            ),
        )
    try:
        (
            negative_endpoint,
            positive_endpoint,
            negative_displacement_ratio,
            positive_displacement_ratio,
        ) = _signed_ts_endpoints(frame, imaginary_position)
        reactant, product = sorted(
            (negative_endpoint, positive_endpoint),
            key=lambda endpoint: len(Chem.GetMolFrags(endpoint)),
            reverse=True,
        )
        for endpoint in (negative_endpoint, positive_endpoint, reactant, product):
            endpoint_atoms = [atom.GetAtomicNum() for atom in endpoint.GetAtoms()]
            if endpoint_atoms != frame.atoms:
                raise ValueError("MolOP TS endpoint atom order differs from the TS source frame")
        return _SuccessfulInference(
            file_frame_index=file_frame_index,
            imaginary_mode_index=imaginary_mode_index,
            imaginary_frequency_cm1=frequency_cm1,
            reaction_smiles=_mapped_reaction_smiles(reactant, product),
            negative_endpoint=negative_endpoint,
            positive_endpoint=positive_endpoint,
            negative_displacement_ratio=negative_displacement_ratio,
            positive_displacement_ratio=positive_displacement_ratio,
        )
    except Exception as error:
        return _FailedInference(
            file_frame_index=file_frame_index,
            imaginary_mode_index=imaginary_mode_index,
            imaginary_frequency_cm1=frequency_cm1,
            error_code="ts_endpoint_inference_failed",
            error_message=str(error) or type(error).__name__,
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

    record = frame_records_from_molop(
        frame,
        export_schema_version=schema_version,
        fallback_index=fallback_index,
    )
    file_frame_index = frame.file_frame_index
    if file_frame_index is None:
        file_frame_index = fallback_index
    return _ProcessedFrame(
        file_frame_index=file_frame_index,
        record=record,
        inference=_infer_ts_frame(frame, fallback_index),
        topology_reconstruction_status=frame.topology_reconstruction_status,
    )


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

    return tuple(
        _process_frame_without_configuration(frame, fallback_index, schema_version)
        for frame, fallback_index in frames
    )


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


def _parsed_artifact_from_chem_file(
    chem_file: Any,
    *,
    source_compression: str | None,
    artifact_sha256: str | None = None,
    slim_chem_file: bool = False,
    parallel_frame_conversion: bool = False,
    materialize_topologies: bool = True,
) -> _ParsedArtifact:
    inferred: list[_Inference] = []
    if materialize_topologies:
        for fallback_index, frame in enumerate(chem_file):
            if isinstance(frame, BaseCalcFrame):
                inference = _infer_ts_frame(frame, fallback_index)
                if inference is not None:
                    inferred.append(inference)
    frame_records = (
        _frame_records_from_chem_file(
            chem_file,
            parallel=parallel_frame_conversion,
        )
        if materialize_topologies
        else ()
    )
    parsed_chem_file: Any = chem_file
    if slim_chem_file:
        file_payload = chem_file.model_dump(mode="python")
        source_segments = tuple(chem_file.source_segments)
        file_payload["source_segments"] = list(source_segments)
        parsed_chem_file = _ParsedChemFile(
            payload=file_payload,
            source_segments=source_segments,
        )
    return _ParsedArtifact(
        chem_file=parsed_chem_file,
        frame_records=frame_records,
        source_frame_count=len(chem_file),
        source_format=chem_file.source_format,
        source_compression=source_compression,
        inferences=tuple(inferred),
        record_sha256=(
            _revision_record_hash(artifact_sha256, parsed_chem_file, list(frame_records))
            if artifact_sha256 is not None and materialize_topologies
            else None
        ),
        artifact_sha256=artifact_sha256,
    )


def _materialize_parsed_artifacts(
    parsed_artifacts: list[_ParsedArtifact],
) -> list[_ParsedArtifact]:
    """Process deferred frames through the dedicated frame process pool."""

    materialized: dict[int, _ParsedArtifact] = {}
    pool = _get_frame_process_pool(get_settings().molop_batch_n_jobs)
    for parsed in parsed_artifacts:
        if parsed.frame_records:
            materialized[id(parsed)] = parsed
            continue
        chem_file = parsed.chem_file
        if isinstance(chem_file, _ParsedChemFile):
            raise RuntimeError(
                "deferred MolOP topology reconstruction requires the owning-process ChemFile"
            )
        jobs = [
            pool.submit(
                _process_frame_worker,
                _detach_frame_for_process(frame),
                fallback_index,
                str(chem_file.schema_version),
            )
            for fallback_index, frame in enumerate(chem_file)
            if isinstance(frame, BaseCalcFrame)
        ]
        processed = [job.result() for job in jobs]
        status_by_index = {
            item.file_frame_index: item.topology_reconstruction_status
            for item in processed
        }
        for fallback_index, frame in enumerate(chem_file):
            file_frame_index = frame.file_frame_index
            if file_frame_index is None:
                file_frame_index = fallback_index
            frame.topology_reconstruction_status = status_by_index.get(file_frame_index)
        records = tuple(
            item.record
            for item in sorted(processed, key=lambda item: item.file_frame_index)
        )
        inferences = tuple(
            item.inference for item in processed if item.inference is not None
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
    if isinstance(chem_file, _ParsedChemFile):
        raise RuntimeError(
            "deferred MolOP topology reconstruction requires the owning-process ChemFile"
        )
    pool = _get_frame_process_pool(get_settings().molop_batch_n_jobs)
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

    processed_chunks = await asyncio.gather(*(process_frame_chunk(chunk) for chunk in frame_chunks))
    processed = [item for chunk in processed_chunks for item in chunk]
    status_by_index = {
        item.file_frame_index: item.topology_reconstruction_status for item in processed
    }
    for fallback_index, frame in enumerate(chem_file):
        file_frame_index = frame.file_frame_index
        if file_frame_index is None:
            file_frame_index = fallback_index
        frame.topology_reconstruction_status = status_by_index.get(file_frame_index)
    records = tuple(
        item.record for item in sorted(processed, key=lambda item: item.file_frame_index)
    )
    inferences = tuple(item.inference for item in processed if item.inference is not None)
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
            capture_source_evidence=get_settings().molop_capture_source_evidence,
            release_file_content=True,
        )
        return (
            _parsed_artifact_from_chem_file(
                chem_file,
                source_compression=source_compression,
                artifact_sha256=artifact_sha256,
                slim_chem_file=False,
                materialize_topologies=False,
            ),
            None,
        )
    except Exception as error:
        return None, str(error) or type(error).__name__
    finally:
        molopconfig.prewarm_topologies = previous_prewarm
        configure_molecular_graph_reconstruction()


def _parse_calculation_path_parent(
    path: str,
    source_compression: str | None,
    artifact_sha256: str | None = None,
) -> _ParsedArtifact:
    """Parse one file in the API process, optionally deferring topology work."""

    previous_prewarm = molopconfig.prewarm_topologies
    fast_ingestion = _fast_molop_ingestion_enabled()
    try:
        # Fast parsing is intentionally text-only.  ``frame.rdmol`` remains
        # lazy and is materialized later by the persistence microbatch.
        configure_molecular_graph_reconstruction()
        molopconfig.prewarm_topologies = False
        chem_file = AutoFileParser(
            path,
            parser_detection="auto",
            capture_source_evidence=get_settings().molop_capture_source_evidence,
            release_file_content=True,
        )
        return _parsed_artifact_from_chem_file(
            chem_file,
            source_compression=source_compression,
            artifact_sha256=artifact_sha256,
            # The owning process keeps the complete ChemFile for deferred
            # reconstruction.  Audit/validation callers still materialize
            # records immediately when the fast path is disabled.
            slim_chem_file=False,
            parallel_frame_conversion=False,
            materialize_topologies=not fast_ingestion,
        )
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
    with source.open("rb") as stream:
        is_gzip = filename.lower().endswith(".gz") or stream.peek(2)[:2] == b"\x1f\x8b"
        if not is_gzip:
            return str(source), None
        path = temporary_dir / f"{input_index:08d}{_safe_parser_suffix(filename)}"
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


def _persist_uploaded_artifact(
    session: Session,
    *,
    record: ArtifactFileRecord,
) -> ArtifactFile:
    artifact = persist_artifact_file(session, record)
    if artifact.project_id != record.project_id:
        raise ArtifactUploadConflictError(
            "an identical artifact already belongs to a different project"
        )
    if artifact.artifact_kind is not record.artifact_kind:
        raise ArtifactUploadConflictError(
            "an identical artifact is already registered with a different artifact kind"
        )
    return artifact


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
    artifact = session.exec(
        select(ArtifactFile).where(ArtifactFile.content_sha256 == record.content_sha256)
    ).first()
    if artifact is None:
        artifact = ArtifactFile(**record.model_dump())
        if session.info.get("tricycle_fast_insert", False):
            _prepare_new_entity(session, artifact)
        else:
            session.add(artifact)
            session.flush()
        return artifact, None, False
    if artifact.size_bytes != record.size_bytes:
        raise ValueError("artifact SHA-256 resolved to a different byte size")
    if artifact.project_id != record.project_id:
        raise ArtifactUploadConflictError(
            "an identical artifact already belongs to a different project"
        )
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
        if not (
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
    existing_by_digest = {
        artifact.content_sha256: artifact
        for artifact in session.exec(
            select(ArtifactFile).where(col(ArtifactFile.content_sha256).in_(by_digest))
        ).all()
    }
    prepared: dict[
        str,
        tuple[ArtifactFile, _RetiredArtifactReservation | None, bool],
    ] = {}
    for digest, record in by_digest.items():
        artifact = existing_by_digest.get(digest)
        if artifact is None:
            artifact = ArtifactFile(**record.model_dump())
            _prepare_new_entity(session, artifact)
            prepared[digest] = (artifact, None, False)
            continue
        if artifact.size_bytes != record.size_bytes:
            raise ValueError("artifact SHA-256 resolved to a different byte size")
        if artifact.project_id != record.project_id:
            raise ArtifactUploadConflictError(
                "an identical artifact already belongs to a different project"
            )
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
            if not (
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
    return artifact_id, True


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


def _retire_artifacts_without_calculation_frames(
    session: Session,
    *,
    artifact_ids: set[UUID],
) -> dict[UUID, _RetiredArtifactObject]:
    if not artifact_ids:
        return {}
    artifacts = session.exec(
        select(ArtifactFile).where(col(ArtifactFile.id).in_(artifact_ids))
    ).all()
    _acquire_identity_locks(
        session,
        *sorted(
            {("artifact-content", artifact.content_sha256) for artifact in artifacts},
            key=str,
        ),
    )
    retired: dict[UUID, _RetiredArtifactObject] = {}
    for artifact in artifacts:
        artifact_id = _require_id(artifact, label="ArtifactFile")
        if artifact.storage_status is StorageStatus.RETIRED:
            continue
        retired[artifact_id] = _RetiredArtifactObject(
            bucket=artifact.bucket,
            object_key=artifact.object_key,
            version_id=artifact.version_id,
            content_sha256=artifact.content_sha256,
            size_bytes=artifact.size_bytes,
        )
        artifact.storage_status = StorageStatus.RETIRED
        session.add(artifact)
    return retired


def _delete_retired_artifact_object(reservation: _RetiredArtifactObject) -> None:
    settings = RustFSSettings().model_copy(update={"bucket": reservation.bucket})
    with RustFSObjectStore(settings) as store:
        if not store.exists(reservation.object_key, version_id=reservation.version_id):
            return
        metadata = store.head(reservation.object_key, version_id=reservation.version_id)
        if metadata.size != reservation.size_bytes or metadata.sha256 != reservation.content_sha256:
            raise ArtifactUploadError(
                "stored object metadata no longer matches the rejected artifact identity"
            )
        store.delete(reservation.object_key, version_id=reservation.version_id)


async def _delete_retired_artifact_objects(
    reservations: Mapping[UUID, _RetiredArtifactObject],
) -> None:
    for artifact_id, reservation in reservations.items():
        try:
            await asyncio.to_thread(_delete_retired_artifact_object, reservation)
        except Exception:
            logger.exception("failed to remove rejected artifact %s from RustFS", artifact_id)


async def _reject_artifact_without_calculation_frames(
    *,
    ingestion_id: UUID,
) -> ArtifactUploadResult:
    async with session_factory() as session:
        ingestion = await session.get(ArtifactIngestion, ingestion_id)
        if ingestion is None:
            raise ArtifactUploadError("artifact ingestion not found")
        error = NoCalculationFramesError(
            "source contains no QM calculation frames; artifact was filtered"
        )
        await session.run_sync(
            partial(
                _run_mark_ingestion_failed,
                ingestion_id=ingestion_id,
                error=error,
                error_code="no_calculation_frames",
                completed_at=datetime.now(UTC),
                source_frame_count=0,
                transition_state_frame_count=0,
            )
        )
        artifact_id = ingestion.artifact_file_id
        retired = await session.run_sync(
            cast(Any, partial(
                _retire_artifacts_without_calculation_frames,
                artifact_ids={artifact_id},
            ))
        )
        # Keep the content advisory lock held until the object has been
        # verified and removed. This closes the unversioned-object retry race.
        await _delete_retired_artifact_objects(retired)
        await session.commit()
    async with session_factory() as session:
        return await session.run_sync(partial(_run_result, ingestion_id=ingestion_id))


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
            if not should_delete:
                await session.commit()
                return
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
        if session.info.get("tricycle_fast_insert", False):
            _prepare_new_entity(session, ingestion)
        else:
            session.add(ingestion)
            session.flush()
    else:
        has_revision = session.exec(
            select(ParseRevision.id).where(ParseRevision.artifact_file_id == artifact_id)
        ).first()
        if has_revision is None:
            ingestion.status = ArtifactIngestionStatus.PENDING
            ingestion.source_frame_count = None
            ingestion.transition_state_frame_count = None
            ingestion.completed_at = None
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
            _prepare_new_entity(session, ingestion)
        elif artifact_id not in artifacts_with_revisions:
            ingestion.status = ArtifactIngestionStatus.PENDING
            ingestion.source_frame_count = None
            ingestion.transition_state_frame_count = None
            ingestion.completed_at = None
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
    topology_record, source_to_topology = _normalize_transition_state_endpoint_topology(
        endpoint,
        direction,
    )
    persisted_topology = persist_molecular_topology(
        session,
        topology_record,
        context=topology_context,
    )
    topology_id = _require_id(persisted_topology.topology, label="MolecularTopology")
    source_coordinate_hash = sha256(coordinates.tobytes(order="C")).hexdigest()
    endpoint_values = {
        "calculation_frame_id": frame_id,
        "calculation_frame": calculation_frame,
        "topology_id": topology_id,
        "topology": persisted_topology.topology,
        "direction": direction,
        "atom_count": endpoint.GetNumAtoms(),
        "displacement_ratio": displacement_ratio,
        "source_coordinates": coordinates,
        "source_coordinate_hash": source_coordinate_hash,
        "source_to_topology_atom_indices": source_to_topology,
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
        else TransitionStateEndpoint(**endpoint_values)
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
        },
    )


def _persist_transition_state_endpoints(
    session: Session,
    *,
    calculation_frame: CalculationFrame,
    inferred: _SuccessfulInference,
    topology_context: GeometryPersistenceContext | None = None,
    identity_is_new: bool = False,
    defer_flush: bool = False,
) -> None:
    _persist_transition_state_endpoint(
        session,
        calculation_frame=calculation_frame,
        endpoint=inferred.negative_endpoint,
        direction=TransitionStateEndpointDirection.NEGATIVE,
        displacement_ratio=inferred.negative_displacement_ratio,
        topology_context=topology_context,
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
        identity_is_new=identity_is_new,
        defer_flush=defer_flush,
    )


def persist_transition_state_endpoints_from_molop_frame(
    session: Session,
    *,
    calculation_frame: CalculationFrame,
    source_frame: BaseCalcFrame[Any],
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
            imaginary_frequency_cm1=float(frequency.to(atom_ureg.cm_1).magnitude),
            reaction_smiles="signed-mode-anchors-only",
            negative_endpoint=negative,
            positive_endpoint=positive,
            negative_displacement_ratio=negative_ratio,
            positive_displacement_ratio=positive_ratio,
        ),
    )


def _resolve_and_bind_transition_state_reaction(
    session: Session,
    *,
    inferred: _SuccessfulInference,
    calculation_frame: CalculationFrame,
    topology_context: GeometryPersistenceContext | None = None,
) -> tuple[UUID, UUID]:
    """Create the mapped endpoint reaction and bind its TS coordinate evidence."""

    cached_reaction_ids = (
        topology_context.inferred_reaction_ids_by_smiles.get(inferred.reaction_smiles)
        if topology_context is not None
        else None
    )
    if cached_reaction_ids is None:
        reaction_result = create_reaction_in_session(
            session,
            CreateReactionCommand(
                reaction=inferred.reaction_smiles,
                mapped_reaction_kind=MappedReactionKind.OTHER,
            ),
            defer_thermodynamic_refresh=is_transition_state_frame_eligible(
                calculation_frame.frame_role
            ),
            topology_context=topology_context,
            include_creation_metadata=topology_context is None,
            precomputed_topology_records=(
                topology_context.inferred_reaction_topology_records.get(
                    inferred.reaction_smiles
                )
                if topology_context is not None
                else None
            ),
            reconciliation_cache=(
                topology_context.reconciliation_cache if topology_context is not None else None
            ),
        )
        if reaction_result.mapped_reaction_id is None:
            raise ValueError("MolOP TS endpoint reaction did not produce a complete atom mapping")
        logical_reaction_id = reaction_result.logical_reaction_id
        mapped_reaction_id = reaction_result.mapped_reaction_id
        if topology_context is not None:
            topology_context.inferred_reaction_ids_by_smiles[inferred.reaction_smiles] = (
                logical_reaction_id,
                mapped_reaction_id,
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
            refresh_thermodynamics=topology_context is None,
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
) -> TransitionStateInference:
    logical_reaction_id, mapped_reaction_id = _resolve_and_bind_transition_state_reaction(
        session,
        inferred=inferred,
        calculation_frame=calculation_frame,
        topology_context=topology_context,
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
            "sampling_min_ratio": TS_PRE_POST_MIN_RATIO,
            "sampling_max_ratio": TS_PRE_POST_MAX_RATIO,
            "sampling_steps": TS_PRE_POST_STEPS,
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
        else TransitionStateInference(**inference_values)
    )
    _flush_new_entity(session, inference, label="TransitionStateInference")
    if _fast_insert_enabled(session):
        session.add(inference)
    if not defer_flush:
        _attach_pending_entities(session)
        session.flush()
    _persist_transition_state_endpoints(
        session,
        calculation_frame=calculation_frame,
        inferred=inferred,
        topology_context=topology_context,
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
            "artifact_ingestion": deferred.ingestion,
            "parse_revision_id": _require_id(deferred.parse_revision, label="ParseRevision"),
            "parse_revision": deferred.parse_revision,
            "file_frame_index": inferred.file_frame_index,
            "imaginary_mode_index": inferred.imaginary_mode_index,
            "imaginary_frequency_cm1": inferred.imaginary_frequency_cm1,
            "status": TransitionStateInferenceStatus.FAILED,
            "inference_method": "molop/possible_pre_post_ts",
            "inference_settings": {
                "endpoint_selection": "molop.possible_pre_post_ts",
                "sampling_min_ratio": TS_PRE_POST_MIN_RATIO,
                "sampling_max_ratio": TS_PRE_POST_MAX_RATIO,
                "sampling_steps": TS_PRE_POST_STEPS,
            },
            "error_code": error_code,
            "error_message": (
                error_message
                if error_message is not None
                else getattr(inferred, "error_message", None)
            ),
    }
    failed_inference = (
        _new_entity(session, TransitionStateInference, **values)
        if _fast_insert_enabled(session)
        else TransitionStateInference(**values)
    )
    _flush_new_entity(session, failed_inference, label="TransitionStateInference")
    if _fast_insert_enabled(session):
        session.add(failed_inference)


def _persist_one_new_inference(
    session: Session,
    task: _InferencePersistenceTask,
    *,
    topology_context: GeometryPersistenceContext | None,
) -> None:
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
        )
    except Exception as error:
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
    "geometries_to_reconcile",
    "reaction_participants_by_topology",
    "mapped_reactions_by_id",
    "inferred_reaction_ids_by_smiles",
    "inferred_reaction_topology_records",
)
_RECONCILIATION_CACHE_MUTABLE_FIELDS = (
    "nodes_by_reaction",
    "nodes_by_key",
    "loaded_reaction_nodes",
    "node_geometries_by_node",
    "loaded_node_geometries",
    "mappings_by_node_geometry_id",
    "loaded_mappings",
    "transition_state_paths_ready",
    "thermodynamics_refreshed_reactions",
    "new_node_geometry_ids",
    "thermodynamic_property_geometry_ids",
    "affected_reactions_by_id",
)


def _snapshot_inference_context(
    topology_context: GeometryPersistenceContext | None,
) -> tuple[dict[str, object], dict[str, object] | None, int] | None:
    if topology_context is None:
        return None
    context_state = {
        name: copy.copy(getattr(topology_context, name))
        for name in _INFERENCE_CONTEXT_MUTABLE_FIELDS
    }
    cache = topology_context.reconciliation_cache
    cache_state = (
        {
            name: copy.copy(getattr(cache, name))
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
        current.update(saved)  # type: ignore[arg-type]
    topology_context.inferred_reaction_cache_hits = cache_hits
    cache = topology_context.reconciliation_cache
    if cache_state is None or not isinstance(cache, ReconciliationBatchCache):
        return
    for name, saved in cache_state.items():
        current = getattr(cache, name)
        current.clear()
        current.update(saved)  # type: ignore[arg-type]


def _persist_inference_batch(
    session: Session,
    tasks: list[_InferencePersistenceTask],
    *,
    topology_context: GeometryPersistenceContext | None,
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
            )
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
                    )
                _attach_pending_entities(session)
                session.flush()
                _refresh_inference_reaction_profiles(
                    session,
                    topology_context=topology_context,
                )
        except Exception:
            _restore_inference_context(topology_context, context_snapshot)
            if pending_snapshot:
                session.info["_fast_pending_entities"] = pending_snapshot
            else:
                session.info.pop("_fast_pending_entities", None)
            for task in tasks:
                _persist_one_new_inference(
                    session,
                    task,
                    topology_context=topology_context,
                )
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
    for mapped_reaction in reactions:
        mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
        refresh_mapped_reaction_thermodynamics(session, mapped_reaction)
        cache.thermodynamics_refreshed_reactions.add(mapped_reaction_id)
    # New TS frames can add the same reaction in a later inference
    # microbatch, so retain only the dirty set for the current flush window.
    cache.affected_reactions_by_id.clear()


def _persist_artifact_inferences(
    session: Session,
    deferred: _DeferredArtifactInferences,
    *,
    topology_context: GeometryPersistenceContext | None = None,
) -> None:
    _persist_artifact_inferences_batch(
        session,
        [deferred],
        topology_context=topology_context,
    )


def _persist_artifact_inferences_batch(
    session: Session,
    deferred_items: list[_DeferredArtifactInferences],
    *,
    topology_context: GeometryPersistenceContext | None = None,
) -> None:
    pending_tasks: list[_InferencePersistenceTask] = []

    def flush_pending() -> None:
        nonlocal pending_tasks
        if pending_tasks:
            _persist_inference_batch(
                session,
                pending_tasks,
                topology_context=topology_context,
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
    artifact = ingestion.artifact_file
    if existing_revision_ids is None:
        existing_revision_ids = {
            revision_id
            for revision_id in session.exec(
                select(ParseRevision.id).where(ParseRevision.artifact_file_id == artifact.id)
            ).all()
            if isinstance(revision_id, UUID)
        }
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
        fast_insert=(
            _fast_molop_ingestion_enabled()
            and not existing_revision_ids
            and not force_new_revision
        ),
        parallel_frame_persistence=(
            _fast_molop_ingestion_enabled()
            and not existing_revision_ids
            and not force_new_revision
        ),
        geometry_context=geometry_context,
        preload_geometry_context=preload_geometry_context,
        defer_reconciliation=defer_reconciliation,
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
        _persist_artifact_inferences(session, inference_work)
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
    status = ArtifactIngestionStatus.PARTIAL if failures else ArtifactIngestionStatus.SUCCEEDED
    ingestion.status = status
    ingestion.source_frame_count = parsed.source_frame_count
    ingestion.transition_state_frame_count = len(parsed.inferences)
    ingestion.completed_at = completed_at
    ingestion.error_code = None
    ingestion.error_message = None
    ingestion.parser_metadata = {
        "source_format": parsed.source_format,
        "latest_parse_revision_id": str(parse_revision_id),
        "latest_parse_revision_created": revision_created,
        "ts_selection": "frame.is_TS is True",
        "inferred_reaction_identity": "shared topology-and-atom-mapping identity",
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
) -> None:
    ingestion = ingestion or session.get(ArtifactIngestion, ingestion_id)
    if ingestion is None:
        raise RuntimeError("artifact ingestion disappeared during parsing")
    ingestion.status = ArtifactIngestionStatus.FAILED
    ingestion.completed_at = completed_at
    if isinstance(error, GeometryAssignmentAmbiguityError):
        error_code = error.error_code
        ingestion.parser_metadata = {
            **ingestion.parser_metadata,
            "qc_rejection": error.evidence(),
        }
    ingestion.error_code = error_code
    ingestion.error_message = str(error) or type(error).__name__
    if source_frame_count is not None:
        ingestion.source_frame_count = source_frame_count
    if transition_state_frame_count is not None:
        ingestion.transition_state_frame_count = transition_state_frame_count
    session.add(ingestion)


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
    completion_by_ingestion_id: Mapping[UUID, _IngestionCompletion],
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
    inferences_by_ingestion_id: dict[UUID, list[TransitionStateInference]] = {
        ingestion_id: [] for ingestion_id in ingestion_ids
    }
    for inference in session.exec(
        select(TransitionStateInference)
        .where(col(TransitionStateInference.artifact_ingestion_id).in_(ingestion_ids))
        .order_by(col(TransitionStateInference.file_frame_index))
    ).all():
        inferences_by_ingestion_id.setdefault(inference.artifact_ingestion_id, []).append(inference)

    results: dict[UUID, ArtifactUploadResult] = {}
    for ingestion in ingestions:
        ingestion_id = _require_id(ingestion, label="ArtifactIngestion")
        parse_revision_id = parse_revision_by_ingestion_id[ingestion_id]
        rows = [
            row
            for row in inferences_by_ingestion_id[ingestion_id]
            if parse_revision_id is not None and row.parse_revision_id == parse_revision_id
        ]
        completion = completion_by_ingestion_id.get(ingestion_id)
        if completion is not None:
            failures = sum(row.status is TransitionStateInferenceStatus.FAILED for row in rows)
            ingestion.status = (
                ArtifactIngestionStatus.PARTIAL if failures else ArtifactIngestionStatus.SUCCEEDED
            )
            ingestion.source_frame_count = completion.source_frame_count
            ingestion.transition_state_frame_count = completion.transition_state_frame_count
            ingestion.completed_at = completion.completed_at
            ingestion.error_code = None
            ingestion.error_message = None
            ingestion.parser_metadata = {
                "source_format": completion.source_format,
                "latest_parse_revision_id": str(completion.parse_revision_id),
                "latest_parse_revision_created": completion.parse_revision_created,
                "ts_selection": "frame.is_TS is True",
                "inferred_reaction_identity": "shared topology-and-atom-mapping identity",
            }
            session.add(ingestion)
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
        select(ParseRevision.artifact_file_id, ParseRevision.id).where(
            col(ParseRevision.artifact_file_id).in_(artifact_ids)
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
        typed_session.flush()
        diagnostics = typed_session.info.get("_fast_bulk_insert_diagnostics")
        return dict(diagnostics) if isinstance(diagnostics, dict) else {}
    finally:
        typed_session.info["tricycle_fast_insert"] = previous_fast_insert


def _run_disable_autoflush(session: SQLAlchemySession) -> None:
    cast(Session, session).autoflush = False


def _run_persist_parsed_artifact(
    session: SQLAlchemySession,
    *,
    ingestion_id: UUID,
    parsed: _ParsedArtifact,
    started_at: datetime,
    completed_at: datetime,
    geometry_context: GeometryPersistenceContext | None = None,
    preload_geometry_context: bool = True,
    ingestion: ArtifactIngestion | None = None,
    existing_revision_ids: set[UUID] | None = None,
    defer_ingestion_completion: bool = False,
    defer_reconciliation: bool = False,
    deferred_inferences: list[_DeferredArtifactInferences] | None = None,
) -> tuple[UUID, bool]:
    return _persist_parsed_artifact(
        cast(Session, session),
        ingestion_id=ingestion_id,
        parsed=parsed,
        started_at=started_at,
        completed_at=completed_at,
        geometry_context=geometry_context,
        preload_geometry_context=preload_geometry_context,
        ingestion=ingestion,
        existing_revision_ids=existing_revision_ids,
        defer_ingestion_completion=defer_ingestion_completion,
        defer_reconciliation=defer_reconciliation,
        deferred_inferences=deferred_inferences,
    )


def _run_persist_deferred_inferences(
    session: SQLAlchemySession,
    *,
    deferred_inferences: list[_DeferredArtifactInferences],
    topology_context: GeometryPersistenceContext,
) -> None:
    typed_session = cast(Session, session)
    previous_fast_insert = typed_session.info.get("tricycle_fast_insert", False)
    typed_session.info["tricycle_fast_insert"] = True
    try:
        _persist_artifact_inferences_batch(
            typed_session,
            deferred_inferences,
            topology_context=topology_context,
        )
    finally:
        typed_session.info["tricycle_fast_insert"] = previous_fast_insert


def _run_reconcile_molop_geometry_context(
    session: SQLAlchemySession,
    *,
    context: GeometryPersistenceContext,
) -> None:
    reconcile_molop_geometry_context(cast(Session, session), context)


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


def _inference_topology_records(
    inferred: _SuccessfulInference,
    *,
    reaction_records: tuple[Any, ...] | None = None,
) -> list[Any]:
    """Build the shared topology identities consumed by TS inference persistence."""

    records: list[Any] = []
    for endpoint, direction in (
        (inferred.negative_endpoint, TransitionStateEndpointDirection.NEGATIVE),
        (inferred.positive_endpoint, TransitionStateEndpointDirection.POSITIVE),
    ):
        record, _source_to_topology = _normalize_transition_state_endpoint_topology(
            endpoint,
            direction,
        )
        records.append(record)
    if reaction_records is None:
        with suppress(Exception):
            reaction_records = tuple(
                reaction_topology_records(_reaction_from_representation(inferred.reaction_smiles))
            )
    if reaction_records:
        records.extend(reaction_records)
    return records


def _run_result(
    session: SQLAlchemySession,
    *,
    ingestion_id: UUID,
    parse_revision_id: UUID | None = None,
    parse_revision_created: bool | None = None,
) -> ArtifactUploadResult:
    return _result(
        cast(Session, session),
        ingestion_id,
        parse_revision_id=parse_revision_id,
        parse_revision_created=parse_revision_created,
    )


def _run_batch_results(
    session: SQLAlchemySession,
    *,
    parse_revision_by_ingestion_id: Mapping[UUID, UUID | None],
    parse_revision_created_by_ingestion_id: Mapping[UUID, bool | None],
    completion_by_ingestion_id: Mapping[UUID, _IngestionCompletion],
) -> dict[UUID, ArtifactUploadResult]:
    return _batch_results(
        cast(Session, session),
        parse_revision_by_ingestion_id=parse_revision_by_ingestion_id,
        parse_revision_created_by_ingestion_id=parse_revision_created_by_ingestion_id,
        completion_by_ingestion_id=completion_by_ingestion_id,
    )


def _run_preload_batch_persistence_state(
    session: SQLAlchemySession,
    *,
    ingestion_ids: list[UUID],
) -> tuple[dict[UUID, ArtifactIngestion], dict[UUID, set[UUID]]]:
    return _preload_batch_persistence_state(cast(Session, session), ingestion_ids=ingestion_ids)


def _stored_result(artifact: ArtifactFile) -> ArtifactUploadResult:
    return ArtifactUploadResult(
        artifact_id=_require_id(artifact, label="ArtifactFile"),
        artifact_kind=artifact.artifact_kind,
        storage_status=artifact.storage_status,
        inferred_reaction_count=0,
        inferences=[],
    )


async def _ingestion_failure_details(ingestion_id: UUID | None) -> tuple[str | None, str | None]:
    if ingestion_id is None:
        return None, None
    async with session_factory() as session:
        ingestion = await session.get(ArtifactIngestion, ingestion_id)
        if ingestion is None:
            return None, None
        return ingestion.error_code, ingestion.error_message


class ArtifactUploadService:
    """Authenticated content-addressed upload with optional calculation ingestion."""

    @classmethod
    async def _prepare_upload(
        cls,
        *,
        payload: bytes,
        filename: str,
        media_type: str,
        artifact_kind: ArtifactKind,
        project_id: UUID,
        user_id: UUID,
    ) -> _PreparedCalculationUpload | ArtifactUploadResult:
        """Reserve and store an upload, leaving calculation parsing for the caller."""

        settings = RustFSSettings()
        started_at = datetime.now(UTC)
        digest = sha256(payload).hexdigest()
        object_key = time_partitioned_content_addressed_key(
            payload,
            uploaded_at=started_at,
            prefix="uploads",
        )
        resolved_media_type = detect_artifact_media_type(filename, media_type, payload)
        record = ArtifactFileRecord(
            project_id=project_id,
            created_by_user_id=user_id,
            visibility=ArtifactVisibility.PROJECT,
            bucket=settings.bucket,
            object_key=object_key,
            content_sha256=digest,
            size_bytes=len(payload),
            original_filename=Path(filename).name,
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
            stored = await asyncio.to_thread(
                cls._store_payload,
                settings,
                object_key,
                payload,
                resolved_media_type,
                check_existing_object=check_existing_object,
            )
            if stored.size != len(payload) or stored.sha256 != digest:
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
            source=payload,
            size_bytes=len(payload),
            media_type=resolved_media_type,
            content_sha256=digest,
            retired_reservation=retired_reservation,
            needs_storage=True,
            check_existing_object=check_existing_object,
        )

    @classmethod
    async def upload(
        cls,
        *,
        payload: bytes,
        filename: str,
        media_type: str,
        artifact_kind: ArtifactKind,
        project_id: UUID,
        user_id: UUID,
    ) -> ArtifactUploadResult:
        if not payload:
            raise ArtifactUploadError("uploaded artifact is empty")
        _require_upload_size(payload)
        _require_decompressed_upload_size(payload, filename)
        await AuthorizationService.require_project_permission(
            user_id,
            project_id,
            ProjectPermission.ARTIFACT_UPLOAD,
        )
        prepared = await cls._prepare_upload(
            payload=payload,
            filename=filename,
            media_type=media_type,
            artifact_kind=artifact_kind,
            project_id=project_id,
            user_id=user_id,
        )
        if isinstance(prepared, ArtifactUploadResult):
            return prepared
        ingestion_id = _require_prepared_ingestion_id(prepared)
        started_at = prepared.started_at

        try:
            parsed = await _run_molop_file_parser(payload, filename)
            parsed = await _process_parsed_artifact_frames(
                parsed,
                submission_slots=asyncio.Semaphore(_frame_submission_limit()),
            )
        except Exception as error:
            parse_error = error
            async with session_factory() as session:
                await session.run_sync(
                    lambda sync_session: _mark_ingestion_failed(
                        cast(Session, sync_session),
                        ingestion_id=ingestion_id,
                        error=parse_error,
                        error_code="molop_parse_failed",
                        completed_at=datetime.now(UTC),
                    )
                )
                await session.commit()
                return await session.run_sync(
                    lambda sync_session: _result(cast(Session, sync_session), ingestion_id)
                )

        if parsed.source_frame_count == 0:
            return await _reject_artifact_without_calculation_frames(
                ingestion_id=ingestion_id,
            )

        try:
            async with session_factory() as session:
                parse_revision_id, parse_revision_created = await session.run_sync(
                    lambda sync_session: _persist_parsed_artifact(
                        cast(Session, sync_session),
                        ingestion_id=ingestion_id,
                        parsed=parsed,
                        started_at=started_at,
                        completed_at=datetime.now(UTC),
                    )
                )
                await session.commit()
                return await session.run_sync(
                    lambda sync_session: _result(
                        cast(Session, sync_session),
                        ingestion_id,
                        parse_revision_id=parse_revision_id,
                        parse_revision_created=parse_revision_created,
                    )
                )
        except Exception as error:
            persistence_error = error
            async with session_factory() as session:
                await session.run_sync(
                    lambda sync_session: _mark_ingestion_failed(
                        cast(Session, sync_session),
                        ingestion_id=ingestion_id,
                        error=persistence_error,
                        error_code="calculation_persistence_failed",
                        completed_at=datetime.now(UTC),
                    )
                )
                await session.commit()
                return await session.run_sync(
                    lambda sync_session: _result(cast(Session, sync_session), ingestion_id)
                )

    @classmethod
    async def reparse(
        cls,
        *,
        artifact_id: UUID,
        user_id: UUID,
    ) -> ArtifactUploadResult:
        """Parse a stored calculation artifact with the current parser identity."""

        started_at = datetime.now(UTC)
        async with session_factory() as session:
            artifact = await session.get(ArtifactFile, artifact_id)
            if artifact is None:
                raise ArtifactUploadError("artifact not found")
            await AuthorizationService.require_project_permission(
                user_id,
                artifact.project_id,
                ProjectPermission.ARTIFACT_UPLOAD,
            )
            if artifact.artifact_kind is not ArtifactKind.CALCULATION_OUTPUT:
                raise ArtifactUploadError("only calculation output artifacts can be reparsed")
            if artifact.storage_status is not StorageStatus.AVAILABLE:
                raise ArtifactUploadError("artifact bytes are not available for reparse")
            ingestion, _ = await session.run_sync(
                lambda sync_session: _create_pending_ingestion(
                    cast(Session, sync_session),
                    artifact=artifact,
                    started_at=started_at,
                )
            )
            ingestion_id = _require_id(ingestion, label="ArtifactIngestion")
            had_parse_revision = (
                await session.exec(
                    select(ParseRevision.id).where(ParseRevision.artifact_file_id == artifact_id)
                )
            ).first() is not None
            filename = artifact.original_filename
            expected_sha256 = artifact.content_sha256
            expected_size = artifact.size_bytes
            settings = RustFSSettings().model_copy(update={"bucket": artifact.bucket})
            object_key = artifact.object_key
            await session.commit()

        payload = await asyncio.to_thread(cls._load_payload, settings, object_key)
        if len(payload) != expected_size or sha256(payload).hexdigest() != expected_sha256:
            raise ArtifactUploadError("stored artifact bytes do not match database identity")
        _require_upload_size(payload)

        try:
            parsed = await _run_molop_file_parser(payload, filename)
            parsed = await _process_parsed_artifact_frames(
                parsed,
                submission_slots=asyncio.Semaphore(_frame_submission_limit()),
            )
        except Exception as error:
            parse_error = error
            if not had_parse_revision:
                async with session_factory() as session:
                    await session.run_sync(
                        lambda sync_session: _mark_ingestion_failed(
                            cast(Session, sync_session),
                            ingestion_id=ingestion_id,
                            error=parse_error,
                            error_code="molop_reparse_failed",
                            completed_at=datetime.now(UTC),
                        )
                    )
                    await session.commit()
            raise ArtifactUploadError(str(error) or type(error).__name__) from error

        if parsed.source_frame_count == 0 and not had_parse_revision:
            return await _reject_artifact_without_calculation_frames(
                ingestion_id=ingestion_id,
            )

        try:
            async with session_factory() as session:
                parse_revision_id, parse_revision_created = await session.run_sync(
                    lambda sync_session: _persist_parsed_artifact(
                        cast(Session, sync_session),
                        ingestion_id=ingestion_id,
                        parsed=parsed,
                        started_at=started_at,
                        completed_at=datetime.now(UTC),
                        force_new_revision=True,
                    )
                )
                await session.commit()
                return await session.run_sync(
                    lambda sync_session: _result(
                        cast(Session, sync_session),
                        ingestion_id,
                        parse_revision_id=parse_revision_id,
                        parse_revision_created=parse_revision_created,
                    )
                )
        except Exception as error:
            persistence_error = error
            if not had_parse_revision:
                async with session_factory() as session:
                    await session.run_sync(
                        lambda sync_session: _mark_ingestion_failed(
                            cast(Session, sync_session),
                            ingestion_id=ingestion_id,
                            error=persistence_error,
                            error_code="calculation_reparse_persistence_failed",
                            completed_at=datetime.now(UTC),
                        )
                    )
                    await session.commit()
            raise ArtifactUploadError(str(error) or type(error).__name__) from error

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
        parsed = await _run_molop_file_parser(payload, filename)
        parsed = await _process_parsed_artifact_frames(
            parsed,
            submission_slots=asyncio.Semaphore(_frame_submission_limit()),
        )
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

        async with session_factory() as session:
            previous_fast_insert = session.info.get("tricycle_fast_insert", False)
            previous_autoflush = session.autoflush
            session.info["tricycle_fast_insert"] = True
            session.autoflush = False
            try:
                reservations_by_digest = await session.run_sync(
                    partial(
                        _run_prepare_pending_uploads,
                        records=[record for _, _, _, record, _ in candidates],
                    )
                )
                artifacts_by_digest = {
                    digest: artifact
                    for digest, (artifact, _retired, _check_existing) in (
                        reservations_by_digest.items()
                    )
                }
                ingestions_by_artifact_id: dict[UUID, tuple[ArtifactIngestion, bool]] = {}
                if artifact_kind is ArtifactKind.CALCULATION_OUTPUT:
                    started_by_artifact_id = {
                        _require_id(
                            artifacts_by_digest[record.content_sha256],
                            label="ArtifactFile",
                        ): started_at
                        for _, _, _, record, started_at in candidates
                    }
                    ingestions_by_artifact_id = await session.run_sync(
                        partial(
                            _run_create_pending_ingestions,
                            artifacts=list(artifacts_by_digest.values()),
                            started_by_artifact_id=started_by_artifact_id,
                        )
                    )
                for index, _file, inspected, record, started_at in candidates:
                    artifact, retired_reservation, check_existing_object = reservations_by_digest[
                        record.content_sha256
                    ]
                    artifact_id = _require_id(artifact, label="ArtifactFile")
                    ingestion_id: UUID | None = None
                    skip_parse = False
                    if artifact_kind is ArtifactKind.CALCULATION_OUTPUT:
                        ingestion, created = ingestions_by_artifact_id[artifact_id]
                        ingestion_id = _require_id(ingestion, label="ArtifactIngestion")
                        skip_parse = (
                            not created and ingestion.status is not ArtifactIngestionStatus.PENDING
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
                        duplicate_of=None,
                    )
                # Duplicate content identities reuse the first reservation and
                # object key; no extra INSERT/UPDATE is needed for the sibling.
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
                        duplicate_of=first_index,
                    )
                await session.run_sync(_run_flush)
                await session.commit()
            finally:
                session.autoflush = previous_autoflush
                session.info["tricycle_fast_insert"] = previous_fast_insert
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
    ) -> ArtifactBatchUploadResult:
        """Prepare once, then advance files through an asynchronous pipeline.

        Each RustFS completion submits that file to the shared MolOP process
        pool. A single bounded consumer writes parse results to the database;
        the final transaction remains atomic for the request.
        """

        timings: dict[str, float] = {}
        started = perf_counter()
        source_inspections = _require_batch_upload_budget(files)
        timings["validate_budget_ms"] = (perf_counter() - started) * 1000
        prepare_function = getattr(cls._prepare_upload, "__func__", cls._prepare_upload)
        if prepare_function is not _ORIGINAL_PREPARE_UPLOAD:
            return await cls._upload_batch_with_prepare_hook(
                files=files,
                artifact_kind=artifact_kind,
                project_id=project_id,
                user_id=user_id,
                on_file_parsed=on_file_parsed,
            )
        phase_started = perf_counter()
        await AuthorizationService.require_project_permission(
            user_id,
            project_id,
            ProjectPermission.ARTIFACT_UPLOAD,
        )
        timings["authorize_ms"] = (perf_counter() - phase_started) * 1000

        phase_started = perf_counter()
        prepared, item_by_index = await cls._prepare_upload_batch(
            files=files,
            artifact_kind=artifact_kind,
            project_id=project_id,
            user_id=user_id,
            source_inspections=source_inspections,
        )
        timings["prepare_db_ms"] = (perf_counter() - phase_started) * 1000

        stored: dict[int, Any] = {}
        storage_errors: dict[int, Exception] = {}
        phase_started = perf_counter()
        storage_pool = _get_storage_process_pool(get_settings().upload_max_concurrency)
        frame_submission_slots = asyncio.Semaphore(_frame_submission_limit())
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
                            value = await asyncio.to_thread(
                                cls._store_payload,
                                reservation.settings,
                                reservation.object_key,
                                reservation.source,
                                reservation.media_type,
                                content_sha256=reservation.content_sha256,
                                size_bytes=reservation.size_bytes,
                                check_existing_object=reservation.check_existing_object,
                            )
                        else:
                            # Test and extension overrides historically receive
                            # the original four-argument bytes contract.
                            value = await asyncio.to_thread(
                                cls._store_payload,
                                reservation.settings,
                                reservation.object_key,
                                reservation.source,
                                reservation.media_type,
                            )
                    else:
                        value = await loop.run_in_executor(
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
            parse_started_at = perf_counter()
            parsed: _ParsedArtifact | Exception
            molop_started_at = perf_counter()
            try:
                parsed = await _run_molop_source_parser(
                    reservation.source,
                    files[index].filename,
                    artifact_sha256=reservation.content_sha256,
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
                molgr_started_at = perf_counter()
                try:
                    parsed = await _process_parsed_artifact_frames(
                        parsed,
                        submission_slots=frame_submission_slots,
                    )
                except Exception as error:
                    parsed = error
                molgr_finished_at = perf_counter()
                async with storage_completion_lock:
                    if (
                        molgr_reconstruction_phase_started_at is None
                        or molgr_started_at < molgr_reconstruction_phase_started_at
                    ):
                        molgr_reconstruction_phase_started_at = molgr_started_at
                    molgr_reconstruction_phase_finished_at = max(
                        molgr_reconstruction_phase_finished_at or molgr_finished_at,
                        molgr_finished_at,
                    )
                    molgr_reconstruction_elapsed_ms += (
                        molgr_finished_at - molgr_started_at
                    ) * 1000
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
        ] = asyncio.Queue(maxsize=PERSISTENCE_PRELOAD_BATCH_SIZE)

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
            asyncio.create_task(enqueue_pipeline_result(index, reservation))
            for index, reservation in prepared.items()
        ]

        parse_indices: list[int] = []
        for index, reservation in prepared.items():
            if (
                reservation.ingestion_id is not None
                and not reservation.skip_parse
            ):
                parse_indices.append(index)

        # Open the single atomic persistence transaction while parser workers
        # are still running. The bounded queue provides backpressure, while
        # consuming completed files here lets CPU parsing overlap database
        # writes instead of creating a full-batch barrier.
        persistence_pipeline_started = perf_counter()
        async with session_factory() as session:
            persist_preload_started = perf_counter()
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
            geometry_context = GeometryPersistenceContext()
            persist_preload_elapsed_ms = (perf_counter() - persist_preload_started) * 1000
            persist_write_elapsed_ms = 0.0
            parse_errors_by_index: dict[int, Exception] = {}
            persisted_revisions_by_index: dict[int, tuple[UUID, bool]] = {}
            completion_by_ingestion_id: dict[UUID, _IngestionCompletion] = {}
            deferred_inferences: list[_DeferredArtifactInferences] = []
            pending_preload: list[tuple[int, _ParsedArtifact]] = []
            no_frame_indices: set[int] = set()
            retired_no_frame_objects: dict[UUID, _RetiredArtifactObject] = {}

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

            async def persist_parsed_files(
                parsed_files: list[tuple[int, _ParsedArtifact]],
            ) -> None:
                nonlocal persist_preload_elapsed_ms, persist_write_elapsed_ms
                inference_topology_records: list[Any] = []
                for _, parsed in parsed_files:
                    for inferred in parsed.inferences:
                        if not isinstance(inferred, _SuccessfulInference):
                            continue
                        try:
                            cached_reaction_records = (
                                geometry_context.inferred_reaction_topology_records.get(
                                    inferred.reaction_smiles
                                )
                            )
                            records = _inference_topology_records(
                                inferred,
                                reaction_records=cached_reaction_records,
                            )
                            inference_topology_records.extend(records)
                            if cached_reaction_records is None and len(records) > 2:
                                geometry_context.inferred_reaction_topology_records.setdefault(
                                    inferred.reaction_smiles,
                                    tuple(records[2:]),
                                )
                        except Exception:
                            # Endpoint normalization is retried inside the
                            # per-inference savepoint below, where it becomes
                            # inferred_reaction_persistence_failed.
                            logger.warning(
                                "failed to preload inferred reaction topology for frame %s",
                                inferred.file_frame_index,
                                exc_info=True,
                            )
                file_preload_started = perf_counter()
                await session.run_sync(
                    partial(
                        _run_preload_molecular_geometry_context,
                        parsed_artifacts=[parsed for _, parsed in parsed_files],
                        context=geometry_context,
                        topology_records=inference_topology_records,
                    )
                )
                persist_preload_elapsed_ms += (perf_counter() - file_preload_started) * 1000
                for local_index, parsed in parsed_files:
                    original_index = parse_indices[local_index]
                    reservation = prepared[original_index]
                    ingestion_id = _require_prepared_ingestion_id(reservation)
                    ingestion = persistence_ingestions_by_id[ingestion_id]
                    persist_write_started = perf_counter()
                    parse_revision_id, parse_revision_created = await session.run_sync(
                        partial(
                            _run_persist_parsed_artifact,
                            ingestion_id=ingestion_id,
                            parsed=parsed,
                            started_at=reservation.started_at,
                            completed_at=datetime.now(UTC),
                            geometry_context=geometry_context,
                            preload_geometry_context=False,
                            ingestion=ingestion,
                            existing_revision_ids=(
                                persistence_revision_ids_by_artifact_id[ingestion.artifact_file_id]
                            ),
                            defer_ingestion_completion=True,
                            defer_reconciliation=True,
                            deferred_inferences=deferred_inferences,
                        )
                    )
                    persist_write_elapsed_ms += (perf_counter() - persist_write_started) * 1000
                    persisted_revisions_by_index[original_index] = (
                        parse_revision_id,
                        parse_revision_created,
                    )
                    completion_by_ingestion_id[ingestion_id] = _IngestionCompletion(
                        parse_revision_id=parse_revision_id,
                        parse_revision_created=parse_revision_created,
                        source_frame_count=parsed.source_frame_count,
                        transition_state_frame_count=len(parsed.inferences),
                        source_format=parsed.source_format,
                        completed_at=datetime.now(UTC),
                    )

            async def persist_completed_file(local_index: int, parser_result: Any) -> None:
                parsed = normalize_parser_result(parser_result)
                original_index = parse_indices[local_index]
                if on_file_parsed is not None:
                    await on_file_parsed(
                        original_index,
                        isinstance(parsed, _ParsedArtifact) and parsed.source_frame_count > 0,
                    )
                if isinstance(parsed, Exception):
                    parse_errors_by_index[original_index] = parsed
                    return
                if parsed.source_frame_count == 0:
                    no_frame_error = NoCalculationFramesError(
                        "source contains no QM calculation frames; artifact was filtered"
                    )
                    no_frame_indices.add(original_index)
                    parse_errors_by_index[original_index] = no_frame_error
                    return
                pending_preload.append((local_index, parsed))

            parse_pipeline_started = persistence_pipeline_started
            local_index_by_original = {
                original_index: local_index
                for local_index, original_index in enumerate(parse_indices)
            }
            for _ in pipeline_tasks:
                index, storage_error, parsed = await pipeline_result_queue.get()
                if storage_error is not None:
                    storage_errors[index] = storage_error
                    local_index = local_index_by_original.get(index)
                    if local_index is not None:
                        parse_errors_by_index[index] = storage_error
                    continue
                local_index = local_index_by_original.get(index)
                if local_index is not None:
                    await persist_completed_file(local_index, parsed)
                    # Flush a partial microbatch when no later completion is
                    # already queued, so an early file can enter persistence
                    # while slower files are still in MolOP.
                    if pending_preload and (
                        len(pending_preload) >= PERSISTENCE_PRELOAD_BATCH_SIZE
                        or pipeline_result_queue.empty()
                    ):
                        batch = pending_preload.copy()
                        pending_preload.clear()
                        await persist_parsed_files(batch)

            await asyncio.gather(*pipeline_tasks)

            # Failures are accumulated while all process-pool stages drain and
            # are written only now, inside the same final batch transaction as
            # successful frame records.
            for original_index, parse_error in parse_errors_by_index.items():
                ingestion_id = _require_prepared_ingestion_id(prepared[original_index])
                ingestion = persistence_ingestions_by_id[ingestion_id]
                persist_write_started = perf_counter()
                await session.run_sync(
                    partial(
                        _run_mark_ingestion_failed,
                        ingestion_id=ingestion_id,
                        error=parse_error,
                        error_code=(
                            "artifact_storage_failed"
                            if original_index in storage_errors
                            else
                            "no_calculation_frames"
                            if original_index in no_frame_indices
                            else "molop_parse_failed"
                        ),
                        completed_at=datetime.now(UTC),
                        ingestion=ingestion,
                        source_frame_count=(
                            0 if original_index in no_frame_indices else None
                        ),
                        transition_state_frame_count=(
                            0 if original_index in no_frame_indices else None
                        ),
                    )
                )
                persist_write_elapsed_ms += (perf_counter() - persist_write_started) * 1000
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
                (
                    molgr_reconstruction_phase_finished_at
                    - molgr_reconstruction_phase_started_at
                )
                * 1000
                if (
                    molgr_reconstruction_phase_started_at is not None
                    and molgr_reconstruction_phase_finished_at is not None
                )
                else 0.0
            )
            timings["molgr_frame_reconstruction_sum_ms"] = molgr_reconstruction_elapsed_ms
            timings["parse_ms"] = timings["molop_parse_ms"]
            timings["parse_persistence_pipeline_ms"] = (
                perf_counter() - parse_pipeline_started
            ) * 1000
            if pending_preload:
                batch = pending_preload.copy()
                pending_preload.clear()
                await persist_parsed_files(batch)

            storage_phase_finished = storage_phase_finished_at or perf_counter()
            timings["storage_ms"] = (storage_phase_finished - phase_started) * 1000
            storage_db_started = perf_counter()
            await session.run_sync(
                partial(
                    _run_mark_uploads_available,
                    stored_by_artifact_id={
                        prepared[index].artifact_id: (prepared[index].object_key, value)
                        for index, value in stored.items()
                    },
                )
            )
            timings["storage_db_ms"] = (perf_counter() - storage_db_started) * 1000

            # Persist the large revision-local tables once across the complete
            # batch before reaction inference introduces shared-identity flushes.
            persist_write_started = perf_counter()
            flush_started = perf_counter()
            bulk_diagnostics = await session.run_sync(_run_flush)
            timings["persist_flush_initial_ms"] = (perf_counter() - flush_started) * 1000
            if isinstance(bulk_diagnostics, dict):
                timings["persist_bulk_pending_rows"] = float(
                    bulk_diagnostics.get("pending", 0)
                )
                timings["persist_bulk_transient_rows"] = float(
                    bulk_diagnostics.get("transient", 0)
                )
                timings["persist_bulk_prepare_ms"] = float(
                    bulk_diagnostics.get("prepare_ms", 0.0)
                )
                timings["persist_bulk_execute_ms"] = float(
                    bulk_diagnostics.get("execute_ms", 0.0)
                )
            reconcile_started = perf_counter()
            await session.run_sync(
                partial(
                    _run_reconcile_molop_geometry_context,
                    context=geometry_context,
                )
            )
            timings["persist_reconcile_geometry_ms"] = (
                perf_counter() - reconcile_started
            ) * 1000
            inference_started = perf_counter()
            await session.run_sync(
                partial(
                    _run_persist_deferred_inferences,
                    deferred_inferences=deferred_inferences,
                    topology_context=geometry_context,
                )
            )
            timings["persist_deferred_inferences_ms"] = (
                perf_counter() - inference_started
            ) * 1000
            retire_started = perf_counter()
            retired_no_frame_objects = await session.run_sync(
                cast(Any, partial(
                    _retire_artifacts_without_calculation_frames,
                    artifact_ids={prepared[index].artifact_id for index in no_frame_indices},
                ))
            )
            timings["persist_retire_no_frame_ms"] = (perf_counter() - retire_started) * 1000
            timings["persist_inferred_reaction_cache_hits"] = float(
                geometry_context.inferred_reaction_cache_hits
            )
            persist_write_elapsed_ms += (perf_counter() - persist_write_started) * 1000
            timings["persist_preload_db_ms"] = persist_preload_elapsed_ms
            timings["persist_write_db_ms"] = persist_write_elapsed_ms
            persist_result_started = perf_counter()
            parse_revision_by_ingestion_id: dict[UUID, UUID | None] = {}
            parse_revision_created_by_ingestion_id: dict[UUID, bool | None] = {}
            for original_index in parse_indices:
                ingestion_id = _require_prepared_ingestion_id(prepared[original_index])
                persisted_revision = persisted_revisions_by_index.get(original_index)
                parse_revision_by_ingestion_id[ingestion_id] = (
                    persisted_revision[0] if persisted_revision is not None else None
                )
                parse_revision_created_by_ingestion_id[ingestion_id] = (
                    persisted_revision[1] if persisted_revision is not None else None
                )
            flush_started = perf_counter()
            await session.run_sync(_run_flush)
            timings["persist_flush_results_ms"] = (perf_counter() - flush_started) * 1000
            results_by_ingestion_id = await session.run_sync(
                partial(
                    _run_batch_results,
                    parse_revision_by_ingestion_id=parse_revision_by_ingestion_id,
                    parse_revision_created_by_ingestion_id=(parse_revision_created_by_ingestion_id),
                    completion_by_ingestion_id=completion_by_ingestion_id,
                )
            )
            timings["persist_result_db_ms"] = (perf_counter() - persist_result_started) * 1000
            lock_stats = await session.run_sync(
                lambda sync_session: dict(
                    cast(Session, sync_session).info.get("_identity_lock_stats", {})
                )
            )
            timings["advisory_lock_calls"] = float(lock_stats.get("calls", 0))
            timings["advisory_lock_requested_ids"] = float(
                lock_stats.get("requested_ids", 0)
            )
            timings["advisory_lock_uncached_ids"] = float(
                lock_stats.get("uncached_ids", 0)
            )
            for prefix, count in sorted(lock_stats.get("prefixes", {}).items()):
                timings[f"advisory_lock_{prefix}_calls"] = float(count)
            for original_index in parse_indices:
                reservation = prepared[original_index]
                ingestion_id = _require_prepared_ingestion_id(reservation)
                result = results_by_ingestion_id[ingestion_id]
                parse_error = parse_errors_by_index.get(original_index)
                error_code = (
                    "no_calculation_frames"
                    if original_index in no_frame_indices
                    else "artifact_storage_failed"
                    if original_index in storage_errors
                    else "molop_parse_failed"
                    if parse_error is not None
                    else None
                )
                item_by_index[original_index] = ArtifactBatchUploadItem(
                    filename=files[original_index].filename,
                    succeeded=(
                        parse_error is None
                        and result.ingestion_status is not ArtifactIngestionStatus.FAILED
                    ),
                    result=result,
                    error_code=error_code,
                    error_message=(
                        (str(parse_error) or type(parse_error).__name__)
                        if parse_error is not None
                        else None
                    ),
                )
            persist_commit_started = perf_counter()
            # The retirement helper acquired each content lock in this same
            # transaction; delete before commit so an unversioned retry cannot
            # replace the object between retirement and cleanup.
            await _delete_retired_artifact_objects(retired_no_frame_objects)
            await session.commit()
        timings["persist_commit_db_ms"] = (perf_counter() - persist_commit_started) * 1000
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
                source_item = item_by_index[reservation.duplicate_of]
                item_by_index[index] = source_item.model_copy(
                    update={"filename": files[index].filename}
                )
                continue
            item_by_index[index] = ArtifactBatchUploadItem(
                filename=files[index].filename,
                succeeded=index not in storage_errors,
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

    @classmethod
    async def _upload_batch_with_prepare_hook(
        cls,
        *,
        files: list[ArtifactUploadPayload],
        artifact_kind: ArtifactKind,
        project_id: UUID,
        user_id: UUID,
        on_file_parsed: Callable[[int, bool], Awaitable[None]] | None,
    ) -> ArtifactBatchUploadResult:
        """Preserve dependency-injected single-upload test doubles.

        Production never enters this adapter: it only applies when an embedding
        or test replaces the class's private preparation hook.
        """

        await AuthorizationService.require_project_permission(
            user_id,
            project_id,
            ProjectPermission.ARTIFACT_UPLOAD,
        )
        items: list[ArtifactBatchUploadItem] = []
        for index, file in enumerate(files):
            try:
                payload = _upload_payload_bytes(file)
                prepared = await cls._prepare_upload(
                    payload=payload,
                    filename=file.filename,
                    media_type=file.media_type,
                    artifact_kind=artifact_kind,
                    project_id=project_id,
                    user_id=user_id,
                )
                if not isinstance(prepared, ArtifactUploadResult):
                    raise RuntimeError("prepare-hook adapters must return ArtifactUploadResult")
                succeeded = prepared.ingestion_status is not ArtifactIngestionStatus.FAILED
                items.append(
                    ArtifactBatchUploadItem(
                        filename=file.filename,
                        succeeded=succeeded,
                        result=prepared,
                        error_code=None if succeeded else "ingestion_failed",
                    )
                )
            except Exception as error:
                items.append(
                    ArtifactBatchUploadItem(
                        filename=file.filename,
                        succeeded=False,
                        error_code="artifact_upload_failed",
                        error_message=str(error) or type(error).__name__,
                    )
                )
            if on_file_parsed is not None:
                await on_file_parsed(index, items[-1].succeeded)
        succeeded_count = sum(item.succeeded for item in items)
        results = [item.result for item in items if item.result is not None]
        return ArtifactBatchUploadResult(
            total_count=len(items),
            succeeded_count=succeeded_count,
            failed_count=len(items) - succeeded_count,
            source_frame_count=sum(result.source_frame_count or 0 for result in results),
            transition_state_frame_count=sum(
                result.transition_state_frame_count or 0 for result in results
            ),
            inferred_reaction_count=sum(result.inferred_reaction_count for result in results),
            items=items,
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


_ORIGINAL_PREPARE_UPLOAD = cast(
    Any,
    ArtifactUploadService.__dict__["_prepare_upload"],
).__func__
_ORIGINAL_STORE_PAYLOAD = cast(
    Any,
    ArtifactUploadService.__dict__["_store_payload"],
).__func__


__all__ = [
    "ArtifactUploadConflictError",
    "ArtifactUploadError",
    "ArtifactUploadPayload",
    "ArtifactUploadService",
]
