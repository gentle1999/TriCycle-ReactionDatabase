"""Upload artifacts and infer reactions from every MolOP TS frame."""

from __future__ import annotations

import asyncio
import gzip
import io
import logging
import tempfile
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import numpy as np
from molop import AutoParser
from molop.io.base_models.ChemFileFrame import BaseCalcFrame
from molop.unit import atom_ureg
from rdkit import Chem
from sqlalchemy.orm import Session as SQLAlchemySession
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
from tricycle_reaction_db.application.services.molecular_geometry import (
    GeometryAssignmentAmbiguityError,
    persist_molecular_topology,
)
from tricycle_reaction_db.application.services.molop_artifact_ingestion import (
    persist_molop_calculation_artifact,
)
from tricycle_reaction_db.application.services.reaction_commands import (
    create_reaction_in_session,
)
from tricycle_reaction_db.application.services.reaction_geometry_reconciliation import (
    bind_transition_state_frame,
    ensure_transition_state_path,
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
)

MOLOP_VERSION = version("molop")
logger = logging.getLogger(__name__)
# Start with the smallest useful displacement and expand only when crowding
# removes one side of the signed mode pair.
INFERENCE_RATIOS = (1.0, 1.25, 1.5)
# ``BaseCalcFrame.vibrate`` samples a linspace.  Endpoint inference must only
# evaluate the two signed extrema, otherwise a rejected extremum can silently
# persist an intermediate displacement ratio such as 0.8333.
INFERENCE_STEPS = 2


class ArtifactUploadError(RuntimeError):
    pass


class ArtifactUploadLimitError(ArtifactUploadError):
    """Upload bytes or file count exceed a configured hard resource budget."""


class ArtifactUploadConflictError(ArtifactUploadError):
    pass


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
    chem_file: Any
    frame_records: tuple[MolOPFrameRecords, ...]
    source_frame_count: int
    source_format: str | None
    source_compression: str | None
    inferences: tuple[_Inference, ...]


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
class _PreparedCalculationUpload:
    settings: RustFSSettings
    artifact_id: UUID
    object_key: str
    ingestion_id: UUID
    started_at: datetime


def _safe_parser_suffix(filename: str) -> str:
    name = Path(filename).name.lower()
    if name.endswith(".gz"):
        name = name[:-3]
    suffix = Path(name).suffix
    return suffix if suffix in {".log", ".out", ".xyz"} else ".log"


_molop_parse_semaphore: asyncio.Semaphore | None = None
_molop_parse_slots: int | None = None
_molop_parse_loop: asyncio.AbstractEventLoop | None = None


def _get_molop_parse_semaphore() -> asyncio.Semaphore:
    global _molop_parse_loop, _molop_parse_semaphore, _molop_parse_slots
    slots = get_settings().molop_parse_slots
    loop = asyncio.get_running_loop()
    if (
        _molop_parse_semaphore is None
        or _molop_parse_slots != slots
        or _molop_parse_loop is not loop
    ):
        _molop_parse_semaphore = asyncio.Semaphore(slots)
        _molop_parse_slots = slots
        _molop_parse_loop = loop
    return _molop_parse_semaphore


async def _run_molop_parser(function: Any, *args: Any, **kwargs: Any) -> Any:
    """Run one parser task under the process-level parse-slot budget."""

    semaphore = _get_molop_parse_semaphore()
    async with semaphore:
        return await asyncio.to_thread(function, *args, **kwargs)


def _require_upload_size(payload: bytes) -> None:
    maximum = get_settings().max_upload_bytes
    if len(payload) > maximum:
        raise ArtifactUploadError(f"uploaded artifact exceeds the {maximum}-byte limit")


