"""Artifact-rooted visibility predicates shared by every read transport."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select, true
from sqlmodel import col

from tricycle_reaction_db.application.services.authentication import (
    AuthenticatedPrincipal,
    current_principal,
)
from tricycle_reaction_db.application.services.authorization import (
    AuthorizationService,
    ProjectPermission,
)
from tricycle_reaction_db.application.services.reaction_geometry_policy import (
    geometry_has_thermodynamic_property_predicate,
)
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    CalculationFrame,
    CalculationSegment,
    Geometry,
    LogicalReactionParticipant,
    ManifestArtifactBinding,
    MappedReaction,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionParticipant,
    ParseRevision,
    TransitionStateEndpoint,
    TransitionStateInference,
    WorkflowManifest,
)
from tricycle_reaction_db.domain.enums import ArtifactVisibility, StorageStatus


@dataclass(frozen=True, slots=True)
class QueryVisibilityScope:
    """Projects and public artifacts visible to one request principal."""

    principal: AuthenticatedPrincipal | None
    project_ids: frozenset[UUID]
    permission: ProjectPermission = ProjectPermission.ARTIFACT_READ
    unrestricted: bool = False

    @property
    def is_authenticated(self) -> bool:
        return self.principal is not None

    def artifact_predicate(self) -> Any:
        if self.unrestricted:
            return true()
        return or_(
            and_(
                col(ArtifactFile.storage_status) != StorageStatus.RETIRED,
                col(ArtifactFile.visibility) == ArtifactVisibility.PUBLIC,
            ),
            and_(
                col(ArtifactFile.storage_status) != StorageStatus.RETIRED,
                col(ArtifactFile.visibility) == ArtifactVisibility.PROJECT,
                self.project_access_predicate(col(ArtifactFile.project_id)),
            ),
        )

    def project_access_predicate(self, project_id: Any) -> Any:
        if self.principal is None:
            return project_id.in_(self.project_ids)
        predicate = AuthorizationService.project_permission_predicate(
            self.principal.user_id,
            project_id,
            self.permission,
        )
        if self.project_ids:
            return and_(predicate, project_id.in_(self.project_ids))
        return predicate


async def query_visibility_scope(
    permission: ProjectPermission = ProjectPermission.ARTIFACT_READ,
    project_id: UUID | None = None,
) -> QueryVisibilityScope:
    principal = current_principal()
    project_ids = (
        frozenset({project_id}) if principal is not None and project_id is not None else frozenset()
    )
    return QueryVisibilityScope(
        principal=principal,
        project_ids=project_ids,
        permission=permission,
    )


def visible_artifact_ids(scope: QueryVisibilityScope) -> Any:
    if scope.unrestricted:
        return select(col(ArtifactFile.id))
    return select(col(ArtifactFile.id)).where(scope.artifact_predicate())


def visible_parse_revision_ids(scope: QueryVisibilityScope) -> Any:
    if scope.unrestricted:
        return select(col(ParseRevision.id))
    return select(col(ParseRevision.id)).where(
        col(ParseRevision.artifact_file_id).in_(visible_artifact_ids(scope))
    )


def visible_frame_ids(scope: QueryVisibilityScope) -> Any:
    if scope.unrestricted:
        return select(col(CalculationFrame.id))
    return select(col(CalculationFrame.id)).where(
        col(CalculationFrame.parse_revision_id).in_(visible_parse_revision_ids(scope))
    )


def artifact_id_is_visible(scope: QueryVisibilityScope, artifact_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    return artifact_id.in_(visible_artifact_ids(scope))


def parse_revision_id_is_visible(scope: QueryVisibilityScope, revision_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    return revision_id.in_(visible_parse_revision_ids(scope))


def frame_id_is_visible(scope: QueryVisibilityScope, frame_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    return frame_id.in_(visible_frame_ids(scope))


def geometry_id_is_visible(scope: QueryVisibilityScope, geometry_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    return geometry_id.in_(
        select(col(CalculationFrame.geometry_id)).where(
            col(CalculationFrame.id).in_(visible_frame_ids(scope))
        )
    )


def topology_derivation_id_is_visible(scope: QueryVisibilityScope, derivation_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    return derivation_id.in_(
        select(col(CalculationFrame.topology_derivation_id)).where(
            col(CalculationFrame.id).in_(visible_frame_ids(scope))
        )
    )


def topology_id_is_visible(scope: QueryVisibilityScope, topology_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    geometry_topology_ids = select(col(Geometry.topology_id)).where(
        geometry_id_is_visible(scope, col(Geometry.id))
    )
    endpoint_topology_ids = select(col(TransitionStateEndpoint.topology_id)).where(
        col(TransitionStateEndpoint.calculation_frame_id).in_(visible_frame_ids(scope))
    )
    participant_topology_ids = (
        select(col(LogicalReactionParticipant.topology_id))
        .join(
            MappedReactionParticipant,
            col(MappedReactionParticipant.logical_reaction_participant_id)
            == col(LogicalReactionParticipant.id),
        )
        .where(
            _mapped_reaction_id_has_visible_source(
                scope,
                col(MappedReactionParticipant.mapped_reaction_id),
            )
        )
    )
    return topology_id.in_(
        geometry_topology_ids.union(endpoint_topology_ids, participant_topology_ids)
    )


def _mapped_reaction_ids_with_calculations(frame_ids: Any) -> Any:
    return (
        select(col(MappedReactionNode.mapped_reaction_id))
        .join(
            MappedReactionNodeGeometry,
            col(MappedReactionNodeGeometry.mapped_reaction_node_id) == col(MappedReactionNode.id),
        )
        .where(
            col(MappedReactionNodeGeometry.geometry_id).in_(
                select(col(CalculationFrame.geometry_id)).where(
                    col(CalculationFrame.id).in_(frame_ids)
                )
            ),
            geometry_has_thermodynamic_property_predicate(
                col(MappedReactionNodeGeometry.geometry_id)
            ),
        )
    )


def _mapped_reaction_ids_with_inferences(revision_ids: Any) -> Any:
    return select(col(TransitionStateInference.mapped_reaction_id)).where(
        col(TransitionStateInference.mapped_reaction_id).is_not(None),
        col(TransitionStateInference.parse_revision_id).in_(revision_ids),
    )


def _mapped_reaction_id_has_visible_source(
    scope: QueryVisibilityScope,
    mapped_reaction_id: Any,
) -> Any:
    return or_(
        mapped_reaction_id.in_(_mapped_reaction_ids_with_calculations(visible_frame_ids(scope))),
        mapped_reaction_id.in_(
            _mapped_reaction_ids_with_inferences(visible_parse_revision_ids(scope))
        ),
    )


def mapped_reaction_id_is_visible(scope: QueryVisibilityScope, mapped_reaction_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    visible_source = _mapped_reaction_id_has_visible_source(scope, mapped_reaction_id)
    if not scope.is_authenticated:
        return visible_source

    any_source = or_(
        mapped_reaction_id.in_(
            _mapped_reaction_ids_with_calculations(select(col(CalculationFrame.id)))
        ),
        mapped_reaction_id.in_(_mapped_reaction_ids_with_inferences(select(col(ParseRevision.id)))),
    )
    return or_(visible_source, and_(mapped_reaction_id.is_not(None), ~any_source))


def logical_reaction_id_is_visible(scope: QueryVisibilityScope, logical_reaction_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    return logical_reaction_id.in_(
        select(col(MappedReaction.logical_reaction_id)).where(
            mapped_reaction_id_is_visible(scope, col(MappedReaction.id))
        )
    )


def workflow_manifest_id_is_visible(scope: QueryVisibilityScope, manifest_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    return manifest_id.in_(
        select(col(WorkflowManifest.id)).where(
            artifact_id_is_visible(scope, col(WorkflowManifest.artifact_file_id))
        )
    )


def manifest_binding_id_is_visible(scope: QueryVisibilityScope, binding_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    return binding_id.in_(
        select(col(ManifestArtifactBinding.id)).where(
            workflow_manifest_id_is_visible(
                scope,
                col(ManifestArtifactBinding.workflow_manifest_id),
            )
        )
    )


def calculation_segment_id_is_visible(scope: QueryVisibilityScope, segment_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    return segment_id.in_(
        select(col(CalculationSegment.id)).where(
            parse_revision_id_is_visible(scope, col(CalculationSegment.parse_revision_id))
        )
    )


def calculation_protocol_id_is_visible(scope: QueryVisibilityScope, protocol_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    return protocol_id.in_(
        select(col(CalculationSegment.protocol_id)).where(
            col(CalculationSegment.protocol_id).is_not(None),
            parse_revision_id_is_visible(scope, col(CalculationSegment.parse_revision_id)),
        )
    )


__all__ = [
    "QueryVisibilityScope",
    "artifact_id_is_visible",
    "calculation_protocol_id_is_visible",
    "calculation_segment_id_is_visible",
    "frame_id_is_visible",
    "geometry_id_is_visible",
    "logical_reaction_id_is_visible",
    "manifest_binding_id_is_visible",
    "mapped_reaction_id_is_visible",
    "parse_revision_id_is_visible",
    "query_visibility_scope",
    "topology_derivation_id_is_visible",
    "topology_id_is_visible",
    "visible_artifact_ids",
    "visible_frame_ids",
    "visible_parse_revision_ids",
    "workflow_manifest_id_is_visible",
]
