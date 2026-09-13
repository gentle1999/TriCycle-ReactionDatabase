"""Content-address raw artifacts and normalize calculation protocols."""

import json
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from tricycle_reaction_db.application.dtos.artifacts import (
    ArtifactFileRecord,
    CalculationProtocolRecord,
)
from tricycle_reaction_db.core.chemistry_config import CALCULATION_PROTOCOL_VERSION
from tricycle_reaction_db.core.protocol_normalization import (
    normalize_functional_and_dispersion,
    normalize_protocol_text,
)
from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    ArtifactVisibility,
    QMSoftware,
    StorageStatus,
)
from tricycle_reaction_db.ingestion.media_type import detect_artifact_media_type


def _normalized_protocol_spec(
    normalized_spec: dict[str, Any],
    *,
    functional: str | None,
    basis_set: str | None,
    auxiliary_basis_set: str | None,
    dispersion_model: str | None,
) -> dict[str, Any]:
    """Project normalized fields while retaining changed source protocol data."""

    spec = dict(normalized_spec)
    source_protocol = spec.get("protocol")
    if not isinstance(source_protocol, Mapping):
        return spec

    raw_protocol = dict(source_protocol)
    normalized_protocol = dict(raw_protocol)
    normalized_protocol["functional"] = functional
    normalized_protocol["basis_set"] = basis_set
    normalized_protocol["auxiliary_basis_set"] = auxiliary_basis_set
    normalized_protocol["dispersion_correction"] = dispersion_model
    if normalized_protocol != raw_protocol:
        spec.setdefault("source_protocol", raw_protocol)
    spec["protocol"] = normalized_protocol
    return spec


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

    normalized_functional, normalized_dispersion_model = normalize_functional_and_dispersion(
        functional,
        dispersion_model,
    )
    normalized_basis_set = normalize_protocol_text(basis_set)
    normalized_auxiliary_basis_set = normalize_protocol_text(auxiliary_basis_set)
    normalized_spec = _normalized_protocol_spec(
        normalized_spec,
        functional=normalized_functional,
        basis_set=normalized_basis_set,
        auxiliary_basis_set=normalized_auxiliary_basis_set,
        dispersion_model=normalized_dispersion_model,
    )
    normalized_tasks = sorted(set(task_requests))
    identity = {
        "schema_version": CALCULATION_PROTOCOL_VERSION,
        "qm_software": qm_software.value,
        "qm_software_version": qm_software_version,
        "model_chemistry": {
            "method_family": method_family,
            "method": method,
            "reference_method": reference_method,
            "functional": normalized_functional,
            "basis_set": normalized_basis_set,
            "auxiliary_basis_set": normalized_auxiliary_basis_set,
            "dispersion_model": normalized_dispersion_model,
            "solvation_model": solvation_model,
            "solvent": solvent,
            "relativistic_method": relativistic_method,
        },
        "task_requests": normalized_tasks,
        # ``source_protocol`` is provenance, not protocol identity. Retaining
        # it in the JSON response is useful, but including original casing in
        # the hash would recreate one protocol per spelling.
        "normalized_spec": {
            key: value
            for key, value in normalized_spec.items()
            if key != "source_protocol"
        },
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
        functional=normalized_functional,
        basis_set=normalized_basis_set,
        auxiliary_basis_set=normalized_auxiliary_basis_set,
        dispersion_model=normalized_dispersion_model,
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
    "normalize_functional_and_dispersion",
    "normalize_protocol_text",
]