def _require_batch_upload_budget(files: list[ArtifactUploadPayload]) -> None:
    """Validate every batch dimension before authorization, storage, or parsing."""

    settings = get_settings()
    if len(files) > settings.max_batch_files:
        raise ArtifactUploadLimitError(
            f"upload batch exceeds the {settings.max_batch_files}-file limit"
        )
    total_bytes = 0
    for file in files:
        if file.payload is None and file.spool_path is None:
            continue
        payload_size = (
            len(file.payload) if file.payload is not None else file.spool_path.stat().st_size  # type: ignore[union-attr]
        )
        if payload_size > settings.max_upload_bytes:
            raise ArtifactUploadLimitError(
                f"uploaded artifact exceeds the {settings.max_upload_bytes}-byte limit"
            )
        total_bytes += payload_size
        if total_bytes > settings.max_batch_bytes:
            raise ArtifactUploadLimitError(
                f"upload batch exceeds the {settings.max_batch_bytes}-byte limit"
            )
        _require_decompressed_upload_size(_upload_payload_bytes(file), file.filename)


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
    return f"{sides[0]}>>{sides[1]}"


def _signed_ts_endpoints(
    frame: BaseCalcFrame[Any],
    vibration_position: int,
) -> tuple[Chem.Mol, Chem.Mol, float, float]:
    """Return the first/last valid signed vibration structures in source order."""

    if frame.vibrations is None or not len(frame.vibrations):
        raise ValueError("TS frame has no vibration mode")
    center = np.asarray(frame.coords.to(atom_ureg.angstrom).magnitude, dtype=np.float64)
    mode = np.asarray(
        frame.vibrations[vibration_position].vibration_mode.to(atom_ureg.angstrom).magnitude,
        dtype=np.float64,
    )
    mode_norm = float(np.sum(np.square(mode)))
    if mode.shape != center.shape or mode_norm <= 0:
        raise ValueError("TS imaginary mode does not match the source coordinates")
    for ratio in INFERENCE_RATIOS:
        molecules = frame.vibrate(
            vibration_id=vibration_position,
            ratio=ratio,
            steps=INFERENCE_STEPS,
        )
        candidates: list[tuple[float, Chem.Mol]] = []
        for molecule in molecules:
            endpoint = molecule.rdmol
            if not isinstance(endpoint, Chem.Mol) or endpoint.GetNumConformers() != 1:
                continue
            coordinates = np.asarray(
                endpoint.GetConformer().GetPositions(),
                dtype=np.float64,
            )
            signed_ratio = float(np.sum((center - coordinates) * mode) / mode_norm)
            candidates.append((signed_ratio, endpoint))
        if len(candidates) > 1:
            candidates.sort(key=lambda item: item[0])
            negative_ratio, negative = candidates[0]
            positive_ratio, positive = candidates[-1]
            if negative_ratio < 0 < positive_ratio:
                return (
                    Chem.Mol(negative),
                    Chem.Mol(positive),
                    abs(negative_ratio),
                    positive_ratio,
                )
    raise ValueError("Failed to generate signed TS vibration endpoints")


