"""Project-scoped destructive cleanup for uploaded and generated data.

The project remains available after cleanup. This service removes raw artifacts,
generated dataset exports, durable upload queue rows, parser materialization,
reaction mappings, project-owned chemistry identities, and project directory
entries. Reusable RustFS objects are deleted only after PostgreSQL no longer
references them and only when no other live artifact uses the same object key.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import delete, func, or_, text, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from tricycle_reaction_db.application.dtos import (
    ProjectDataRemovalPreview,
    ProjectDataRemovalResult,
)
from tricycle_reaction_db.application.services.authorization import (
    AuthorizationService,
    ProjectPermission,
)
from tricycle_reaction_db.application.services.database_statistics import (
    refresh_project_statistics,
)
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    ArtifactIngestion,
    CalculationFrame,
    CalculationProtocol,
    CalculationSegment,
    Geometry,
    LogicalParticipantConcreteTopology,
    LogicalReaction,
    LogicalReactionParticipant,
    ManifestArtifactBinding,
    MappedReaction,
    MappedReactionEdge,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionNodeGeometryMapping,
    MappedReactionParticipant,
    MappedReactionThermodynamicProfile,
    MappedReactionThermodynamicProfileSource,
    MolecularFormula,
    MolecularTopology,
    MolecularTopologyAbstraction,
    MolecularTopologyDerivation,
    ParseRevision,
    Project,
    ProjectGeometryCatalog,
    ProjectGeometryCatalogCount,
    TransitionStateEndpoint,
    TransitionStateInference,
    UnitsTsDatasetExportJob,
    UploadBatch,
    UploadBatchItem,
    WorkflowManifest,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    StorageStatus,
    UploadBatchItemStatus,
)
from tricycle_reaction_db.storage.rustfs import RustFSObjectStore, RustFSSettings

logger = logging.getLogger(__name__)

_PROJECT_DATA_REMOVAL_STATEMENT_TIMEOUT = "10min"


class ProjectDataRemovalError(RuntimeError):
    """Base error for project-data cleanup."""


class ProjectDataRemovalNotFoundError(ProjectDataRemovalError):
    """The requested project does not exist."""


class ProjectDataRemovalConflictError(ProjectDataRemovalError):
    """The destructive operation cannot safely remove the requested rows."""


@dataclass(frozen=True, slots=True)
class _ObjectReference:
    bucket: str
    object_key: str
    version_id: str | None


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


async def _count(
    session: AsyncSession,
    model: Any,
    *criteria: Any,
) -> int:
    return int((await session.exec(select(func.count()).select_from(model).where(*criteria))).one())


async def _delete(
    session: AsyncSession,
    statement: Any,
) -> int:
    result = await session.exec(statement.execution_options(synchronize_session=False))
    return _rowcount(result)


async def _project(
    session: AsyncSession,
    project_id: UUID,
    *,
    lock: bool = False,
) -> Project:
    statement = select(Project).where(col(Project.id) == project_id)
    if lock:
        statement = statement.with_for_update()
    project = (await session.exec(statement)).one_or_none()
    if project is None or project.id is None:
        raise ProjectDataRemovalNotFoundError("project not found")
    return project


async def _artifact_object_references(
    session: AsyncSession,
    artifact_ids: Any,
) -> tuple[_ObjectReference, ...]:
    rows = (
        await session.exec(
            select(
                col(ArtifactFile.bucket),
                col(ArtifactFile.object_key),
                col(ArtifactFile.version_id),
            ).where(col(ArtifactFile.id).in_(artifact_ids))
        )
    ).all()
    unique = {
        _ObjectReference(bucket=bucket, object_key=object_key, version_id=version_id)
        for bucket, object_key, version_id in rows
    }
    return tuple(
        sorted(unique, key=lambda item: (item.bucket, item.object_key, item.version_id or ""))
    )


async def _dataset_export_object_references(
    session: AsyncSession,
    project_id: UUID,
) -> tuple[_ObjectReference, ...]:
    rows = (
        await session.exec(
            select(
                col(UnitsTsDatasetExportJob.bucket),
                col(UnitsTsDatasetExportJob.object_key),
            ).where(
                col(UnitsTsDatasetExportJob.project_id) == project_id,
                col(UnitsTsDatasetExportJob.bucket).is_not(None),
                col(UnitsTsDatasetExportJob.object_key).is_not(None),
            )
        )
    ).all()
    unique = {
        _ObjectReference(bucket=bucket, object_key=object_key, version_id=None)
        for bucket, object_key in rows
        if bucket is not None and object_key is not None
    }
    return tuple(
        sorted(unique, key=lambda item: (item.bucket, item.object_key, item.version_id or ""))
    )


async def _project_object_references(
    session: AsyncSession,
    project_id: UUID,
    artifact_ids: Any,
) -> tuple[_ObjectReference, ...]:
    unique = set(await _artifact_object_references(session, artifact_ids))
    unique.update(await _dataset_export_object_references(session, project_id))
    return tuple(
        sorted(unique, key=lambda item: (item.bucket, item.object_key, item.version_id or ""))
    )


async def _delete_project_dataset_export_jobs(
    session: AsyncSession,
    project_id: UUID,
) -> int:
    return await _delete(
        session,
        delete(UnitsTsDatasetExportJob).where(
            col(UnitsTsDatasetExportJob.project_id) == project_id
        ),
    )


async def _preview_in_session(
    session: AsyncSession,
    project: Project,
) -> ProjectDataRemovalPreview:
    project_id = project.id
    if project_id is None:
        raise ProjectDataRemovalNotFoundError("project not found")

    artifact_ids = select(col(ArtifactFile.id)).where(col(ArtifactFile.project_id) == project_id)
    batch_ids = select(col(UploadBatch.id)).where(col(UploadBatch.project_id) == project_id)
    revision_ids = select(col(ParseRevision.id)).where(
        col(ParseRevision.artifact_file_id).in_(artifact_ids)
    )
    logical_ids = select(col(LogicalReaction.id)).where(
        col(LogicalReaction.project_id) == project_id
    )

    object_references = await _project_object_references(
        session,
        project_id,
        artifact_ids,
    )
    return ProjectDataRemovalPreview(
        project_id=project_id,
        project_slug=project.slug,
        artifact_count=await _count(
            session, ArtifactFile, col(ArtifactFile.project_id) == project_id
        ),
        upload_batch_count=await _count(
            session, UploadBatch, col(UploadBatch.project_id) == project_id
        ),
        upload_batch_item_count=await _count(
            session,
            UploadBatchItem,
            col(UploadBatchItem.batch_id).in_(batch_ids),
        ),
        ingestion_count=await _count(
            session,
            ArtifactIngestion,
            col(ArtifactIngestion.artifact_file_id).in_(artifact_ids),
        ),
        parse_revision_count=await _count(
            session,
            ParseRevision,
            col(ParseRevision.artifact_file_id).in_(artifact_ids),
        ),
        calculation_segment_count=await _count(
            session,
            CalculationSegment,
            col(CalculationSegment.parse_revision_id).in_(revision_ids),
        ),
        calculation_frame_count=await _count(
            session,
            CalculationFrame,
            col(CalculationFrame.parse_revision_id).in_(revision_ids),
        ),
        logical_reaction_count=await _count(
            session,
            LogicalReaction,
            col(LogicalReaction.project_id) == project_id,
        ),
        mapped_reaction_count=await _count(
            session,
            MappedReaction,
            or_(
                col(MappedReaction.project_id) == project_id,
                col(MappedReaction.logical_reaction_id).in_(logical_ids),
            ),
        ),
        geometry_count=await _count(session, Geometry, col(Geometry.project_id) == project_id),
        topology_count=await _count(
            session,
            MolecularTopology,
            col(MolecularTopology.project_id) == project_id,
        ),
        formula_count=await _count(
            session,
            MolecularFormula,
            col(MolecularFormula.project_id) == project_id,
        ),
        protocol_count=await _count(
            session,
            CalculationProtocol,
            col(CalculationProtocol.project_id) == project_id,
        ),
        topology_derivation_count=await _count(
            session,
            MolecularTopologyDerivation,
            col(MolecularTopologyDerivation.project_id) == project_id,
        ),
        manifest_count=await _count(
            session,
            WorkflowManifest,
            col(WorkflowManifest.artifact_file_id).in_(artifact_ids),
        ),
        geometry_catalog_entry_count=await _count(
            session,
            ProjectGeometryCatalog,
            col(ProjectGeometryCatalog.project_id) == project_id,
        ),
        geometry_catalog_count_row_count=await _count(
            session,
            ProjectGeometryCatalogCount,
            col(ProjectGeometryCatalogCount.project_id) == project_id,
        ),
        units_ts_dataset_export_job_count=await _count(
            session,
            UnitsTsDatasetExportJob,
            col(UnitsTsDatasetExportJob.project_id) == project_id,
        ),
        rustfs_object_count=len(object_references),
        processing_item_count=await _count(
            session,
            UploadBatchItem,
            col(UploadBatchItem.batch_id).in_(batch_ids),
            col(UploadBatchItem.status) == UploadBatchItemStatus.PROCESSING,
        ),
        pending_ingestion_count=await _count(
            session,
            ArtifactIngestion,
            col(ArtifactIngestion.artifact_file_id).in_(artifact_ids),
            col(ArtifactIngestion.status) == ArtifactIngestionStatus.PENDING,
        ),
    )


async def _external_reference_count(
    session: AsyncSession,
    project_id: UUID,
    *,
    artifact_ids: Any,
    revision_ids: Any,
    frame_ids: Any,
    logical_ids: Any,
    mapped_ids: Any,
    geometry_ids: Any,
    topology_ids: Any,
    derivation_ids: Any,
    protocol_ids: Any,
    manifest_ids: Any,
) -> tuple[str, int] | None:
    """Find a reference that must not be removed with this project.

    Project-owned chemistry rows are normally isolated by the ingestion
    boundary.  Refuse a destructive operation if legacy/manual data crosses
    that boundary instead of silently breaking another project's queries.
    """

    checks: tuple[tuple[str, Any], ...] = (
        (
            "artifact referenced by an external manifest",
            select(func.count())
            .select_from(ManifestArtifactBinding)
            .join(
                WorkflowManifest,
                col(WorkflowManifest.id) == col(ManifestArtifactBinding.workflow_manifest_id),
            )
            .join(ArtifactFile, col(ArtifactFile.id) == col(WorkflowManifest.artifact_file_id))
            .where(
                col(ManifestArtifactBinding.artifact_file_id).in_(artifact_ids),
                col(ArtifactFile.project_id) != project_id,
            ),
        ),
        (
            "artifact parse revision referenced by an external reparse",
            select(func.count())
            .select_from(ParseRevision)
            .where(
                col(ParseRevision.reparse_of_id).in_(revision_ids),
                ~col(ParseRevision.artifact_file_id).in_(artifact_ids),
            ),
        ),
        (
            "logical reaction referenced by an external mapped reaction",
            select(func.count())
            .select_from(MappedReaction)
            .where(
                col(MappedReaction.logical_reaction_id).in_(logical_ids),
                or_(
                    col(MappedReaction.project_id).is_(None),
                    col(MappedReaction.project_id) != project_id,
                ),
            ),
        ),
        (
            "logical reaction referenced by an external inference",
            select(func.count())
            .select_from(TransitionStateInference)
            .join(
                ArtifactIngestion,
                col(ArtifactIngestion.id) == col(TransitionStateInference.artifact_ingestion_id),
            )
            .join(ArtifactFile, col(ArtifactFile.id) == col(ArtifactIngestion.artifact_file_id))
            .where(
                col(TransitionStateInference.logical_reaction_id).in_(logical_ids),
                col(ArtifactFile.project_id) != project_id,
            ),
        ),
        (
            "mapped reaction referenced by an external inference",
            select(func.count())
            .select_from(TransitionStateInference)
            .join(
                ArtifactIngestion,
                col(ArtifactIngestion.id) == col(TransitionStateInference.artifact_ingestion_id),
            )
            .join(ArtifactFile, col(ArtifactFile.id) == col(ArtifactIngestion.artifact_file_id))
            .where(
                col(TransitionStateInference.mapped_reaction_id).in_(mapped_ids),
                col(ArtifactFile.project_id) != project_id,
            ),
        ),
        (
            "calculation frame referenced by an external inference",
            select(func.count())
            .select_from(TransitionStateInference)
            .join(
                ArtifactIngestion,
                col(ArtifactIngestion.id) == col(TransitionStateInference.artifact_ingestion_id),
            )
            .join(ArtifactFile, col(ArtifactFile.id) == col(ArtifactIngestion.artifact_file_id))
            .where(
                col(TransitionStateInference.calculation_frame_id).in_(frame_ids),
                col(ArtifactFile.project_id) != project_id,
            ),
        ),
        (
            "calculation frame referenced by an external profile",
            select(func.count())
            .select_from(MappedReactionThermodynamicProfileSource)
            .join(
                MappedReactionThermodynamicProfile,
                col(MappedReactionThermodynamicProfile.id)
                == col(MappedReactionThermodynamicProfileSource.profile_id),
            )
            .join(
                MappedReaction,
                col(MappedReaction.id)
                == col(MappedReactionThermodynamicProfile.mapped_reaction_id),
            )
            .where(
                col(MappedReactionThermodynamicProfileSource.calculation_frame_id).in_(frame_ids),
                or_(
                    col(MappedReaction.project_id).is_(None),
                    col(MappedReaction.project_id) != project_id,
                ),
            ),
        ),
        (
            "geometry referenced by an external frame",
            select(func.count())
            .select_from(CalculationFrame)
            .join(
                ParseRevision,
                col(ParseRevision.id) == col(CalculationFrame.parse_revision_id),
            )
            .join(ArtifactFile, col(ArtifactFile.id) == col(ParseRevision.artifact_file_id))
            .where(
                col(CalculationFrame.geometry_id).in_(geometry_ids),
                col(ArtifactFile.project_id) != project_id,
            ),
        ),
        (
            "geometry referenced by an external mapped reaction",
            select(func.count())
            .select_from(MappedReactionNodeGeometry)
            .join(
                MappedReactionNode,
                col(MappedReactionNode.id)
                == col(MappedReactionNodeGeometry.mapped_reaction_node_id),
            )
            .join(
                MappedReaction,
                col(MappedReaction.id) == col(MappedReactionNode.mapped_reaction_id),
            )
            .where(
                col(MappedReactionNodeGeometry.geometry_id).in_(geometry_ids),
                or_(
                    col(MappedReaction.project_id).is_(None),
                    col(MappedReaction.project_id) != project_id,
                ),
            ),
        ),
        (
            "topology derivation referenced by an external frame",
            select(func.count())
            .select_from(CalculationFrame)
            .join(
                ParseRevision,
                col(ParseRevision.id) == col(CalculationFrame.parse_revision_id),
            )
            .join(ArtifactFile, col(ArtifactFile.id) == col(ParseRevision.artifact_file_id))
            .where(
                col(CalculationFrame.topology_derivation_id).in_(derivation_ids),
                col(ArtifactFile.project_id) != project_id,
            ),
        ),
        (
            "protocol referenced by an external calculation segment",
            select(func.count())
            .select_from(CalculationSegment)
            .join(ParseRevision, col(ParseRevision.id) == col(CalculationSegment.parse_revision_id))
            .join(ArtifactFile, col(ArtifactFile.id) == col(ParseRevision.artifact_file_id))
            .where(
                col(CalculationSegment.protocol_id).in_(protocol_ids),
                col(ArtifactFile.project_id) != project_id,
            ),
        ),
        (
            "formula referenced by an external topology",
            select(func.count())
            .select_from(MolecularTopology)
            .where(
                col(MolecularTopology.formula_id).in_(
                    select(col(MolecularFormula.id)).where(
                        col(MolecularFormula.project_id) == project_id
                    )
                ),
                or_(
                    col(MolecularTopology.project_id).is_(None),
                    col(MolecularTopology.project_id) != project_id,
                ),
            ),
        ),
        (
            "topology referenced by an external geometry",
            select(func.count())
            .select_from(Geometry)
            .where(
                col(Geometry.topology_id).in_(topology_ids),
                or_(col(Geometry.project_id).is_(None), col(Geometry.project_id) != project_id),
            ),
        ),
        (
            "topology referenced by an external reaction participant",
            select(func.count())
            .select_from(LogicalReactionParticipant)
            .join(
                LogicalReaction,
                col(LogicalReaction.id) == col(LogicalReactionParticipant.logical_reaction_id),
            )
            .where(
                col(LogicalReactionParticipant.topology_id).in_(topology_ids),
                or_(
                    col(LogicalReaction.project_id).is_(None),
                    col(LogicalReaction.project_id) != project_id,
                ),
            ),
        ),
        (
            "topology referenced by an external concrete membership",
            select(func.count())
            .select_from(LogicalParticipantConcreteTopology)
            .join(
                LogicalReactionParticipant,
                col(LogicalReactionParticipant.id)
                == col(LogicalParticipantConcreteTopology.logical_reaction_participant_id),
            )
            .join(
                LogicalReaction,
                col(LogicalReaction.id) == col(LogicalReactionParticipant.logical_reaction_id),
            )
            .where(
                col(LogicalParticipantConcreteTopology.concrete_topology_id).in_(topology_ids),
                or_(
                    col(LogicalReaction.project_id).is_(None),
                    col(LogicalReaction.project_id) != project_id,
                ),
            ),
        ),
        (
            "topology referenced by an external mapped participant",
            select(func.count())
            .select_from(MappedReactionParticipant)
            .join(
                MappedReaction,
                col(MappedReaction.id) == col(MappedReactionParticipant.mapped_reaction_id),
            )
            .where(
                col(MappedReactionParticipant.concrete_topology_id).in_(topology_ids),
                or_(
                    col(MappedReaction.project_id).is_(None),
                    col(MappedReaction.project_id) != project_id,
                ),
            ),
        ),
        (
            "topology referenced by an external derivation",
            select(func.count())
            .select_from(MolecularTopologyDerivation)
            .where(
                col(MolecularTopologyDerivation.topology_id).in_(topology_ids),
                or_(
                    col(MolecularTopologyDerivation.project_id).is_(None),
                    col(MolecularTopologyDerivation.project_id) != project_id,
                ),
            ),
        ),
        (
            "topology referenced by an external transition-state endpoint",
            select(func.count())
            .select_from(TransitionStateEndpoint)
            .join(
                CalculationFrame,
                col(CalculationFrame.id) == col(TransitionStateEndpoint.calculation_frame_id),
            )
            .join(
                ParseRevision,
                col(ParseRevision.id) == col(CalculationFrame.parse_revision_id),
            )
            .join(ArtifactFile, col(ArtifactFile.id) == col(ParseRevision.artifact_file_id))
            .where(
                col(TransitionStateEndpoint.topology_id).in_(topology_ids),
                col(ArtifactFile.project_id) != project_id,
            ),
        ),
        (
            "topology abstraction referenced by another project",
            select(func.count())
            .select_from(MolecularTopologyAbstraction)
            .where(
                or_(
                    col(MolecularTopologyAbstraction.specific_topology_id).in_(topology_ids),
                    col(MolecularTopologyAbstraction.general_topology_id).in_(topology_ids),
                ),
                or_(
                    col(MolecularTopologyAbstraction.project_id).is_(None),
                    col(MolecularTopologyAbstraction.project_id) != project_id,
                ),
            ),
        ),
        (
            "manifest revision superseded by another project",
            select(func.count())
            .select_from(WorkflowManifest)
            .join(
                ArtifactFile,
                col(ArtifactFile.id) == col(WorkflowManifest.artifact_file_id),
            )
            .where(
                col(WorkflowManifest.supersedes_id).in_(manifest_ids),
                col(ArtifactFile.project_id) != project_id,
            ),
        ),
    )
    for label, statement in checks:
        count = int((await session.exec(statement)).one())
        if count:
            return label, count
    return None


async def _delete_project_rows(
    session: AsyncSession,
    project: Project,
) -> ProjectDataRemovalPreview:
    project_id = project.id
    if project_id is None:
        raise ProjectDataRemovalNotFoundError("project not found")

    preview = await _preview_in_session(session, project)
    artifact_ids = select(col(ArtifactFile.id)).where(col(ArtifactFile.project_id) == project_id)
    revision_ids = select(col(ParseRevision.id)).where(
        col(ParseRevision.artifact_file_id).in_(artifact_ids)
    )
    frame_ids = select(col(CalculationFrame.id)).where(
        col(CalculationFrame.parse_revision_id).in_(revision_ids)
    )
    logical_ids = select(col(LogicalReaction.id)).where(
        col(LogicalReaction.project_id) == project_id
    )
    mapped_ids = select(col(MappedReaction.id)).where(
        or_(
            col(MappedReaction.project_id) == project_id,
            col(MappedReaction.logical_reaction_id).in_(logical_ids),
        )
    )
    geometry_ids = select(col(Geometry.id)).where(col(Geometry.project_id) == project_id)
    topology_ids = select(col(MolecularTopology.id)).where(
        col(MolecularTopology.project_id) == project_id
    )
    derivation_ids = select(col(MolecularTopologyDerivation.id)).where(
        col(MolecularTopologyDerivation.project_id) == project_id
    )
    protocol_ids = select(col(CalculationProtocol.id)).where(
        col(CalculationProtocol.project_id) == project_id
    )
    manifest_ids = select(col(WorkflowManifest.id)).where(
        col(WorkflowManifest.artifact_file_id).in_(artifact_ids)
    )

    external_reference = await _external_reference_count(
        session,
        project_id,
        artifact_ids=artifact_ids,
        revision_ids=revision_ids,
        frame_ids=frame_ids,
        logical_ids=logical_ids,
        mapped_ids=mapped_ids,
        geometry_ids=geometry_ids,
        topology_ids=topology_ids,
        derivation_ids=derivation_ids,
        protocol_ids=protocol_ids,
        manifest_ids=manifest_ids,
    )
    if external_reference is not None:
        label, count = external_reference
        raise ProjectDataRemovalConflictError(f"{label}: {count}")

    await _delete_project_dataset_export_jobs(session, project_id)

    ingestion_ids = select(col(ArtifactIngestion.id)).where(
        col(ArtifactIngestion.artifact_file_id).in_(artifact_ids)
    )
    node_ids = select(col(MappedReactionNode.id)).where(
        col(MappedReactionNode.mapped_reaction_id).in_(mapped_ids)
    )
    node_geometry_ids = select(col(MappedReactionNodeGeometry.id)).where(
        col(MappedReactionNodeGeometry.mapped_reaction_node_id).in_(node_ids)
    )
    logical_participant_ids = select(col(LogicalReactionParticipant.id)).where(
        col(LogicalReactionParticipant.logical_reaction_id).in_(logical_ids)
    )
    profile_ids = select(col(MappedReactionThermodynamicProfile.id)).where(
        col(MappedReactionThermodynamicProfile.mapped_reaction_id).in_(mapped_ids)
    )

    # Queue and parser rows are removed first.  This handles restrictive
    # inference/frame/revision foreign keys before the project identities are
    # removed below.
    await _delete(
        session,
        delete(TransitionStateInference).where(
            or_(
                col(TransitionStateInference.artifact_ingestion_id).in_(ingestion_ids),
                col(TransitionStateInference.parse_revision_id).in_(revision_ids),
                col(TransitionStateInference.calculation_frame_id).in_(frame_ids),
            )
        ),
    )
    await session.exec(
        update(ParseRevision)
        .where(
            or_(
                col(ParseRevision.artifact_file_id).in_(artifact_ids),
                col(ParseRevision.reparse_of_id).in_(revision_ids),
            )
        )
        .values(reparse_of_id=None)
    )
    await _delete(
        session,
        delete(ParseRevision).where(col(ParseRevision.artifact_file_id).in_(artifact_ids)),
    )

    # Remove manifest bindings and revision-chain edges before their artifact
    # files.  All of these rows are project-owned through their source file.
    await session.exec(
        update(ManifestArtifactBinding)
        .where(col(ManifestArtifactBinding.workflow_manifest_id).in_(manifest_ids))
        .values(source_geometry_artifact_key=None)
    )
    await session.exec(
        update(WorkflowManifest)
        .where(col(WorkflowManifest.id).in_(manifest_ids))
        .values(supersedes_id=None)
    )
    await _delete(
        session,
        delete(ManifestArtifactBinding).where(
            col(ManifestArtifactBinding.workflow_manifest_id).in_(manifest_ids)
        ),
    )
    await _delete(
        session,
        delete(WorkflowManifest).where(col(WorkflowManifest.id).in_(manifest_ids)),
    )

    # Remove mapped reaction children explicitly because mapped edges have
    # restrictive same-path node references.
    await _delete(
        session,
        delete(MappedReactionNodeGeometryMapping).where(
            col(MappedReactionNodeGeometryMapping.mapped_reaction_node_geometry_id).in_(
                node_geometry_ids
            )
        ),
    )
    await _delete(
        session,
        delete(MappedReactionNodeGeometry).where(
            col(MappedReactionNodeGeometry.mapped_reaction_node_id).in_(node_ids)
        ),
    )
    await _delete(
        session,
        delete(MappedReactionEdge).where(
            col(MappedReactionEdge.mapped_reaction_id).in_(mapped_ids)
        ),
    )
    await _delete(
        session,
        delete(MappedReactionParticipant).where(
            col(MappedReactionParticipant.mapped_reaction_id).in_(mapped_ids)
        ),
    )
    await _delete(
        session,
        delete(MappedReactionThermodynamicProfile).where(
            col(MappedReactionThermodynamicProfile.id).in_(profile_ids)
        ),
    )
    await _delete(
        session,
        delete(MappedReactionNode).where(col(MappedReactionNode.id).in_(node_ids)),
    )
    await _delete(
        session,
        delete(MappedReaction).where(col(MappedReaction.id).in_(mapped_ids)),
    )
    await _delete(
        session,
        delete(LogicalParticipantConcreteTopology).where(
            col(LogicalParticipantConcreteTopology.logical_reaction_participant_id).in_(
                logical_participant_ids
            )
        ),
    )
    await _delete(
        session,
        delete(LogicalReactionParticipant).where(
            col(LogicalReactionParticipant.id).in_(logical_participant_ids)
        ),
    )
    await _delete(
        session,
        delete(LogicalReaction).where(col(LogicalReaction.project_id) == project_id),
    )

    # The parser cascade has removed frames/results.  Project directories and
    # chemistry identities can now be deleted without touching another project.
    await _delete(
        session,
        delete(ProjectGeometryCatalog).where(col(ProjectGeometryCatalog.project_id) == project_id),
    )
    await _delete(
        session,
        delete(ProjectGeometryCatalogCount).where(
            col(ProjectGeometryCatalogCount.project_id) == project_id
        ),
    )
    await _delete(
        session,
        delete(Geometry).where(col(Geometry.project_id) == project_id),
    )
    await _delete(
        session,
        delete(MolecularTopologyAbstraction).where(
            col(MolecularTopologyAbstraction.project_id) == project_id
        ),
    )
    await _delete(
        session,
        delete(MolecularTopologyDerivation).where(
            col(MolecularTopologyDerivation.project_id) == project_id
        ),
    )
    await _delete(
        session,
        delete(MolecularTopology).where(col(MolecularTopology.project_id) == project_id),
    )
    await _delete(
        session,
        delete(MolecularFormula).where(col(MolecularFormula.project_id) == project_id),
    )
    await _delete(
        session,
        delete(CalculationProtocol).where(col(CalculationProtocol.project_id) == project_id),
    )

    await _delete(
        session,
        delete(ArtifactFile).where(col(ArtifactFile.project_id) == project_id),
    )
    await _delete(
        session,
        delete(UploadBatch).where(col(UploadBatch.project_id) == project_id),
    )
    return preview


def _delete_objects(
    settings: RustFSSettings,
    references: tuple[_ObjectReference, ...],
) -> tuple[int, int, str | None]:
    if not references:
        return 0, 0, None
    grouped: dict[str, list[tuple[str, str | None]]] = {}
    for reference in references:
        grouped.setdefault(reference.bucket, []).append(
            (reference.object_key, reference.version_id)
        )

    deleted = 0
    pending = 0
    first_error: str | None = None
    for bucket, objects in grouped.items():
        try:
            bucket_settings = settings.model_copy(update={"bucket": bucket})
            with RustFSObjectStore(bucket_settings) as store:
                bucket_deleted, bucket_failed = store.delete_many(objects)
            deleted += bucket_deleted
            pending += bucket_failed
            if bucket_failed and first_error is None:
                first_error = f"RustFS multi-delete failed for {bucket_failed} object(s)"
        except (BotoCoreError, ClientError, RuntimeError) as error:
            pending += len(objects)
            if first_error is None:
                first_error = str(error)
            logger.exception("failed to remove project RustFS objects bucket=%s", bucket)
    return deleted, pending, first_error


class ProjectDataRemovalService:
    """Preview and physically remove all data owned by one project."""

    @staticmethod
    async def preview(
        project_id: UUID,
        *,
        user_id: UUID,
    ) -> ProjectDataRemovalPreview:
        await AuthorizationService.require_project_permission(
            user_id,
            project_id,
            ProjectPermission.PROJECT_MANAGE,
        )
        async with session_factory() as session:
            project = await _project(session, project_id)
            return await _preview_in_session(session, project)

    @staticmethod
    async def clear(
        project_id: UUID,
        *,
        user_id: UUID,
        confirmation: str,
    ) -> ProjectDataRemovalResult:
        await AuthorizationService.require_project_permission(
            user_id,
            project_id,
            ProjectPermission.PROJECT_MANAGE,
        )
        async with session_factory() as session:
            # The normal 15-second query budget is appropriate for request
            # traffic, but not for one explicitly confirmed, set-based purge
            # of a large project's parser materialization.  Keep the larger
            # limit local to this transaction so normal API queries retain
            # their usual protection.
            await session.execute(
                text(f"SET LOCAL statement_timeout = '{_PROJECT_DATA_REMOVAL_STATEMENT_TIMEOUT}'")
            )
            project = await _project(session, project_id, lock=True)
            if confirmation != project.slug:
                raise ProjectDataRemovalConflictError(
                    "confirmation must exactly match the project slug"
                )
            references = await _project_object_references(
                session,
                project_id,
                select(col(ArtifactFile.id)).where(col(ArtifactFile.project_id) == project_id),
            )
            try:
                # This is a row-level trigger and recomputes every affected
                # profile by walking all of its source frames.  The profiles
                # and source rows being removed here are already covered by
                # the cross-project reference check, so refreshing their
                # transient visibility state during deletion is both useless
                # and quadratic for large projects.  PostgreSQL DDL is
                # transactional; rollback restores the trigger if anything
                # fails before the explicit re-enable below.
                await session.execute(
                    text(
                        "ALTER TABLE mapped_reaction_thermodynamic_profile_source "
                        "DISABLE TRIGGER trg_thermodynamic_profile_source_change"
                    )
                )
                preview = await _delete_project_rows(session, project)
                await session.execute(
                    text(
                        "ALTER TABLE mapped_reaction_thermodynamic_profile_source "
                        "ENABLE TRIGGER trg_thermodynamic_profile_source_change"
                    )
                )
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise ProjectDataRemovalConflictError(
                    "project data has an external reference and was not removed"
                ) from error

        # The delete is committed before refreshing statistics so ANALYZE does
        # not share the long-running purge transaction or its locks.
        await refresh_project_statistics(
            (project_id,),
            reason="project-data-removal",
        )
        settings = RustFSSettings()
        active_object_keys: set[tuple[str, str]] = set()
        if references:
            async with session_factory() as session:
                remaining = (
                    await session.exec(
                        select(col(ArtifactFile.bucket), col(ArtifactFile.object_key))
                        .where(
                            col(ArtifactFile.storage_status) != StorageStatus.RETIRED,
                            col(ArtifactFile.bucket).in_({item.bucket for item in references}),
                            col(ArtifactFile.object_key).in_(
                                {item.object_key for item in references}
                            ),
                        )
                        .distinct()
                    )
                ).all()
            active_object_keys = {(bucket, key) for bucket, key in remaining}

        deletable_references = tuple(
            reference
            for reference in references
            if (reference.bucket, reference.object_key) not in active_object_keys
        )
        deleted, pending, error_message = await asyncio.to_thread(
            _delete_objects,
            settings,
            deletable_references,
        )
        return ProjectDataRemovalResult(
            **preview.model_dump(),
            rustfs_objects_deleted=deleted,
            rustfs_objects_pending=pending + len(references) - len(deletable_references),
            rustfs_cleanup_error=error_message,
        )


__all__ = [
    "ProjectDataRemovalConflictError",
    "ProjectDataRemovalError",
    "ProjectDataRemovalNotFoundError",
    "ProjectDataRemovalService",
]
