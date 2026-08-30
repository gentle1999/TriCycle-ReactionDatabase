"""Idempotent persistence and lifecycle checks for artifact records."""

from sqlmodel import Session, select

from tricycle_reaction_db.application.dtos.artifacts import (
    ArtifactFileRecord,
    CalculationProtocolRecord,
)
from tricycle_reaction_db.application.services._persistence import (
    _acquire_identity_locks,
    _flush_shared_entity,
)
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    CalculationProtocol,
)
from tricycle_reaction_db.domain.enums import StorageStatus


def persist_artifact_file(session: Session, record: ArtifactFileRecord) -> ArtifactFile:
    _acquire_identity_locks(session, ("artifact-content", record.content_sha256))
    artifact = session.exec(
        select(ArtifactFile).where(
            ArtifactFile.project_id == record.project_id,
            ArtifactFile.content_sha256 == record.content_sha256,
        )
    ).first()
    if artifact is None:
        values = record.model_dump()
        shared_available = session.exec(
            select(ArtifactFile).where(
                ArtifactFile.content_sha256 == record.content_sha256,
                ArtifactFile.bucket == record.bucket,
                ArtifactFile.storage_status == StorageStatus.AVAILABLE,
            )
        ).first()
        if shared_available is not None:
            values.update(
                bucket=shared_available.bucket,
                object_key=shared_available.object_key,
                version_id=shared_available.version_id,
                storage_status=StorageStatus.AVAILABLE,
                etag=shared_available.etag,
                storage_verified_at=shared_available.storage_verified_at,
            )
        artifact = ArtifactFile(**values)
        session.add(artifact)
        session.flush()
    elif artifact.size_bytes != record.size_bytes:
        raise ValueError("artifact SHA-256 resolved to a different byte size")
    elif artifact.artifact_kind is not record.artifact_kind:
        raise ValueError(
            "an identical artifact is already registered with a different artifact kind"
        )
    return artifact


def persist_calculation_protocol(
    session: Session,
    record: CalculationProtocolRecord,
) -> CalculationProtocol:
    _acquire_identity_locks(session, ("calculation_protocol", record.protocol_hash))
    protocol = session.exec(
        select(CalculationProtocol).where(CalculationProtocol.protocol_hash == record.protocol_hash)
    ).first()
    if protocol is None:
        protocol = CalculationProtocol(**record.model_dump())
        _flush_shared_entity(session, protocol, label="CalculationProtocol")
    return protocol


__all__ = [
    "persist_artifact_file",
    "persist_calculation_protocol",
]
