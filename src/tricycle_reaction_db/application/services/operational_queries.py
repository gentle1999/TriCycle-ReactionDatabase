"""Read-only workflow, storage, and topology provenance queries."""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from nexusx import UseCaseService, query  # type: ignore[import-untyped]
from sqlalchemy import func, literal, or_, select
from sqlmodel import col

from tricycle_reaction_db.application.dtos import (
    ManifestArtifactBindingDetail,
    ManifestArtifactBindingPage,
    ManifestArtifactBindingSummary,
    MolecularTopologyDerivationDetail,
    MolecularTopologyDerivationPage,
    MolecularTopologyDerivationSummary,
    PageInfo,
    StorageGarbageCollectionRunPage,
    StorageGarbageCollectionRunSummary,
    StorageGarbageCollectionStateDetail,
    StorageGarbageCollectionStatePage,
    StorageGarbageCollectionStateSummary,
    WorkflowManifestDetail,
    WorkflowManifestPage,
    WorkflowManifestSummary,
)
from tricycle_reaction_db.application.services.queries import (
    PageLimit,
    PageOffset,
    _enum_value,
    _required_uuid,
)
from tricycle_reaction_db.application.services.query_visibility import (
    artifact_id_is_visible,
    frame_id_is_visible,
    manifest_binding_id_is_visible,
    query_visibility_scope,
    topology_derivation_id_is_visible,
    workflow_manifest_id_is_visible,
)
from tricycle_reaction_db.db.models import (
    CalculationFrame,
    ManifestArtifactBinding,
    MolecularTopology,
    MolecularTopologyDerivation,
    StorageGarbageCollectionRun,
    StorageGarbageCollectionState,
    WorkflowManifest,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import (
    ArtifactResolutionStatus,
    ManifestArtifactRole,
    StorageGarbageCollectionRunStatus,
    WorkflowManifestStatus,
)


def _page(total: int, limit: int, offset: int) -> PageInfo:
    return PageInfo(total=total, limit=limit, offset=offset)


def _binding_summary(
    binding: ManifestArtifactBinding,
    *,
    artifact_file_id_visible: bool = True,
) -> ManifestArtifactBindingSummary:
    return ManifestArtifactBindingSummary(
        id=_required_uuid(binding.id, "ManifestArtifactBinding"),
        workflow_manifest_id=binding.workflow_manifest_id,
        artifact_key=binding.artifact_key,
        artifact_file_id=(binding.artifact_file_id if artifact_file_id_visible else None),
        expected_content_sha256=binding.expected_content_sha256,
        artifact_role=_enum_value(binding.artifact_role),
        reaction_key=binding.reaction_key,
        path_key=binding.path_key,
        node_key=binding.node_key,
        segment_index=binding.segment_index,
        frame_index=binding.frame_index,
        source_geometry_artifact_key=binding.source_geometry_artifact_key,
        resolution_status=_enum_value(binding.resolution_status),
        created_at=binding.created_at,
    )


def _manifest_summary(
    manifest: WorkflowManifest,
    artifact_binding_count: int,
) -> WorkflowManifestSummary:
    return WorkflowManifestSummary(
        id=_required_uuid(manifest.id, "WorkflowManifest"),
        artifact_file_id=manifest.artifact_file_id,
        manifest_key=manifest.manifest_key,
        revision=manifest.revision,
        schema_version=manifest.schema_version,
        payload_sha256=manifest.payload_sha256,
        qc_policy_version=manifest.qc_policy_version,
        status=_enum_value(manifest.status),
        supersedes_id=manifest.supersedes_id,
        published_at=manifest.published_at,
        created_at=manifest.created_at,
        artifact_binding_count=artifact_binding_count,
    )


def _run_summary(
    run: StorageGarbageCollectionRun,
    state: StorageGarbageCollectionState,
) -> StorageGarbageCollectionRunSummary:
    return StorageGarbageCollectionRunSummary(
        id=_required_uuid(run.id, "StorageGarbageCollectionRun"),
        state_id=run.state_id,
        bucket=state.bucket,
        root_prefix=state.root_prefix,
        started_at=run.started_at,
        completed_at=run.completed_at,
        scan_after=run.scan_after,
        scan_until=run.scan_until,
        status=_enum_value(run.status),
        objects_seen=run.objects_seen,
        objects_deleted=run.objects_deleted,
        objects_retained=run.objects_retained,
        objects_failed=run.objects_failed,
        error_message=run.error_message,
        created_at=run.created_at,
    )


class WorkflowManifestQueryService(UseCaseService):  # type: ignore[misc]
    """Inspect optional curated workflow manifests and artifact declarations."""

    @query  # type: ignore[untyped-decorator]
    async def list_workflow_manifests(
        cls,
        artifact_file_id: UUID | None = None,
        manifest_key: str | None = None,
        revision: int | None = None,
        status: WorkflowManifestStatus | None = None,
        schema_version: str | None = None,
        qc_policy_version: str | None = None,
        bound_artifact_file_id: UUID | None = None,
        limit: PageLimit = 50,
        offset: PageOffset = 0,
    ) -> WorkflowManifestPage:
        scope = await query_visibility_scope()
        predicates: list[Any] = [workflow_manifest_id_is_visible(scope, col(WorkflowManifest.id))]
        for field, value in (
            (WorkflowManifest.artifact_file_id, artifact_file_id),
            (WorkflowManifest.manifest_key, manifest_key),
            (WorkflowManifest.revision, revision),
            (WorkflowManifest.status, status),
            (WorkflowManifest.schema_version, schema_version),
            (WorkflowManifest.qc_policy_version, qc_policy_version),
        ):
            if value is not None:
                predicates.append(col(field) == value)
        if artifact_file_id is not None:
            predicates.append(artifact_id_is_visible(scope, literal(artifact_file_id)))
        if bound_artifact_file_id is not None:
            manifest_ids = select(col(ManifestArtifactBinding.workflow_manifest_id)).where(
                col(ManifestArtifactBinding.artifact_file_id) == bound_artifact_file_id
            )
            predicates.append(col(WorkflowManifest.id).in_(manifest_ids))
            predicates.append(artifact_id_is_visible(scope, literal(bound_artifact_file_id)))
        binding_count = (
            select(func.count())
            .select_from(ManifestArtifactBinding)
            .where(col(ManifestArtifactBinding.workflow_manifest_id) == col(WorkflowManifest.id))
            .scalar_subquery()
        )
        count_statement = select(func.count()).select_from(WorkflowManifest).where(*predicates)
        statement = (
            select(WorkflowManifest, binding_count.label("artifact_binding_count"))
            .where(*predicates)
            .order_by(col(WorkflowManifest.manifest_key), col(WorkflowManifest.revision))
            .offset(offset)
            .limit(limit)
        )
        async with session_factory() as session:
            total = int((await session.execute(count_statement)).scalar_one())
            rows = (await session.execute(statement)).all()
        return WorkflowManifestPage(
            items=[_manifest_summary(manifest, int(count)) for manifest, count in rows],
            page=_page(total, limit, offset),
        )

    @query  # type: ignore[untyped-decorator]
    async def get_workflow_manifest(
        cls,
        workflow_manifest_id: UUID,
    ) -> WorkflowManifestDetail | None:
        scope = await query_visibility_scope()
        async with session_factory() as session:
            manifest = (
                await session.execute(
                    select(WorkflowManifest).where(
                        col(WorkflowManifest.id) == workflow_manifest_id,
                        workflow_manifest_id_is_visible(scope, col(WorkflowManifest.id)),
                    )
                )
            ).scalar_one_or_none()
            if manifest is None:
                return None
            revisions = (
                (
                    await session.execute(
                        select(WorkflowManifest)
                        .where(
                            col(WorkflowManifest.manifest_key) == manifest.manifest_key,
                            workflow_manifest_id_is_visible(scope, col(WorkflowManifest.id)),
                        )
                        .order_by(col(WorkflowManifest.revision))
                    )
                )
                .scalars()
                .all()
            )
            revision_ids = [
                _required_uuid(revision.id, "WorkflowManifest") for revision in revisions
            ]
            count_rows = (
                await session.execute(
                    select(
                        col(ManifestArtifactBinding.workflow_manifest_id),
                        func.count(col(ManifestArtifactBinding.id)),
                    )
                    .where(col(ManifestArtifactBinding.workflow_manifest_id).in_(revision_ids))
                    .group_by(col(ManifestArtifactBinding.workflow_manifest_id))
                )
            ).all()
            counts = {manifest_id: int(count) for manifest_id, count in count_rows}
            bindings = (
                (
                    await session.execute(
                        select(ManifestArtifactBinding)
                        .where(
                            col(ManifestArtifactBinding.workflow_manifest_id)
                            == workflow_manifest_id
                        )
                        .order_by(col(ManifestArtifactBinding.artifact_key))
                    )
                )
                .scalars()
                .all()
            )
            visible_binding_artifact_ids = set(
                (
                    await session.execute(
                        select(col(ManifestArtifactBinding.artifact_file_id)).where(
                            col(ManifestArtifactBinding.workflow_manifest_id)
                            == workflow_manifest_id,
                            col(ManifestArtifactBinding.artifact_file_id).is_not(None),
                            artifact_id_is_visible(
                                scope,
                                col(ManifestArtifactBinding.artifact_file_id),
                            ),
                        )
                    )
                ).scalars()
            )
        summary = _manifest_summary(manifest, len(bindings))
        return WorkflowManifestDetail(
            **summary.model_dump(),
            validation_metadata_json=json.dumps(
                manifest.validation_metadata,
                sort_keys=True,
                separators=(",", ":"),
            ),
            revisions=[
                _manifest_summary(
                    revision,
                    counts.get(_required_uuid(revision.id, "WorkflowManifest"), 0),
                )
                for revision in revisions
            ],
            artifact_bindings=[
                _binding_summary(
                    binding,
                    artifact_file_id_visible=(
                        binding.artifact_file_id is None
                        or binding.artifact_file_id in visible_binding_artifact_ids
                    ),
                )
                for binding in bindings
            ],
        )

    @query  # type: ignore[untyped-decorator]
    async def list_manifest_artifact_bindings(
        cls,
        workflow_manifest_id: UUID | None = None,
        artifact_file_id: UUID | None = None,
        reaction_key: str | None = None,
        path_key: str | None = None,
        node_key: str | None = None,
        artifact_role: ManifestArtifactRole | None = None,
        resolution_status: ArtifactResolutionStatus | None = None,
        limit: PageLimit = 100,
        offset: PageOffset = 0,
    ) -> ManifestArtifactBindingPage:
        scope = await query_visibility_scope()
        predicates: list[Any] = [
            manifest_binding_id_is_visible(scope, col(ManifestArtifactBinding.id))
        ]
        for field, value in (
            (ManifestArtifactBinding.workflow_manifest_id, workflow_manifest_id),
            (ManifestArtifactBinding.artifact_file_id, artifact_file_id),
            (ManifestArtifactBinding.reaction_key, reaction_key),
            (ManifestArtifactBinding.path_key, path_key),
            (ManifestArtifactBinding.node_key, node_key),
            (ManifestArtifactBinding.artifact_role, artifact_role),
            (ManifestArtifactBinding.resolution_status, resolution_status),
        ):
            if value is not None:
                predicates.append(col(field) == value)
        if artifact_file_id is not None:
            predicates.append(artifact_id_is_visible(scope, literal(artifact_file_id)))
        count_statement = (
            select(func.count()).select_from(ManifestArtifactBinding).where(*predicates)
        )
        artifact_file_visible = or_(
            col(ManifestArtifactBinding.artifact_file_id).is_(None),
            artifact_id_is_visible(scope, col(ManifestArtifactBinding.artifact_file_id)),
        )
        statement = (
            select(ManifestArtifactBinding, artifact_file_visible.label("artifact_file_visible"))
            .where(*predicates)
            .order_by(
                col(ManifestArtifactBinding.workflow_manifest_id),
                col(ManifestArtifactBinding.artifact_key),
            )
            .offset(offset)
            .limit(limit)
        )
        async with session_factory() as session:
            total = int((await session.execute(count_statement)).scalar_one())
            bindings = (await session.execute(statement)).all()
        return ManifestArtifactBindingPage(
            items=[
                _binding_summary(binding, artifact_file_id_visible=bool(target_visible))
                for binding, target_visible in bindings
            ],
            page=_page(total, limit, offset),
        )

    @query  # type: ignore[untyped-decorator]
    async def get_manifest_artifact_binding(
        cls,
        binding_id: UUID,
    ) -> ManifestArtifactBindingDetail | None:
        scope = await query_visibility_scope()
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(
                        ManifestArtifactBinding,
                        or_(
                            col(ManifestArtifactBinding.artifact_file_id).is_(None),
                            artifact_id_is_visible(
                                scope,
                                col(ManifestArtifactBinding.artifact_file_id),
                            ),
                        ).label("artifact_file_visible"),
                    ).where(
                        col(ManifestArtifactBinding.id) == binding_id,
                        manifest_binding_id_is_visible(
                            scope,
                            col(ManifestArtifactBinding.id),
                        ),
                    )
                )
            ).first()
            if row is None:
                return None
            binding, artifact_file_visible = row
            source_id = None
            if binding.source_geometry_artifact_key is not None:
                source_id = (
                    await session.execute(
                        select(col(ManifestArtifactBinding.id)).where(
                            col(ManifestArtifactBinding.workflow_manifest_id)
                            == binding.workflow_manifest_id,
                            col(ManifestArtifactBinding.artifact_key)
                            == binding.source_geometry_artifact_key,
                        )
                    )
                ).scalar_one_or_none()
            dependent_ids = (
                (
                    await session.execute(
                        select(col(ManifestArtifactBinding.id))
                        .where(
                            col(ManifestArtifactBinding.workflow_manifest_id)
                            == binding.workflow_manifest_id,
                            col(ManifestArtifactBinding.source_geometry_artifact_key)
                            == binding.artifact_key,
                        )
                        .order_by(col(ManifestArtifactBinding.artifact_key))
                    )
                )
                .scalars()
                .all()
            )
        return ManifestArtifactBindingDetail(
            **_binding_summary(
                binding,
                artifact_file_id_visible=bool(artifact_file_visible),
            ).model_dump(),
            source_geometry_binding_id=source_id,
            dependent_binding_ids=list(dependent_ids),
        )


