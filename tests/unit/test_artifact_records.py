from hashlib import sha256

import pytest

from tricycle_reaction_db.application.dtos import ArtifactFileRecord
from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    ArtifactVisibility,
    QMSoftware,
    StorageStatus,
)
from tricycle_reaction_db.ingestion import (
    artifact_record_from_path,
    calculation_protocol_record,
    normalize_functional_and_dispersion,
)


def test_artifact_record_uses_raw_content_address(tmp_path) -> None:
    source = tmp_path / "sample.log"
    payload = b"Gaussian fixture\n"
    source.write_bytes(payload)

    record = artifact_record_from_path(
        source,
        bucket="tricycle-raw",
        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
    )

    expected_hash = sha256(payload).hexdigest()
    assert record.content_sha256 == expected_hash
    assert record.object_key == f"raw/sha256/{expected_hash[:2]}/{expected_hash}"
    assert record.size_bytes == len(payload)
    assert record.media_type == "text/plain"
    assert record.storage_status is StorageStatus.PENDING
    assert record.visibility is ArtifactVisibility.PUBLIC


def test_artifact_record_uses_content_for_mime_type(tmp_path) -> None:
    source = tmp_path / "payload.log"
    payload = b"\x89PNG\r\n\x1a\nnot actually a log"
    source.write_bytes(payload)

    record = artifact_record_from_path(
        source,
        bucket="tricycle-raw",
        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
    )

    assert record.media_type == "image/png"


def test_artifact_record_detects_text_without_text_suffix(tmp_path) -> None:
    source = tmp_path / "payload"
    source.write_text("plain UTF-8 output\n", encoding="utf-8")

    record = artifact_record_from_path(
        source,
        bucket="tricycle-raw",
        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
    )

    assert record.media_type == "text/plain"


def test_new_artifact_record_defaults_to_project_visibility() -> None:
    record = ArtifactFileRecord(
        bucket="tricycle-raw",
        object_key="raw/new-input.xyz",
        content_sha256="a" * 64,
        size_bytes=1,
        original_filename="new-input.xyz",
        media_type="chemical/x-xyz",
        artifact_kind=ArtifactKind.INPUT,
        storage_status=StorageStatus.PENDING,
    )

    assert record.visibility is ArtifactVisibility.PROJECT


def test_protocol_hash_is_independent_of_task_order_and_duplicates() -> None:
    common = {
        "qm_software": QMSoftware.GAUSSIAN,
        "qm_software_version": "G16RevA.03",
        "method_family": "DFT",
        "functional": "B3LYP",
        "basis_set": "def2SVP",
        "normalized_spec": {"route": "opt freq"},
    }
    first = calculation_protocol_record(task_requests=["opt", "freq", "opt"], **common)
    second = calculation_protocol_record(task_requests=["freq", "opt"], **common)

    assert first.protocol_hash == second.protocol_hash
    assert first.task_requests == second.task_requests == ["freq", "opt"]


def test_functional_gets_the_canonical_dispersion_suffix() -> None:
    functional, dispersion_model = normalize_functional_and_dispersion("B3LYP", "gd3bj")

    assert functional == "B3LYP-GD3BJ"
    assert dispersion_model == "GD3BJ"


def test_functional_dispersion_aliases_are_canonicalized() -> None:
    functional, dispersion_model = normalize_functional_and_dispersion(
        "B3LYP-D3(BJ)",
        "GD3BJ",
    )

    assert functional == "B3LYP-GD3BJ"
    assert dispersion_model == "GD3BJ"


def test_functional_dispersion_conflict_is_rejected() -> None:
    with pytest.raises(ValueError, match="conflicts with dispersion_model"):
        normalize_functional_and_dispersion("B3LYP-D4", "GD3BJ")


def test_protocol_record_keeps_source_protocol_when_normalizing() -> None:
    record = calculation_protocol_record(
        qm_software=QMSoftware.GAUSSIAN,
        qm_software_version="G16RevC.01",
        method_family="DFT",
        method="B3LYP",
        functional="B3LYP",
        basis_set="def2SVP",
        dispersion_model="GD3BJ",
        task_requests=["freq"],
        normalized_spec={
            "protocol": {
                "functional": "B3LYP",
                "dispersion_correction": "GD3BJ",
                "raw_keywords": "#P Geom=AllCheck Freq",
            }
        },
    )

    assert record.functional == "B3LYP-GD3BJ"
    assert record.dispersion_model == "GD3BJ"
    assert record.normalized_spec["protocol"]["functional"] == "B3LYP-GD3BJ"
    assert record.normalized_spec["source_protocol"]["functional"] == "B3LYP"
    assert record.normalized_spec["source_protocol"]["raw_keywords"] == ("#P Geom=AllCheck Freq")