def _parsed_artifact_from_chem_file(
    chem_file: Any,
    *,
    source_compression: str | None,
) -> _ParsedArtifact:
    inferred: list[_Inference] = []
    for fallback_index, frame in enumerate(chem_file):
        if not isinstance(frame, BaseCalcFrame):
            continue
        if frame.is_TS is not True:
            continue
        file_frame_index = frame.file_frame_index
        if file_frame_index is None:
            file_frame_index = fallback_index
        vibrations = frame.vibrations
        if vibrations is None or len(vibrations.imaginary_idxs) != 1:
            continue
        imaginary_position = vibrations.imaginary_idxs[0]
        imaginary_mode_index = (
            vibrations.mode_indices[imaginary_position]
            if vibrations.mode_indices
            else imaginary_position
        )
        frequency = vibrations[imaginary_position].frequency
        if frequency is None:
            continue
        frequency_cm1 = float(frequency.to(atom_ureg.cm_1).magnitude)
        if frame.topology_reconstruction_status == "suspicious_fallback":
            inferred.append(
                _FailedInference(
                    file_frame_index=file_frame_index,
                    imaginary_mode_index=imaginary_mode_index,
                    imaginary_frequency_cm1=frequency_cm1,
                    error_code="ts_topology_untrusted",
                    error_message=(
                        "MolGR returned a suspicious fallback topology; "
                        "TS endpoint inference was skipped"
                    ),
                )
            )
            continue
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
                endpoint_atoms = [atom.GetAtomicNum() for atom in endpoint.GetAtoms()]  # type: ignore[no-untyped-call]
                if endpoint_atoms != frame.atoms:
                    raise ValueError(
                        "MolOP TS endpoint atom order differs from the TS source frame"
                    )
            inferred.append(
                _SuccessfulInference(
                    file_frame_index=file_frame_index,
                    imaginary_mode_index=imaginary_mode_index,
                    imaginary_frequency_cm1=frequency_cm1,
                    reaction_smiles=_mapped_reaction_smiles(reactant, product),
                    negative_endpoint=negative_endpoint,
                    positive_endpoint=positive_endpoint,
                    negative_displacement_ratio=negative_displacement_ratio,
                    positive_displacement_ratio=positive_displacement_ratio,
                )
            )
        except Exception as error:
            inferred.append(
                _FailedInference(
                    file_frame_index=file_frame_index,
                    imaginary_mode_index=imaginary_mode_index,
                    imaginary_frequency_cm1=frequency_cm1,
                    error_code="ts_endpoint_inference_failed",
                    error_message=str(error) or type(error).__name__,
                )
            )
    return _ParsedArtifact(
        chem_file=chem_file,
        frame_records=tuple(
            frame_records_from_molop(frame, export_schema_version=chem_file.schema_version)
            for frame in chem_file
        ),
        source_frame_count=len(chem_file),
        source_format=chem_file.source_format,
        source_compression=source_compression,
        inferences=tuple(inferred),
    )


def _parse_calculation_output(payload: bytes, filename: str) -> _ParsedArtifact:
    configure_molecular_graph_reconstruction()
    decoded_payload, source_compression = _parser_payload(payload, filename)
    with tempfile.NamedTemporaryFile(suffix=_safe_parser_suffix(filename)) as temporary:
        temporary.write(decoded_payload)
        temporary.flush()
        parsed_batch = AutoParser(
            temporary.name,
            n_jobs=1,
            parser_detection="auto",
            capture_source_evidence=True,
            release_file_content=True,
        )
        if len(parsed_batch) != 1:
            raise ArtifactUploadError("MolOP did not produce exactly one parsed artifact")
        return _parsed_artifact_from_chem_file(
            parsed_batch[0],
            source_compression=source_compression,
        )