class StorageGarbageCollectionQueryService(UseCaseService):  # type: ignore[misc]
    """Inspect object-store GC watermarks and immutable run audits."""

    @query  # type: ignore[untyped-decorator]
    async def list_storage_gc_states(
        cls,
        bucket: str | None = None,
        root_prefix: str | None = None,
        limit: PageLimit = 50,
        offset: PageOffset = 0,
    ) -> StorageGarbageCollectionStatePage:
        predicates: list[Any] = []
        if bucket is not None:
            predicates.append(col(StorageGarbageCollectionState.bucket) == bucket)
        if root_prefix is not None:
            predicates.append(col(StorageGarbageCollectionState.root_prefix) == root_prefix)
        count_statement = (
            select(func.count()).select_from(StorageGarbageCollectionState).where(*predicates)
        )
        statement = (
            select(StorageGarbageCollectionState)
            .where(*predicates)
            .order_by(
                col(StorageGarbageCollectionState.bucket),
                col(StorageGarbageCollectionState.root_prefix),
            )
            .offset(offset)
            .limit(limit)
        )
        async with session_factory() as session:
            total = int((await session.execute(count_statement)).scalar_one())
            states = (await session.execute(statement)).scalars().all()
            summaries = [await _state_summary(session, state) for state in states]
        return StorageGarbageCollectionStatePage(
            items=summaries,
            page=_page(total, limit, offset),
        )

    @query  # type: ignore[untyped-decorator]
    async def get_storage_gc_state(
        cls,
        state_id: UUID,
        recent_run_limit: PageLimit = 20,
    ) -> StorageGarbageCollectionStateDetail | None:
        async with session_factory() as session:
            state = await session.get(StorageGarbageCollectionState, state_id)
            if state is None:
                return None
            summary = await _state_summary(session, state)
            runs = (
                (
                    await session.execute(
                        select(StorageGarbageCollectionRun)
                        .where(col(StorageGarbageCollectionRun.state_id) == state_id)
                        .order_by(
                            col(StorageGarbageCollectionRun.started_at).desc(),
                            col(StorageGarbageCollectionRun.id).desc(),
                        )
                        .limit(recent_run_limit)
                    )
                )
                .scalars()
                .all()
            )
        return StorageGarbageCollectionStateDetail(
            **summary.model_dump(),
            recent_runs=[_run_summary(run, state) for run in runs],
        )

    @query  # type: ignore[untyped-decorator]
    async def list_storage_gc_runs(
        cls,
        state_id: UUID | None = None,
        bucket: str | None = None,
        root_prefix: str | None = None,
        status: StorageGarbageCollectionRunStatus | None = None,
        started_after: datetime | None = None,
        started_before: datetime | None = None,
        limit: PageLimit = 100,
        offset: PageOffset = 0,
    ) -> StorageGarbageCollectionRunPage:
        if (
            started_after is not None
            and started_before is not None
            and started_after > started_before
        ):
            raise ValueError("started_after cannot exceed started_before")
        predicates: list[Any] = []
        for field, value in (
            (StorageGarbageCollectionRun.state_id, state_id),
            (StorageGarbageCollectionState.bucket, bucket),
            (StorageGarbageCollectionState.root_prefix, root_prefix),
            (StorageGarbageCollectionRun.status, status),
        ):
            if value is not None:
                predicates.append(col(field) == value)
        if started_after is not None:
            predicates.append(col(StorageGarbageCollectionRun.started_at) >= started_after)
        if started_before is not None:
            predicates.append(col(StorageGarbageCollectionRun.started_at) <= started_before)
        base = (
            select(StorageGarbageCollectionRun, StorageGarbageCollectionState)
            .join(
                StorageGarbageCollectionState,
                col(StorageGarbageCollectionRun.state_id) == col(StorageGarbageCollectionState.id),
            )
            .where(*predicates)
        )
        count_statement = (
            select(func.count())
            .select_from(StorageGarbageCollectionRun)
            .join(
                StorageGarbageCollectionState,
                col(StorageGarbageCollectionRun.state_id) == col(StorageGarbageCollectionState.id),
            )
            .where(*predicates)
        )
        statement = (
            base.order_by(
                col(StorageGarbageCollectionRun.started_at).desc(),
                col(StorageGarbageCollectionRun.id).desc(),
            )
            .offset(offset)
            .limit(limit)
        )
        async with session_factory() as session:
            total = int((await session.execute(count_statement)).scalar_one())
            rows = (await session.execute(statement)).all()
        return StorageGarbageCollectionRunPage(
            items=[_run_summary(run, state) for run, state in rows],
            page=_page(total, limit, offset),
        )

    @query  # type: ignore[untyped-decorator]
    async def get_storage_gc_run(
        cls,
        run_id: UUID,
    ) -> StorageGarbageCollectionRunSummary | None:
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(StorageGarbageCollectionRun, StorageGarbageCollectionState)
                    .join(
                        StorageGarbageCollectionState,
                        col(StorageGarbageCollectionRun.state_id)
                        == col(StorageGarbageCollectionState.id),
                    )
                    .where(col(StorageGarbageCollectionRun.id) == run_id)
                )
            ).first()
        return _run_summary(*row) if row is not None else None


