"""Types shared by the artifact upload pipeline.

The upload service coordinates storage, parsing, and persistence, but the
objects exchanged between those stages do not need to live in that
orchestrator.  Keeping them here also gives the smaller pipeline modules a
dependency direction that does not point back at :mod:`artifact_uploads`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from rdkit import Chem

from tricycle_reaction_db.db.models import (
    ArtifactIngestion,
    CalculationFrame,
    ParseRevision,
)
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ParseCompleteness,
)
from tricycle_reaction_db.ingestion import MolOPFrameRecords
from tricycle_reaction_db.storage.rustfs import RustFSSettings


class ArtifactUploadError(RuntimeError):
    """Base error for failures that belong to one artifact upload."""


class MolOPFileParseTimeoutError(ArtifactUploadError):
    """One file exceeded the bounded MolOP plus post-processing budget."""

    error_code = "molop_parse_timeout"

    def __init__(self, message: str) -> None:
        super().__init__(f"[{self.error_code}] {message}")


class ArtifactUploadLimitError(ArtifactUploadError):
    """Upload bytes or file count exceed a configured hard resource budget."""


class ArtifactUploadConflictError(ArtifactUploadError):
    """The requested artifact conflicts with an existing catalogue identity."""


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
    charge: int
    multiplicity: int


@dataclass(frozen=True, slots=True)
class _FailedInference:
    file_frame_index: int
    imaginary_mode_index: int
    imaginary_frequency_cm1: float
    error_code: str
    error_message: str
    error_metadata: dict[str, Any] | None = None


_Inference = _SuccessfulInference | _FailedInference


@dataclass(frozen=True, slots=True)
class _ParsedArtifact:
    # Production parsing keeps the owning-process ChemFile so topology
    # reconstruction can be deferred until persistence.
    chem_file: Any
    frame_records: tuple[MolOPFrameRecords, ...]
    source_frame_count: int
    source_format: str | None
    source_compression: str | None
    inferences: tuple[_Inference, ...]
    record_sha256: str | None = None
    artifact_sha256: str | None = None
    parse_diagnostics: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ParsedArtifactTask:
    """One durable queue item after RustFS verification and MolOP parsing."""

    artifact_id: UUID
    started_at: datetime
    parsed: _ParsedArtifact | Exception


@dataclass(frozen=True, slots=True)
class _ProcessedFrame:
    """One frame after MolGR reconstruction and ingestion-level validation."""

    file_frame_index: int
    record: MolOPFrameRecords | None
    inference: _Inference | None
    topology_reconstruction_status: str | None
    error_code: str | None = None
    error_message: str | None = None
    error_type: str | None = None
    error_metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class _IngestionCompletion:
    parse_revision_id: UUID
    parse_revision_created: bool
    source_frame_count: int
    transition_state_frame_count: int
    source_format: str | None
    completed_at: datetime
    parse_completeness: ParseCompleteness = ParseCompleteness.COMPLETE
    parse_diagnostics: tuple[dict[str, Any], ...] = ()


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
    # Optional manifest identity. These values are checked against the bytes
    # after the source has been inspected, immediately before persistence.
    relative_path: str | None = None
    expected_sha256: str | None = None
    expected_size_bytes: int | None = None


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
    force_new_revision: bool = False
    ingestion_status: ArtifactIngestionStatus | None = None
    duplicate_of: int | None = None


@dataclass(frozen=True, slots=True)
class _InspectedUploadSource:
    source: bytes | Path
    size_bytes: int
    content_sha256: str
    media_probe: bytes


__all__ = [
    "ArtifactUploadConflictError",
    "ArtifactUploadError",
    "ArtifactUploadLimitError",
    "ArtifactUploadPayload",
    "MolOPFileParseTimeoutError",
    "NoCalculationFramesError",
    "_DeferredArtifactInferences",
    "_FailedInference",
    "_Inference",
    "_InferencePersistenceTask",
    "_IngestionCompletion",
    "_InspectedUploadSource",
    "_ParsedArtifact",
    "ParsedArtifactTask",
    "_PreparedCalculationUpload",
    "_ProcessedFrame",
    "_RetiredArtifactReservation",
    "_SuccessfulInference",
]