def _parse_calculation_outputs_batch(
    files: list[tuple[bytes | Path, str]],
    *,
    n_jobs: int,
) -> dict[int, _ParsedArtifact | Exception]:
    """Parse all supplied files in one MolOP batch while retaining input order."""

    configure_molecular_graph_reconstruction()
    with tempfile.TemporaryDirectory(prefix="tricycle-molop-batch-") as temporary_dir:
        parsed_by_index: dict[int, _ParsedArtifact | Exception] = {}
        paths: list[str] = []
        file_indices: list[int] = []
        compressions: list[str | None] = []
        for index, (source, filename) in enumerate(files):
            try:
                payload = source.read_bytes() if isinstance(source, Path) else source
                decoded_payload, source_compression = _parser_payload(payload, filename)
            except Exception as error:
                parsed_by_index[index] = error
                continue
            path = Path(temporary_dir) / f"{index:08d}{_safe_parser_suffix(filename)}"
            path.write_bytes(decoded_payload)
            paths.append(str(path))
            file_indices.append(index)
            compressions.append(source_compression)
        if not paths:
            return parsed_by_index
        try:
            parsed_report = AutoParser(
                paths,
                n_jobs=n_jobs,
                return_report=True,
                parser_detection="auto",
                capture_source_evidence=True,
                release_file_content=True,
            )
        except Exception as error:
            parsed_by_index.update(dict.fromkeys(file_indices, error))
            return parsed_by_index

        for fallback_index, outcome in enumerate(parsed_report.outcomes):
            parser_index = outcome.input_index
            if parser_index is None:
                parser_index = fallback_index
            if parser_index < 0 or parser_index >= len(file_indices):
                continue
            input_index = file_indices[parser_index]
            if outcome.succeeded and outcome.value is not None:
                try:
                    parsed_by_index[input_index] = _parsed_artifact_from_chem_file(
                        outcome.value,
                        source_compression=compressions[parser_index],
                    )
                except Exception as error:
                    parsed_by_index[input_index] = error
            else:
                failure = outcome.failure
                message = (
                    failure.message
                    if failure is not None
                    else f"MolOP parse status: {outcome.status}"
                )
                parsed_by_index[input_index] = ArtifactUploadError(message)
        for index in file_indices:
            parsed_by_index.setdefault(
                index,
                ArtifactUploadError("MolOP did not return an outcome for this input file"),
            )
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
) -> tuple[ArtifactFile, _RetiredArtifactReservation | None]:
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
        session.add(artifact)
        session.flush()
        return artifact, None
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
        session.flush()
    return artifact, retired_reservation


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
    session.flush()
    return artifact


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


def _persist_transition_state_endpoint(
    session: Session,
    *,
    calculation_frame: CalculationFrame,
    endpoint: Chem.Mol,
    direction: TransitionStateEndpointDirection,
    displacement_ratio: float,
) -> TransitionStateEndpoint:
    """Persist one signed endpoint without creating a normalized Geometry.

    Topology identity is canonicalized for reuse, but the Cartesian payload is
    intentionally kept in the original TS source atom order.  This preserves
    the exact common coordinate frame used by the TS and both displaced modes.
    """

    frame_id = _require_id(calculation_frame, label="CalculationFrame")
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
    topology_record, source_to_topology = normalize_topology_with_mapping(
        endpoint,
        add_hydrogens=False,
        reconstruction_method="molop/ts-vibration",
        reconstruction_version=MOLOP_VERSION,
        reconstruction_metadata={
            "coordinate_frame": "calculation_frame.observed_coordinates",
            "coordinate_policy": "source-cartesian-no-independent-normalization",
            "direction": direction.value,
        },
    )
    persisted_topology = persist_molecular_topology(session, topology_record)
    topology_id = _require_id(persisted_topology.topology, label="MolecularTopology")
    source_coordinate_hash = sha256(coordinates.tobytes(order="C")).hexdigest()
    endpoint_row = TransitionStateEndpoint(
        calculation_frame_id=frame_id,
        calculation_frame=calculation_frame,
        topology_id=topology_id,
        topology=persisted_topology.topology,
        direction=direction,
        atom_count=endpoint.GetNumAtoms(),
        displacement_ratio=displacement_ratio,
        source_coordinates=coordinates,
        source_coordinate_hash=source_coordinate_hash,
        source_to_topology_atom_indices=source_to_topology,
        provenance={
            "method": "molop.ts_vibration",
            "molop_version": MOLOP_VERSION,
            "coordinate_frame": "calculation_frame.observed_coordinates",
            "coordinate_order": "molop_source_atom_order",
            "direction": direction.value,
        },
    )
    session.add(endpoint_row)
    session.flush()
    return endpoint_row


def _persist_transition_state_endpoints(
    session: Session,
    *,
    calculation_frame: CalculationFrame,
    inferred: _SuccessfulInference,
) -> None:
    _persist_transition_state_endpoint(
        session,
        calculation_frame=calculation_frame,
        endpoint=inferred.negative_endpoint,
        direction=TransitionStateEndpointDirection.NEGATIVE,
        displacement_ratio=inferred.negative_displacement_ratio,
    )
    _persist_transition_state_endpoint(
        session,
        calculation_frame=calculation_frame,
        endpoint=inferred.positive_endpoint,
        direction=TransitionStateEndpointDirection.POSITIVE,
        displacement_ratio=inferred.positive_displacement_ratio,
    )


