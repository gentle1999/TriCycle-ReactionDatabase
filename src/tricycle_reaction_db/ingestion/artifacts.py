"""Content-address raw artifacts and normalize calculation protocols."""

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from tricycle_reaction_db.application.dtos.artifacts import (
    ArtifactFileRecord,
    CalculationProtocolRecord,
)
from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    ArtifactVisibility,
    QMSoftware,
    StorageStatus,
)
from tricycle_reaction_db.ingestion.media_type import detect_artifact_media_type

CALCULATION_PROTOCOL_VERSION = "calculation-protocol-v1"


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_record_from_path(
    path: Path,
    *,
    bucket: str,
    artifact_kind: ArtifactKind,
    storage_status: StorageStatus = StorageStatus.PENDING,
) -> ArtifactFileRecord:
    """Build a RustFS catalogue record without uploading the source file."""

    content_sha256 = _hash_file(path)
    with path.open("rb") as stream:
        sample = stream.read(64 * 1024)
    media_type = detect_artifact_media_type(path.name, None, sample)
    return ArtifactFileRecord(
        bucket=bucket,
        object_key=f"raw/sha256/{content_sha256[:2]}/{content_sha256}",
        visibility=ArtifactVisibility.PUBLIC,
        content_sha256=content_sha256,
        size_bytes=path.stat().st_size,
        original_filename=path.name,
        media_type=media_type,
        artifact_kind=artifact_kind,
        storage_status=storage_status,
    )


def calculation_protocol_record(
    *,
    qm_software: QMSoftware,
    qm_software_version: str,
    normalized_spec: dict[str, Any],
    task_requests: list[str],
    method_family: str | None = None,
    method: str | None = None,
    reference_method: str | None = None,
    functional: str | None = None,
    basis_set: str | None = None,
    auxiliary_basis_set: str | None = None,
    dispersion_model: str | None = None,
    solvation_model: str | None = None,
    solvent: str | None = None,
    relativistic_method: str | None = None,
) -> CalculationProtocolRecord:
    """Content-address a normalized protocol specification."""

    normalized_tasks = sorted(set(task_requests))
    identity = {
        "schema_version": CALCULATION_PROTOCOL_VERSION,
        "qm_software": qm_software.value,
        "qm_software_version": qm_software_version,
        "model_chemistry": {
            "method_family": method_family,
            "method": method,
            "reference_method": reference_method,
            "functional": functional,
            "basis_set": basis_set,
            "auxiliary_basis_set": auxiliary_basis_set,
            "dispersion_model": dispersion_model,
            "solvation_model": solvation_model,
            "solvent": solvent,
            "relativistic_method": relativistic_method,
        },
        "task_requests": normalized_tasks,
        "normalized_spec": normalized_spec,
    }
    protocol_hash = sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return CalculationProtocolRecord(
        protocol_hash=protocol_hash,
        spec_schema_version=CALCULATION_PROTOCOL_VERSION,
        qm_software=qm_software,
        qm_software_version=qm_software_version,
        method_family=method_family,
        method=method,
        reference_method=reference_method,
        functional=functional,
        basis_set=basis_set,
        auxiliary_basis_set=auxiliary_basis_set,
        dispersion_model=dispersion_model,
        solvation_model=solvation_model,
        solvent=solvent,
        relativistic_method=relativistic_method,
        task_requests=normalized_tasks,
        normalized_spec=normalized_spec,
    )


__all__ = [
    "CALCULATION_PROTOCOL_VERSION",
    "artifact_record_from_path",
    "calculation_protocol_record",
]
