"""Content-address raw artifacts and normalize calculation protocols."""

import json
import re
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from tricycle_reaction_db.application.dtos.artifacts import (
    ArtifactFileRecord,
    CalculationProtocolRecord,
)
from tricycle_reaction_db.core.chemistry_config import CALCULATION_PROTOCOL_VERSION
from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    ArtifactVisibility,
    QMSoftware,
    StorageStatus,
)
from tricycle_reaction_db.ingestion.media_type import detect_artifact_media_type

# MolOP and quantum-chemistry programs use both ``D3BJ`` and ``GD3BJ`` for
# the Grimme D3(BJ) correction. Keep one spelling in the database so the
# protocol hash does not depend on the program-specific alias.
_DISPERSION_MODEL_ALIASES: dict[str, str] = {
    "D3": "GD3",
    "GD3": "GD3",
    "D3BJ": "GD3BJ",
    "GD3BJ": "GD3BJ",
    "D3BJM": "GD3BJM",
    "GD3BJM": "GD3BJM",
    "D3BJABC": "GD3BJABC",
    "GD3BJABC": "GD3BJABC",
    "D3ZERO": "GD3ZERO",
    "GD3ZERO": "GD3ZERO",
    "D3ZEROM": "GD3ZEROM",
    "GD3ZEROM": "GD3ZEROM",
    "D4": "D4",
    "NL": "NL",
    "VV10": "VV10",
}
_DISPERSION_SUFFIX_KEYS = tuple(sorted(_DISPERSION_MODEL_ALIASES, key=len, reverse=True))


def _dispersion_lookup_key(value: str) -> str:
    return re.sub(r"[-_()\[\]\s]", "", value).upper()


def _normalized_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"\s+", "", value).strip()
    return normalized or None


def _canonical_dispersion_model(value: str | None) -> str | None:
    normalized = _normalized_text(value)
    if normalized is None:
        return None
    return _DISPERSION_MODEL_ALIASES.get(
        _dispersion_lookup_key(normalized),
        normalized.upper(),
    )


def _canonical_functional_text(value: str) -> str:
    normalized = _normalized_text(value)
    if normalized is None:
        return ""
    # Match MolOP's spelling convention: all-lowercase parser tokens become
    # uppercase, while established mixed-case names such as wB97M-V remain
    # readable and stable.
    return normalized.upper() if normalized.islower() else normalized


def _split_functional_dispersion(functional: str) -> tuple[str, str] | None:
    """Return a functional base and canonical suffix when one is explicit."""

    normalized = _normalized_text(functional)
    if normalized is None:
        return None
    # Parentheses are common in spellings such as ``D3(BJ)``.  They are
    # punctuation around the suffix, not part of the canonical functional.
    compact = normalized.replace("(", "").replace(")", "")
    upper = compact.upper()
    for suffix_key in _DISPERSION_SUFFIX_KEYS:
        if not upper.endswith(suffix_key):
            continue
        base = compact[: -len(suffix_key)].rstrip("-_/ ")
        if not base:
            continue
        return _canonical_functional_text(base), _DISPERSION_MODEL_ALIASES[suffix_key]
    return None


def normalize_functional_and_dispersion(
    functional: str | None,
    dispersion_model: str | None,
) -> tuple[str | None, str | None]:
    """Normalize a functional together with its independent dispersion field.

    The database functional is the display/query-ready combined name.  When a
    dispersion model is present and the functional does not carry a known
    dispersion suffix, the suffix is added.  An explicit suffix is compared
    against the model using program-independent aliases; a conflict is an
    ingestion error rather than something that can be repaired safely.
    """

    canonical_model = _canonical_dispersion_model(dispersion_model)
    normalized_functional = (
        _canonical_functional_text(functional) if functional is not None else None
    )
    if normalized_functional is None:
        return None, canonical_model

    explicit = _split_functional_dispersion(normalized_functional)
    if explicit is not None:
        base, explicit_model = explicit
        if canonical_model is not None and explicit_model != canonical_model:
            raise ValueError(
                "functional dispersion suffix conflicts with dispersion_model: "
                f"functional={functional!r} ({explicit_model}), "
                f"dispersion_model={dispersion_model!r} ({canonical_model})"
            )
        return f"{base}-{explicit_model}", canonical_model

    if canonical_model is not None:
        return f"{normalized_functional}-{canonical_model}", canonical_model
    return normalized_functional, None


def _normalized_protocol_spec(
    normalized_spec: dict[str, Any],
    *,
    functional: str | None,
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
    normalized_spec = _normalized_protocol_spec(
        normalized_spec,
        functional=normalized_functional,
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
            "basis_set": basis_set,
            "auxiliary_basis_set": auxiliary_basis_set,
            "dispersion_model": normalized_dispersion_model,
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
        functional=normalized_functional,
        basis_set=basis_set,
        auxiliary_basis_set=auxiliary_basis_set,
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
]