def persist_transition_state_endpoints_from_molop_frame(
    session: Session,
    *,
    calculation_frame: CalculationFrame,
    source_frame: BaseCalcFrame[Any],
) -> None:
    """Persist signed mode anchors for an already-persisted MolOP TS frame."""

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
) -> tuple[UUID, UUID]:
    """Create the mapped endpoint reaction and bind its TS coordinate evidence."""

    reaction_result = create_reaction_in_session(
        session,
        CreateReactionCommand(
            reaction=inferred.reaction_smiles,
            mapped_reaction_kind=MappedReactionKind.OTHER,
        ),
    )
    if reaction_result.mapped_reaction_id is None:
        raise ValueError("MolOP TS endpoint reaction did not produce a complete atom mapping")
    mapped_reaction = session.get(MappedReaction, reaction_result.mapped_reaction_id)
    if mapped_reaction is None:
        raise RuntimeError("MolOP TS inference created a missing MappedReaction")
    ensure_transition_state_path(
        session,
        mapped_reaction=mapped_reaction,
    )
    if is_transition_state_frame_eligible(calculation_frame.frame_role):
        bind_transition_state_frame(
            session,
            mapped_reaction=mapped_reaction,
            calculation_frame=calculation_frame,
        )
    return reaction_result.logical_reaction_id, reaction_result.mapped_reaction_id


def _persist_successful_inference(
    session: Session,
    *,
    ingestion: ArtifactIngestion,
    parse_revision: ParseRevision,
    inferred: _SuccessfulInference,
    calculation_frame: CalculationFrame,
) -> TransitionStateInference:
    logical_reaction_id, mapped_reaction_id = _resolve_and_bind_transition_state_reaction(
        session,
        inferred=inferred,
        calculation_frame=calculation_frame,
    )
    inference = TransitionStateInference(
        artifact_ingestion_id=_require_id(ingestion, label="ArtifactIngestion"),
        artifact_ingestion=ingestion,
        parse_revision_id=_require_id(parse_revision, label="ParseRevision"),
        parse_revision=parse_revision,
        file_frame_index=inferred.file_frame_index,
        imaginary_mode_index=inferred.imaginary_mode_index,
        imaginary_frequency_cm1=inferred.imaginary_frequency_cm1,
        status=TransitionStateInferenceStatus.SUCCEEDED,
        inference_settings={
            "ratio_attempts": list(INFERENCE_RATIOS),
            "steps": INFERENCE_STEPS,
            "reaction_side_semantics": "fragment-rich endpoint first",
            "direction_semantics": "coords - normal_mode * signed_ratio",
            "imaginary_mode_index": inferred.imaginary_mode_index,
        },
        logical_reaction_id=logical_reaction_id,
        mapped_reaction_id=mapped_reaction_id,
        calculation_frame_id=_require_id(calculation_frame, label="CalculationFrame"),
    )
    session.add(inference)
    session.flush()
    _persist_transition_state_endpoints(
        session,
        calculation_frame=calculation_frame,
        inferred=inferred,
    )
    return inference


