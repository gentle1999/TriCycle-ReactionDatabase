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


def persist_artifact_file(session: Session, record: ArtifactFileRecord) -> ArtifactFile:
    artifact = session.exec(
        select(ArtifactFile).where(ArtifactFile.content_sha256 == record.content_sha256)
    ).first()
    if artifact is None:
        artifact = ArtifactFile(**record.model_dump())
        session.add(artifact)
        session.flush()
    elif artifact.size_bytes != record.size_bytes:
        raise ValueError("artifact SHA-256 resolved to a different byte size")
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