async def _state_summary(
    session: Any,
    state: StorageGarbageCollectionState,
) -> StorageGarbageCollectionStateSummary:
    state_id = _required_uuid(state.id, "StorageGarbageCollectionState")
    latest_run = (
        await session.execute(
            select(StorageGarbageCollectionRun)
            .where(col(StorageGarbageCollectionRun.state_id) == state_id)
            .order_by(
                col(StorageGarbageCollectionRun.started_at).desc(),
                col(StorageGarbageCollectionRun.id).desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    latest_failed = (
        await session.execute(
            select(StorageGarbageCollectionRun)
            .where(
                col(StorageGarbageCollectionRun.state_id) == state_id,
                col(StorageGarbageCollectionRun.status) == StorageGarbageCollectionRunStatus.FAILED,
            )
            .order_by(
                col(StorageGarbageCollectionRun.started_at).desc(),
                col(StorageGarbageCollectionRun.id).desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    run_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(StorageGarbageCollectionRun)
                .where(col(StorageGarbageCollectionRun.state_id) == state_id)
            )
        ).scalar_one()
    )
    return StorageGarbageCollectionStateSummary(
        id=state_id,
        bucket=state.bucket,
        root_prefix=state.root_prefix,
        watermark_at=state.watermark_at,
        updated_at=state.updated_at,
        last_successful_run_id=state.last_successful_run_id,
        latest_run_id=latest_run.id if latest_run is not None else None,
        latest_run_status=_enum_value(latest_run.status) if latest_run is not None else None,
        latest_failed_run_id=latest_failed.id if latest_failed is not None else None,
        run_count=run_count,
        created_at=state.created_at,
    )


class MolecularTopologyDerivationQueryService(UseCaseService):  # type: ignore[misc]
    """Inspect immutable topology reconstruction provenance and frame usage."""

    @query  # type: ignore[untyped-decorator]
    async def list_topology_derivations(
        cls,
        topology_id: UUID | None = None,
        reconstruction_method: str | None = None,
        reconstruction_version: str | None = None,
        provenance_schema_version: str | None = None,
        provenance_hash: str | None = None,
        limit: PageLimit = 50,
        offset: PageOffset = 0,
    ) -> MolecularTopologyDerivationPage:
        scope = await query_visibility_scope()
        predicates: list[Any] = [
            topology_derivation_id_is_visible(
                scope,
                col(MolecularTopologyDerivation.id),
            )
        ]
        for field, value in (
            (MolecularTopologyDerivation.topology_id, topology_id),
            (MolecularTopologyDerivation.reconstruction_method, reconstruction_method),
            (MolecularTopologyDerivation.reconstruction_version, reconstruction_version),
            (MolecularTopologyDerivation.provenance_schema_version, provenance_schema_version),
            (MolecularTopologyDerivation.provenance_hash, provenance_hash),
        ):
            if value is not None:
                predicates.append(col(field) == value)
        count_statement = (
            select(func.count()).select_from(MolecularTopologyDerivation).where(*predicates)
        )
        statement = (
            select(MolecularTopologyDerivation, MolecularTopology)
            .join(
                MolecularTopology,
                col(MolecularTopologyDerivation.topology_id) == col(MolecularTopology.id),
            )
            .where(*predicates)
            .order_by(
                col(MolecularTopologyDerivation.topology_id),
                col(MolecularTopologyDerivation.created_at),
            )
            .offset(offset)
            .limit(limit)
        )
        async with session_factory() as session:
            total = int((await session.execute(count_statement)).scalar_one())
            rows = (await session.execute(statement)).all()
            summaries = [
                await _derivation_summary(session, derivation, topology, scope)
                for derivation, topology in rows
            ]
        return MolecularTopologyDerivationPage(
            items=summaries,
            page=_page(total, limit, offset),
        )

    @query  # type: ignore[untyped-decorator]
    async def get_topology_derivation(
        cls,
        derivation_id: UUID,
    ) -> MolecularTopologyDerivationDetail | None:
        scope = await query_visibility_scope()
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(MolecularTopologyDerivation, MolecularTopology)
                    .join(
                        MolecularTopology,
                        col(MolecularTopologyDerivation.topology_id) == col(MolecularTopology.id),
                    )
                    .where(
                        col(MolecularTopologyDerivation.id) == derivation_id,
                        topology_derivation_id_is_visible(
                            scope,
                            col(MolecularTopologyDerivation.id),
                        ),
                    )
                )
            ).first()
            if row is None:
                return None
            derivation, topology = row
            summary = await _derivation_summary(session, derivation, topology, scope)
        return MolecularTopologyDerivationDetail(
            **summary.model_dump(),
            reconstruction_metadata_json=json.dumps(
                derivation.reconstruction_metadata,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )


async def _derivation_summary(
    session: Any,
    derivation: MolecularTopologyDerivation,
    topology: MolecularTopology,
    scope: Any,
) -> MolecularTopologyDerivationSummary:
    derivation_id = _required_uuid(derivation.id, "MolecularTopologyDerivation")
    frame_count, geometry_count = (
        await session.execute(
            select(
                func.count(col(CalculationFrame.id)),
                func.count(func.distinct(col(CalculationFrame.geometry_id))),
            ).where(
                col(CalculationFrame.topology_derivation_id) == derivation_id,
                frame_id_is_visible(scope, col(CalculationFrame.id)),
            )
        )
    ).one()
    return MolecularTopologyDerivationSummary(
        id=derivation_id,
        topology_id=derivation.topology_id,
        canonical_isomeric_smiles=topology.canonical_isomeric_smiles,
        reconstruction_method=derivation.reconstruction_method,
        reconstruction_version=derivation.reconstruction_version,
        provenance_schema_version=derivation.provenance_schema_version,
        provenance_hash=derivation.provenance_hash,
        referenced_geometry_count=int(geometry_count),
        calculation_frame_count=int(frame_count),
        created_at=derivation.created_at,
    )


__all__ = [
    "MolecularTopologyDerivationQueryService",
    "StorageGarbageCollectionQueryService",
    "WorkflowManifestQueryService",
]