def _persist_parsed_artifact(
    session: Session,
    *,
    ingestion_id: UUID,
    parsed: _ParsedArtifact,
    started_at: datetime,
    completed_at: datetime,
    force_new_revision: bool = False,
) -> tuple[UUID, bool]:
    ingestion = session.get(ArtifactIngestion, ingestion_id)
    if ingestion is None:
        raise RuntimeError("artifact ingestion disappeared during parsing")
    artifact = ingestion.artifact_file
    existing_revision_ids = set(
        session.exec(
            select(ParseRevision.id).where(ParseRevision.artifact_file_id == artifact.id)
        ).all()
    )
    persisted_artifact = persist_molop_calculation_artifact(
        session,
        artifact=artifact,
        chem_file=parsed.chem_file,
        records=list(parsed.frame_records),
        source_compression=parsed.source_compression,
        started_at=started_at,
        completed_at=completed_at,
        force_new_revision=force_new_revision,
        fast_insert=not existing_revision_ids and not force_new_revision,
    )
    parse_revision = persisted_artifact.parse_revision
    parse_revision_id = _require_id(parse_revision, label="ParseRevision")
    revision_created = parse_revision_id not in existing_revision_ids
    for inferred in parsed.inferences:
        existing = session.exec(
            select(TransitionStateInference).where(
                TransitionStateInference.parse_revision_id == parse_revision_id,
                TransitionStateInference.file_frame_index == inferred.file_frame_index,
            )
        ).first()
        if existing is not None:
            if (
                existing.status is TransitionStateInferenceStatus.SUCCEEDED
                and existing.mapped_reaction_id is not None
                and existing.calculation_frame_id is not None
            ):
                mapped_reaction = session.get(MappedReaction, existing.mapped_reaction_id)
                calculation_frame = session.get(CalculationFrame, existing.calculation_frame_id)
                if mapped_reaction is None or calculation_frame is None:
                    raise RuntimeError(
                        "successful TS inference references missing reaction evidence"
                    )
                ensure_transition_state_path(session, mapped_reaction=mapped_reaction)
                if is_transition_state_frame_eligible(calculation_frame.frame_role):
                    bind_transition_state_frame(
                        session,
                        mapped_reaction=mapped_reaction,
                        calculation_frame=calculation_frame,
                    )
                if isinstance(inferred, _SuccessfulInference):
                    _persist_transition_state_endpoints(
                        session,
                        calculation_frame=calculation_frame,
                        inferred=inferred,
                    )
            continue
        if isinstance(inferred, _FailedInference):
            session.add(
                TransitionStateInference(
                    artifact_ingestion_id=ingestion_id,
                    artifact_ingestion=ingestion,
                    parse_revision_id=parse_revision_id,
                    parse_revision=parse_revision,
                    file_frame_index=inferred.file_frame_index,
                    imaginary_mode_index=inferred.imaginary_mode_index,
                    imaginary_frequency_cm1=inferred.imaginary_frequency_cm1,
                    status=TransitionStateInferenceStatus.FAILED,
                    inference_settings={
                        "ratio_attempts": list(INFERENCE_RATIOS),
                        "steps": INFERENCE_STEPS,
                    },
                    error_code=inferred.error_code,
                    error_message=inferred.error_message,
                )
            )
            continue
        try:
            calculation_frame = persisted_artifact.frames_by_file_index.get(
                inferred.file_frame_index
            )
            if calculation_frame is None:
                raise ValueError("persisted calculation is missing the MolOP TS frame")
            with session.begin_nested():
                _persist_successful_inference(
                    session,
                    ingestion=ingestion,
                    parse_revision=parse_revision,
                    inferred=inferred,
                    calculation_frame=calculation_frame,
                )
        except Exception as error:
            session.add(
                TransitionStateInference(
                    artifact_ingestion_id=ingestion_id,
                    artifact_ingestion=ingestion,
                    parse_revision_id=parse_revision_id,
                    parse_revision=parse_revision,
                    file_frame_index=inferred.file_frame_index,
                    imaginary_mode_index=inferred.imaginary_mode_index,
                    imaginary_frequency_cm1=inferred.imaginary_frequency_cm1,
                    status=TransitionStateInferenceStatus.FAILED,
                    inference_settings={
                        "ratio_attempts": list(INFERENCE_RATIOS),
                        "steps": INFERENCE_STEPS,
                    },
                    error_code="inferred_reaction_persistence_failed",
                    error_message=str(error) or type(error).__name__,
                )
            )

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
) -> None:
    ingestion = session.get(ArtifactIngestion, ingestion_id)
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


def _run_mark_ingestion_failed(
    session: SQLAlchemySession,
    *,
    ingestion_id: UUID,
    error: Exception,
    error_code: str,
    completed_at: datetime,
) -> None:
    _mark_ingestion_failed(
        cast(Session, session),
        ingestion_id=ingestion_id,
        error=error,
        error_code=error_code,
        completed_at=completed_at,
    )


def _run_persist_parsed_artifact(
    session: SQLAlchemySession,
    *,
    ingestion_id: UUID,
    parsed: _ParsedArtifact,
    started_at: datetime,
    completed_at: datetime,
) -> tuple[UUID, bool]:
    return _persist_parsed_artifact(
        cast(Session, session),
        ingestion_id=ingestion_id,
        parsed=parsed,
        started_at=started_at,
        completed_at=completed_at,
    )


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
            artifact, retired_reservation = await session.run_sync(
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
        ingestion_id = prepared.ingestion_id
        started_at = prepared.started_at

        try:
            parsed = await _run_molop_parser(_parse_calculation_output, payload, filename)
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
            parsed = await _run_molop_parser(_parse_calculation_output, payload, filename)
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
        parsed = await _run_molop_parser(_parse_calculation_output, payload, filename)
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
    async def upload_batch(
        cls,
        *,
        files: list[ArtifactUploadPayload],
        artifact_kind: ArtifactKind,
        project_id: UUID,
        user_id: UUID,
    ) -> ArtifactBatchUploadResult:
        """Store a batch, parse calculation files together, then persist independently.

        MolOP owns the CPU-bound file scheduling.  Database persistence remains
        per-ingestion and ordered so a failed file cannot roll back successful
        files or amplify concurrent SQL flush contention.
        """

        _require_batch_upload_budget(files)
        await AuthorizationService.require_project_permission(
            user_id,
            project_id,
            ProjectPermission.ARTIFACT_UPLOAD,
        )
        items: list[ArtifactBatchUploadItem | None] = [None] * len(files)
        prepared: dict[int, _PreparedCalculationUpload] = {}
        parse_inputs: list[tuple[bytes | Path, str]] = []
        parse_indices: list[int] = []

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
                payload = _upload_payload_bytes(file)
                if not payload:
                    raise ArtifactUploadError("uploaded artifact is empty")
                reservation = await cls._prepare_upload(
                    payload=payload,
                    filename=file.filename,
                    media_type=file.media_type,
                    artifact_kind=artifact_kind,
                    project_id=project_id,
                    user_id=user_id,
                )
                if isinstance(reservation, ArtifactUploadResult):
                    succeeded = reservation.ingestion_status is not ArtifactIngestionStatus.FAILED
                    error_code, error_message = (
                        await _ingestion_failure_details(reservation.ingestion_id)
                        if not succeeded
                        else (None, None)
                    )
                    items[index] = ArtifactBatchUploadItem(
                        filename=file.filename,
                        succeeded=succeeded,
                        result=reservation,
                        error_code=error_code or ("ingestion_failed" if not succeeded else None),
                        error_message=error_message,
                    )
                else:
                    prepared[index] = reservation
                    parse_indices.append(index)
                    parse_inputs.append((file.spool_path or payload, file.filename))
            except Exception as error:
                logger.exception("batch artifact upload failed for %s", file.filename)
                items[index] = ArtifactBatchUploadItem(
                    filename=file.filename,
                    succeeded=False,
                    error_code="artifact_upload_failed",
                    error_message=str(error) or type(error).__name__,
                )

        parsed_by_input: dict[int, _ParsedArtifact | Exception] = {}
        if parse_inputs:
            parsed_by_input = await _run_molop_parser(
                _parse_calculation_outputs_batch,
                parse_inputs,
                n_jobs=get_settings().molop_batch_n_jobs,
            )

        for local_index, original_index in enumerate(parse_indices):
            reservation = prepared[original_index]
            file = files[original_index]
            ingestion_id = reservation.ingestion_id
            started_at = reservation.started_at
            parsed = parsed_by_input.get(local_index)
            if parsed is None:
                parsed = ArtifactUploadError("MolOP did not return a result for this input file")
            if isinstance(parsed, Exception):
                parse_error = parsed
                async with session_factory() as session:
                    await session.run_sync(
                        partial(
                            _run_mark_ingestion_failed,
                            ingestion_id=ingestion_id,
                            error=parse_error,
                            error_code="molop_parse_failed",
                            completed_at=datetime.now(UTC),
                        )
                    )
                    await session.commit()
                    result = await session.run_sync(partial(_run_result, ingestion_id=ingestion_id))
                items[original_index] = ArtifactBatchUploadItem(
                    filename=file.filename,
                    succeeded=False,
                    result=result,
                    error_code="molop_parse_failed",
                    error_message=str(parsed) or type(parsed).__name__,
                )
                continue
            try:
                parsed_artifact = parsed
                async with session_factory() as session:
                    parse_revision_id, parse_revision_created = await session.run_sync(
                        partial(
                            _run_persist_parsed_artifact,
                            ingestion_id=ingestion_id,
                            parsed=parsed_artifact,
                            started_at=started_at,
                            completed_at=datetime.now(UTC),
                        )
                    )
                    await session.commit()
                    result = await session.run_sync(
                        partial(
                            _run_result,
                            ingestion_id=ingestion_id,
                            parse_revision_id=parse_revision_id,
                            parse_revision_created=parse_revision_created,
                        )
                    )
                succeeded = result.ingestion_status is not ArtifactIngestionStatus.FAILED
                error_code, error_message = (
                    await _ingestion_failure_details(result.ingestion_id)
                    if not succeeded
                    else (None, None)
                )
                items[original_index] = ArtifactBatchUploadItem(
                    filename=file.filename,
                    succeeded=succeeded,
                    result=result,
                    error_code=error_code or ("ingestion_failed" if not succeeded else None),
                    error_message=error_message,
                )
            except Exception as error:
                logger.exception("batch artifact persistence failed for %s", file.filename)
                persistence_error = error
                async with session_factory() as session:
                    await session.run_sync(
                        partial(
                            _run_mark_ingestion_failed,
                            ingestion_id=ingestion_id,
                            error=persistence_error,
                            error_code="calculation_persistence_failed",
                            completed_at=datetime.now(UTC),
                        )
                    )
                    await session.commit()
                    result = await session.run_sync(partial(_run_result, ingestion_id=ingestion_id))
                items[original_index] = ArtifactBatchUploadItem(
                    filename=file.filename,
                    succeeded=False,
                    result=result,
                    error_code="calculation_persistence_failed",
                    error_message=str(error) or type(error).__name__,
                )

        complete_items = [item for item in items if item is not None]
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
            items=complete_items,
        )

    @staticmethod
    def _store_payload(
        settings: RustFSSettings,
        object_key: str,
        payload: bytes,
        media_type: str,
    ) -> Any:
        with RustFSObjectStore(settings) as store:
            store.ensure_bucket()
            if store.exists(object_key):
                return store.head(object_key)
            return store.put_bytes(
                key=object_key,
                payload=payload,
                content_type=media_type,
                metadata={"ingestion": "transition-state-upload"},
            )

    @staticmethod
    def _load_payload(settings: RustFSSettings, object_key: str) -> bytes:
        with RustFSObjectStore(settings) as store:
            return store.get_bytes(object_key)


__all__ = [
    "ArtifactUploadConflictError",
    "ArtifactUploadError",
    "ArtifactUploadPayload",
    "ArtifactUploadService",
]
