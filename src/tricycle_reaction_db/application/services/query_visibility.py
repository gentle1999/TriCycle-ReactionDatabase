"""Artifact-rooted visibility predicates shared by every read transport."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import and_, false, func, or_, select, true
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import aliased
from sqlmodel import col

from tricycle_reaction_db.application.query_cost import QueryProjectScopeRequired
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
    MappedReactionParticipant,
    MappedReactionThermodynamicProfile,
    MolecularFormula,
    MolecularTopology,
    MolecularTopologyDerivation,
    ParseRevision,
    ProjectGeometryCatalog,
    TransitionStateEndpoint,
    TransitionStateInference,
    WorkflowManifest,
)
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactVisibility,
    ParseStatus,
    StorageStatus,
)


@dataclass(frozen=True, slots=True)
class QueryVisibilityScope:
    """Projects and public artifacts visible to one request principal."""

    principal: AuthenticatedPrincipal | None
    project_ids: frozenset[UUID]
    permission: ProjectPermission = ProjectPermission.ARTIFACT_READ
    unrestricted: bool = False
    requested_project_id: UUID | None = None
    requested_project_permitted: bool = False

    @property
    def is_authenticated(self) -> bool:
        return self.principal is not None

    @property
    def uses_project_geometry_catalog(self) -> bool:
        """Whether the request can use the project-owned geometry directory."""

        return self.uses_project_owned_fast_path

    @property
    def uses_project_owned_fast_path(self) -> bool:
        """Whether direct project ownership is sufficient for normal reads.

        Migrations ``0035``--``0037`` make project ownership non-null and
        immutable for derived roots, and database triggers reject all
        cross-project relationship writes.  After the historical reset, a
        permitted project query can therefore use the indexed owner column as
        its read boundary.  The expensive source-chain predicates remain
        available for unrestricted maintenance/audit queries and transitional
        scopes, but should not be repeated for every ordinary list row.
        """

        return (
            not self.unrestricted
            and self.requested_project_id is not None
            and self.requested_project_permitted
        )

    def artifact_predicate(self, artifact_model: Any = ArtifactFile) -> Any:
        """Build the artifact boundary for a model or an aliased model."""

        artifact_project_id = col(artifact_model.project_id)
        artifact_storage_status = col(artifact_model.storage_status)
        artifact_visibility = col(artifact_model.visibility)
        if self.unrestricted:
            return true()
        if self.requested_project_id is not None:
            requested_project_artifacts = and_(
                artifact_project_id == self.requested_project_id,
                artifact_storage_status != StorageStatus.RETIRED,
            )
            if self.requested_project_permitted:
                return requested_project_artifacts
            return and_(
                requested_project_artifacts,
                artifact_visibility == ArtifactVisibility.PUBLIC,
            )
        return or_(
            and_(
                artifact_storage_status != StorageStatus.RETIRED,
                artifact_visibility == ArtifactVisibility.PUBLIC,
            ),
            and_(
                artifact_storage_status != StorageStatus.RETIRED,
                artifact_visibility == ArtifactVisibility.PROJECT,
                self.project_access_predicate(artifact_project_id),
            ),
        )

    def derived_artifact_predicate(self, artifact_model: Any = ArtifactFile) -> Any:
        """Build the private source boundary for parsed and derived rows.

        ``ArtifactFile`` is the only object that may be discovered through its
        public visibility flag. Parse revisions, ingestion state, calculation
        frames, and every later projection are project data even when their
        source file is public, so they require both the explicit requested
        project and the authenticated principal's project permission.
        """

        artifact_project_id = col(artifact_model.project_id)
        artifact_storage_status = col(artifact_model.storage_status)
        if self.unrestricted:
            return true()
        if self.requested_project_id is None or not self.requested_project_permitted:
            return false()
        return and_(
            artifact_project_id == self.requested_project_id,
            artifact_storage_status != StorageStatus.RETIRED,
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
    if project_id is None:
        raise QueryProjectScopeRequired()
    principal = current_principal()
    requested_project_permitted = False
    if principal is not None and project_id is not None:
        requested_project_permitted = await AuthorizationService.has_project_permission(
            principal.user_id,
            project_id,
            permission,
        )
    project_ids: frozenset[UUID] = (
        frozenset({project_id})
        if requested_project_permitted and project_id is not None
        else frozenset()
    )
    return QueryVisibilityScope(
        principal=principal,
        project_ids=project_ids,
        permission=permission,
        requested_project_id=project_id,
        requested_project_permitted=requested_project_permitted,
    )


def visible_artifact_ids(scope: QueryVisibilityScope) -> Any:
    if scope.unrestricted:
        return select(col(ArtifactFile.id))
    return select(col(ArtifactFile.id)).where(scope.artifact_predicate())


def visible_parse_revision_ids(scope: QueryVisibilityScope) -> Any:
    if scope.unrestricted:
        return select(col(ParseRevision.id))
    return (
        select(col(ParseRevision.id))
        .join(
            ArtifactFile,
            col(ParseRevision.artifact_file_id) == col(ArtifactFile.id),
        )
        .join(
            ArtifactIngestion,
            col(ArtifactIngestion.artifact_file_id) == col(ArtifactFile.id),
        )
        .where(
            scope.derived_artifact_predicate(),
            col(ParseRevision.status) == ParseStatus.SUCCEEDED,
            col(ArtifactIngestion.status) == ArtifactIngestionStatus.SUCCEEDED,
        )
    )


def visible_frame_ids(scope: QueryVisibilityScope) -> Any:
    if scope.unrestricted:
        return select(col(CalculationFrame.id))
    if scope.uses_project_owned_fast_path:
        # The frame has no redundant project_id.  Its source ArtifactFile,
        # Geometry, and topology derivation do, and 0036 rejects a mismatch at
        # write time.  Keep those indexed equality checks in the fast path;
        # do not rebuild the complete provenance graph for every frame.
        return (
            select(col(CalculationFrame.id))
            .join(
                ParseRevision,
                col(CalculationFrame.parse_revision_id) == col(ParseRevision.id),
            )
            .join(
                ArtifactFile,
                col(ParseRevision.artifact_file_id) == col(ArtifactFile.id),
            )
            .join(
                ArtifactIngestion,
                col(ArtifactIngestion.artifact_file_id) == col(ArtifactFile.id),
            )
            .join(Geometry, col(CalculationFrame.geometry_id) == col(Geometry.id))
            .join(
                MolecularTopologyDerivation,
                col(CalculationFrame.topology_derivation_id)
                == col(MolecularTopologyDerivation.id),
            )
            .where(
                col(ArtifactFile.project_id) == scope.requested_project_id,
                col(ArtifactFile.storage_status) != StorageStatus.RETIRED,
                col(ArtifactIngestion.status) == ArtifactIngestionStatus.SUCCEEDED,
                col(ParseRevision.status) == ParseStatus.SUCCEEDED,
                col(Geometry.project_id) == scope.requested_project_id,
                col(MolecularTopologyDerivation.project_id)
                == scope.requested_project_id,
                col(CalculationFrame.geometry_id).is_not(None),
            )
        )
    return (
        select(col(CalculationFrame.id))
        .join(
            ParseRevision,
            col(CalculationFrame.parse_revision_id) == col(ParseRevision.id),
        )
        .join(
            CalculationSegment,
            col(CalculationFrame.segment_id) == col(CalculationSegment.id),
        )
        .join(
            ArtifactFile,
            col(ParseRevision.artifact_file_id) == col(ArtifactFile.id),
        )
        .outerjoin(
            CalculationProtocol,
            col(CalculationSegment.protocol_id) == col(CalculationProtocol.id),
        )
        .join(
            ArtifactIngestion,
            col(ArtifactIngestion.artifact_file_id) == col(ArtifactFile.id),
        )
        .join(
            Geometry,
            col(CalculationFrame.geometry_id) == col(Geometry.id),
        )
        .join(
            MolecularTopologyDerivation,
            col(CalculationFrame.topology_derivation_id)
            == col(MolecularTopologyDerivation.id),
        )
        .where(
            scope.derived_artifact_predicate(),
            col(ArtifactIngestion.status) == ArtifactIngestionStatus.SUCCEEDED,
            _derived_project_owner_is_visible(scope, col(Geometry.project_id)),
            col(Geometry.project_id) == col(ArtifactFile.project_id),
            _protocol_project_is_visible(
                scope,
                col(CalculationSegment.protocol_id),
                col(CalculationProtocol.project_id),
                col(ArtifactFile.project_id),
            ),
            col(ParseRevision.status) == ParseStatus.SUCCEEDED,
            col(CalculationFrame.geometry_id).is_not(None),
            _derived_project_owner_is_visible(
                scope,
                col(MolecularTopologyDerivation.project_id),
            ),
        )
    )


def _visible_geometry_ids_from_frames(scope: QueryVisibilityScope) -> Any:
    """Geometry IDs backed by a non-retired, successfully ingested revision."""

    return (
        select(col(CalculationFrame.geometry_id))
        .join(
            ParseRevision,
            col(CalculationFrame.parse_revision_id) == col(ParseRevision.id),
        )
        .join(
            CalculationSegment,
            col(CalculationFrame.segment_id) == col(CalculationSegment.id),
        )
        .join(
            ArtifactFile,
            col(ParseRevision.artifact_file_id) == col(ArtifactFile.id),
        )
        .outerjoin(
            CalculationProtocol,
            col(CalculationSegment.protocol_id) == col(CalculationProtocol.id),
        )
        .join(
            ArtifactIngestion,
            col(ArtifactIngestion.artifact_file_id) == col(ArtifactFile.id),
        )
        .join(
            Geometry,
            col(CalculationFrame.geometry_id) == col(Geometry.id),
        )
        .join(
            MolecularTopologyDerivation,
            col(CalculationFrame.topology_derivation_id)
            == col(MolecularTopologyDerivation.id),
        )
        .where(
            scope.derived_artifact_predicate(),
            col(ArtifactIngestion.status) == ArtifactIngestionStatus.SUCCEEDED,
            _derived_project_owner_is_visible(scope, col(Geometry.project_id)),
            col(Geometry.project_id) == col(ArtifactFile.project_id),
            _protocol_project_is_visible(
                scope,
                col(CalculationSegment.protocol_id),
                col(CalculationProtocol.project_id),
                col(ArtifactFile.project_id),
            ),
            col(ParseRevision.status) == ParseStatus.SUCCEEDED,
            col(CalculationFrame.geometry_id).is_not(None),
            _derived_project_owner_is_visible(
                scope,
                col(MolecularTopologyDerivation.project_id),
            ),
        )
    )


def calculation_frame_is_visible(scope: QueryVisibilityScope, parse_revision_id: Any) -> Any:
    """Filter an already-selected calculation frame without rescanning its table."""

    if scope.unrestricted:
        return true()
    return parse_revision_id.in_(visible_parse_revision_ids(scope))


def visible_geometry_ids(scope: QueryVisibilityScope) -> Any:
    """Return geometry IDs evidenced by calculation frames visible in the scope."""

    if scope.unrestricted:
        return select(col(Geometry.id))
    if scope.uses_project_owned_fast_path:
        return (
            select(col(ProjectGeometryCatalog.geometry_id))
            .join(
                Geometry,
                col(Geometry.id) == col(ProjectGeometryCatalog.geometry_id),
            )
            .where(
                col(ProjectGeometryCatalog.project_id) == scope.requested_project_id,
                col(Geometry.project_id) == scope.requested_project_id,
            )
        )
    return (
        select(col(CalculationFrame.geometry_id))
        .join(Geometry, col(Geometry.id) == col(CalculationFrame.geometry_id))
        .where(
            frame_id_is_visible(scope, col(CalculationFrame.id)),
            _derived_project_owner_is_visible(scope, col(Geometry.project_id)),
        )
    )


def _geometry_source_is_visible(scope: QueryVisibilityScope, geometry_id: Any) -> Any:
    """Check one Geometry's successful source chain without materializing all frames."""

    if scope.unrestricted:
        return true()
    frame = aliased(CalculationFrame, name="visibility_geometry_source_frame")
    revision = aliased(ParseRevision, name="visibility_geometry_source_revision")
    artifact = aliased(ArtifactFile, name="visibility_geometry_source_artifact")
    ingestion = aliased(ArtifactIngestion, name="visibility_geometry_source_ingestion")
    geometry = aliased(Geometry, name="visibility_geometry_source_geometry")
    segment = aliased(CalculationSegment, name="visibility_geometry_source_segment")
    protocol = aliased(CalculationProtocol, name="visibility_geometry_source_protocol")
    derivation = aliased(
        MolecularTopologyDerivation,
        name="visibility_geometry_source_derivation",
    )
    return (
        select(1)
        .select_from(frame)
        .join(revision, col(frame.parse_revision_id) == col(revision.id))
        .join(segment, col(frame.segment_id) == col(segment.id))
        .join(artifact, col(revision.artifact_file_id) == col(artifact.id))
        .outerjoin(protocol, col(segment.protocol_id) == col(protocol.id))
        .join(ingestion, col(ingestion.artifact_file_id) == col(artifact.id))
        .join(geometry, col(frame.geometry_id) == col(geometry.id))
        .join(derivation, col(frame.topology_derivation_id) == col(derivation.id))
        .where(
            col(frame.geometry_id) == geometry_id,
            scope.derived_artifact_predicate(artifact),
            col(ingestion.status) == ArtifactIngestionStatus.SUCCEEDED,
            col(revision.status) == ParseStatus.SUCCEEDED,
            _derived_project_owner_is_visible(scope, col(geometry.project_id)),
            col(geometry.project_id) == col(artifact.project_id),
            _protocol_project_is_visible(
                scope,
                col(segment.protocol_id),
                col(protocol.project_id),
                col(artifact.project_id),
            ),
            _derived_project_owner_is_visible(scope, col(derivation.project_id)),
        )
        .exists()
    )


def _project_geometry_catalog_source_is_visible(
    scope: QueryVisibilityScope,
    geometry_id: Any,
    *,
    require_thermodynamic_property: bool = False,
) -> Any:
    """Authorize one project-catalog Geometry through its own source chain."""

    if not scope.uses_project_geometry_catalog:
        return geometry_id.in_(visible_geometry_ids(scope))
    geometry = aliased(Geometry, name="visibility_project_catalog_geometry")
    catalog = aliased(ProjectGeometryCatalog, name="visibility_project_catalog")
    predicates = [
        col(catalog.project_id) == scope.requested_project_id,
        col(catalog.geometry_id) == geometry_id,
        col(geometry.id) == col(catalog.geometry_id),
        _derived_project_owner_is_visible(scope, col(geometry.project_id)),
    ]
    if not scope.uses_project_owned_fast_path:
        predicates.append(_geometry_source_is_visible(scope, col(geometry.id)))
    if require_thermodynamic_property:
        predicates.append(col(catalog.has_thermodynamic_property))
    return (
        select(1)
        .select_from(catalog)
        .join(geometry, col(geometry.id) == col(catalog.geometry_id))
        .where(*predicates)
        .exists()
    )


def _successful_source_project_ids_for_geometry(geometry_id: Any) -> Any:
    """Return the projects that own successful, non-retired source frames."""

    frame = aliased(CalculationFrame, name="visibility_geometry_frame")
    revision = aliased(ParseRevision, name="visibility_geometry_revision")
    artifact = aliased(ArtifactFile, name="visibility_geometry_artifact")
    ingestion = aliased(ArtifactIngestion, name="visibility_geometry_ingestion")
    return (
        select(col(artifact.project_id).label("project_id"))
        .select_from(frame)
        .join(revision, col(frame.parse_revision_id) == col(revision.id))
        .join(artifact, col(revision.artifact_file_id) == col(artifact.id))
        .join(ingestion, col(ingestion.artifact_file_id) == col(artifact.id))
        .where(
            col(frame.geometry_id) == geometry_id,
            col(artifact.storage_status) != StorageStatus.RETIRED,
            col(revision.status) == ParseStatus.SUCCEEDED,
            col(ingestion.status) == ArtifactIngestionStatus.SUCCEEDED,
        )
        .distinct()
    )


def _successful_source_project_ids_for_topology(topology_id: Any) -> Any:
    """Return source projects for Geometry and TS-endpoint rows of a topology."""

    geometry = aliased(Geometry, name="visibility_topology_geometry")
    frame = aliased(CalculationFrame, name="visibility_topology_geometry_frame")
    revision = aliased(ParseRevision, name="visibility_topology_geometry_revision")
    artifact = aliased(ArtifactFile, name="visibility_topology_geometry_artifact")
    ingestion = aliased(ArtifactIngestion, name="visibility_topology_geometry_ingestion")
    geometry_projects = (
        select(col(artifact.project_id).label("project_id"))
        .select_from(geometry)
        .join(frame, col(frame.geometry_id) == col(geometry.id))
        .join(revision, col(frame.parse_revision_id) == col(revision.id))
        .join(artifact, col(revision.artifact_file_id) == col(artifact.id))
        .join(ingestion, col(ingestion.artifact_file_id) == col(artifact.id))
        .where(
            col(geometry.topology_id) == topology_id,
            col(artifact.storage_status) != StorageStatus.RETIRED,
            col(revision.status) == ParseStatus.SUCCEEDED,
            col(ingestion.status) == ArtifactIngestionStatus.SUCCEEDED,
        )
    )
    endpoint = aliased(TransitionStateEndpoint, name="visibility_topology_endpoint")
    endpoint_frame = aliased(CalculationFrame, name="visibility_topology_endpoint_frame")
    endpoint_revision = aliased(ParseRevision, name="visibility_topology_endpoint_revision")
    endpoint_artifact = aliased(ArtifactFile, name="visibility_topology_endpoint_artifact")
    endpoint_ingestion = aliased(
        ArtifactIngestion,
        name="visibility_topology_endpoint_ingestion",
    )
    endpoint_projects = (
        select(col(endpoint_artifact.project_id).label("project_id"))
        .select_from(endpoint)
        .join(
            endpoint_frame,
            col(endpoint.calculation_frame_id) == col(endpoint_frame.id),
        )
        .join(
            endpoint_revision,
            col(endpoint_frame.parse_revision_id) == col(endpoint_revision.id),
        )
        .join(
            endpoint_artifact,
            col(endpoint_revision.artifact_file_id) == col(endpoint_artifact.id),
        )
        .join(
            endpoint_ingestion,
            col(endpoint_ingestion.artifact_file_id) == col(endpoint_artifact.id),
        )
        .where(
            col(endpoint.topology_id) == topology_id,
            col(endpoint_artifact.storage_status) != StorageStatus.RETIRED,
            col(endpoint_revision.status) == ParseStatus.SUCCEEDED,
            col(endpoint_ingestion.status) == ArtifactIngestionStatus.SUCCEEDED,
        )
    )
    return geometry_projects.union(endpoint_projects)


def _successful_source_project_ids_for_mapped_reaction(mapped_reaction_id: Any) -> Any:
    """Return all source projects contributing to one mapped reaction."""

    node = aliased(MappedReactionNode, name="visibility_mapped_node")
    node_geometry = aliased(
        MappedReactionNodeGeometry,
        name="visibility_mapped_node_geometry",
    )
    frame = aliased(CalculationFrame, name="visibility_mapped_frame")
    revision = aliased(ParseRevision, name="visibility_mapped_revision")
    artifact = aliased(ArtifactFile, name="visibility_mapped_artifact")
    ingestion = aliased(ArtifactIngestion, name="visibility_mapped_ingestion")
    calculation_projects = (
        select(col(artifact.project_id).label("project_id"))
        .select_from(node)
        .join(
            node_geometry,
            col(node_geometry.mapped_reaction_node_id) == col(node.id),
        )
        .join(frame, col(frame.geometry_id) == col(node_geometry.geometry_id))
        .join(revision, col(frame.parse_revision_id) == col(revision.id))
        .join(artifact, col(revision.artifact_file_id) == col(artifact.id))
        .join(ingestion, col(ingestion.artifact_file_id) == col(artifact.id))
        .where(
            col(node.mapped_reaction_id) == mapped_reaction_id,
            col(artifact.storage_status) != StorageStatus.RETIRED,
            col(revision.status) == ParseStatus.SUCCEEDED,
            col(ingestion.status) == ArtifactIngestionStatus.SUCCEEDED,
        )
    )
    inference = aliased(TransitionStateInference, name="visibility_mapped_inference")
    inference_revision = aliased(ParseRevision, name="visibility_mapped_inference_revision")
    inference_artifact = aliased(ArtifactFile, name="visibility_mapped_inference_artifact")
    inference_ingestion = aliased(
        ArtifactIngestion,
        name="visibility_mapped_inference_ingestion",
    )
    inference_projects = (
        select(col(inference_artifact.project_id).label("project_id"))
        .select_from(inference)
        .join(
            inference_revision,
            col(inference.parse_revision_id) == col(inference_revision.id),
        )
        .join(
            inference_artifact,
            col(inference_revision.artifact_file_id) == col(inference_artifact.id),
        )
        .join(
            inference_ingestion,
            col(inference_ingestion.artifact_file_id) == col(inference_artifact.id),
        )
        .where(
            col(inference.mapped_reaction_id) == mapped_reaction_id,
            col(inference_artifact.storage_status) != StorageStatus.RETIRED,
            col(inference_revision.status) == ParseStatus.SUCCEEDED,
            col(inference_ingestion.status) == ArtifactIngestionStatus.SUCCEEDED,
        )
    )
    return calculation_projects.union(inference_projects)


def _successful_source_project_ids_for_logical_reaction(logical_reaction_id: Any) -> Any:
    """Return all source projects contributing to any mapping of a reaction."""

    mapped_reaction = aliased(MappedReaction, name="visibility_logical_mapped_reaction")
    node = aliased(MappedReactionNode, name="visibility_logical_mapped_node")
    node_geometry = aliased(
        MappedReactionNodeGeometry,
        name="visibility_logical_mapped_node_geometry",
    )
    frame = aliased(CalculationFrame, name="visibility_logical_mapped_frame")
    revision = aliased(ParseRevision, name="visibility_logical_mapped_revision")
    artifact = aliased(ArtifactFile, name="visibility_logical_mapped_artifact")
    ingestion = aliased(ArtifactIngestion, name="visibility_logical_mapped_ingestion")
    calculation_projects = (
        select(col(artifact.project_id).label("project_id"))
        .select_from(mapped_reaction)
        .join(
            node,
            col(node.mapped_reaction_id) == col(mapped_reaction.id),
        )
        .join(
            node_geometry,
            col(node_geometry.mapped_reaction_node_id) == col(node.id),
        )
        .join(frame, col(frame.geometry_id) == col(node_geometry.geometry_id))
        .join(revision, col(frame.parse_revision_id) == col(revision.id))
        .join(artifact, col(revision.artifact_file_id) == col(artifact.id))
        .join(ingestion, col(ingestion.artifact_file_id) == col(artifact.id))
        .where(
            col(mapped_reaction.logical_reaction_id) == logical_reaction_id,
            col(artifact.storage_status) != StorageStatus.RETIRED,
            col(revision.status) == ParseStatus.SUCCEEDED,
            col(ingestion.status) == ArtifactIngestionStatus.SUCCEEDED,
        )
    )
    inference = aliased(TransitionStateInference, name="visibility_logical_inference")
    inference_mapped_reaction = aliased(
        MappedReaction,
        name="visibility_logical_inference_mapped_reaction",
    )
    inference_revision = aliased(ParseRevision, name="visibility_logical_inference_revision")
    inference_artifact = aliased(ArtifactFile, name="visibility_logical_inference_artifact")
    inference_ingestion = aliased(
        ArtifactIngestion,
        name="visibility_logical_inference_ingestion",
    )
    inference_projects = (
        select(col(inference_artifact.project_id).label("project_id"))
        .select_from(inference)
        .join(
            inference_mapped_reaction,
            col(inference.mapped_reaction_id) == col(inference_mapped_reaction.id),
        )
        .join(
            inference_revision,
            col(inference.parse_revision_id) == col(inference_revision.id),
        )
        .join(
            inference_artifact,
            col(inference_revision.artifact_file_id) == col(inference_artifact.id),
        )
        .join(
            inference_ingestion,
            col(inference_ingestion.artifact_file_id) == col(inference_artifact.id),
        )
        .where(
            col(inference_mapped_reaction.logical_reaction_id) == logical_reaction_id,
            col(inference_artifact.storage_status) != StorageStatus.RETIRED,
            col(inference_revision.status) == ParseStatus.SUCCEEDED,
            col(inference_ingestion.status) == ArtifactIngestionStatus.SUCCEEDED,
        )
    )
    return calculation_projects.union(inference_projects)


def _has_source_project_outside(scope: QueryVisibilityScope, source_projects: Any) -> Any:
    if scope.requested_project_id is None:
        return false()
    # Keep each branch correlated to the outer reaction/geometry row. Wrapping
    # the UNION in a derived table breaks SQLAlchemy's correlation and turns
    # the outer mapped_reaction table into an uncorrelated FROM item, which
    # incorrectly marks every row as cross-project.
    branches = getattr(source_projects, "selects", (source_projects,))
    return or_(
        *(
            source.where(
                source.selected_columns.project_id != scope.requested_project_id
            ).exists()
            for source in branches
        )
    )


def _has_multiple_source_projects(source_projects: Any) -> Any:
    project_rows = source_projects.subquery()
    return (
        select(func.count())
        .select_from(project_rows)
        .scalar_subquery()
        > 1
    )


def _source_project_isolation_predicate(
    scope: QueryVisibilityScope,
    source_projects: Any,
) -> Any:
    """Fail closed when a derived object has sources from another project."""

    if scope.unrestricted:
        return true()
    if scope.uses_project_owned_fast_path:
        # Project ownership is now enforced by non-null immutable root
        # columns plus the 0036/0037 relationship triggers.  Repeating the
        # frame -> revision -> ArtifactFile UNION for every result row made a
        # safe project query scale with the whole historical database.
        return true()
    if scope.uses_project_geometry_catalog:
        return ~_has_source_project_outside(scope, source_projects)
    # Project-owned derived rows never participate in the public-artifact
    # fallback.  A caller that has not been granted access to the requested
    # project may still read a public ArtifactFile (the only shareable cache
    # object), but must not use that file to discover formulas, topologies,
    # geometries, or reactions from the project.
    return false()


def _derived_project_owner_is_visible(scope: QueryVisibilityScope, project_id: Any) -> Any:
    """Only project-owned derived roots enter a user-scoped result set.

    ``NULL`` is the durable quarantine marker used by the historical repair.
    It also makes source-less curator rows fail closed for ordinary requests;
    unrestricted maintenance queries can still inspect them.
    """

    if scope.unrestricted:
        return true()
    if scope.requested_project_id is None or not scope.requested_project_permitted:
        return false()
    return project_id == scope.requested_project_id


def _protocol_project_is_visible(
    scope: QueryVisibilityScope,
    protocol_id: Any,
    protocol_project_id: Any,
    artifact_project_id: Any,
) -> Any:
    """Keep optional legacy protocols on the same project as their source.

    Segments without captured source metadata legitimately have no protocol.
    A present protocol, however, is parsed project data and must have the same
    owner as the ArtifactFile that produced the segment.  The owner predicate
    also prevents a public artifact from exposing an unauthorized project's
    protocol metadata.
    """

    if scope.unrestricted:
        return true()
    return or_(
        protocol_id.is_(None),
        and_(
            protocol_project_id == artifact_project_id,
            _derived_project_owner_is_visible(scope, protocol_project_id),
        ),
    )


def _derived_id_is_visible(scope: QueryVisibilityScope, model: Any, id_column: Any) -> Any:
    """Apply ownership to an arbitrary FK/alias without adding an uncorrelated table."""

    if scope.uses_project_owned_fast_path:
        # All callers of this helper either pass a derived root ID column or
        # an FK that is resolved by the model below.  For the former, push the
        # equality directly onto the driving table instead of producing an
        # ``id IN (SELECT ...)`` semi-join.
        project_column = getattr(getattr(id_column, "table", None), "c", None)
        project_column = getattr(project_column, "project_id", None)
        if project_column is not None:
            return project_column == scope.requested_project_id
    return id_column.in_(
        select(col(model.id))
        .where(_derived_project_owner_is_visible(scope, col(model.project_id)))
        .correlate(None)
    )


def _requested_project_owner_is_visible(scope: QueryVisibilityScope, id_column: Any) -> Any:
    """Push the project owner restriction onto the driving derived table.

    The source-isolation predicates below remain the fail-closed defence for
    historical mixed provenance, but they are intentionally expensive: they
    walk frames, revisions, artifacts, and ingestion rows.  A project-scoped
    list/count must first use the indexed ``project_id`` on its driving
    reaction/topology row; otherwise PostgreSQL may evaluate that provenance
    walk for every historical row before discovering that only a handful of
    rows belong to the requested project.

    ``id_column`` may belong to an ORM model or an aliased model.  Reading its
    table's ``project_id`` column keeps the predicate correlated to the caller
    without introducing a second base table into the statement.
    """

    if scope.unrestricted or scope.requested_project_id is None:
        return true()
    table = getattr(id_column, "table", None)
    columns = getattr(table, "c", None)
    project_column = getattr(columns, "project_id", None)
    if project_column is None:
        return true()
    return project_column == scope.requested_project_id


def artifact_id_is_visible(scope: QueryVisibilityScope, artifact_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    return artifact_id.in_(visible_artifact_ids(scope))


def derived_artifact_id_is_visible(scope: QueryVisibilityScope, artifact_id: Any) -> Any:
    """Authorize an ArtifactFile only as the source of project-owned data."""

    if scope.unrestricted:
        return true()
    return artifact_id.in_(
        select(col(ArtifactFile.id)).where(scope.derived_artifact_predicate())
    )


def parse_revision_id_is_visible(scope: QueryVisibilityScope, revision_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    return revision_id.in_(visible_parse_revision_ids(scope))


def _frame_source_is_visible(scope: QueryVisibilityScope, frame_id: Any) -> Any:
    """Authorize one frame through its own source chain.

    The previous implementation expressed this as ``frame_id IN (SELECT all
    visible frames)``. That is correct but forces list/count queries to build a
    large intermediate frame set before applying their own filters. A
    correlated source check lets PostgreSQL use the requested frame/index
    first while keeping the same fail-closed ownership conditions.
    """

    if scope.uses_project_owned_fast_path:
        frame = aliased(CalculationFrame, name="visibility_fast_frame")
        revision = aliased(ParseRevision, name="visibility_fast_revision")
        artifact = aliased(ArtifactFile, name="visibility_fast_artifact")
        ingestion = aliased(ArtifactIngestion, name="visibility_fast_ingestion")
        geometry = aliased(Geometry, name="visibility_fast_geometry")
        derivation = aliased(
            MolecularTopologyDerivation,
            name="visibility_fast_derivation",
        )
        return (
            select(1)
            .select_from(frame)
            .join(revision, col(frame.parse_revision_id) == col(revision.id))
            .join(artifact, col(revision.artifact_file_id) == col(artifact.id))
            .join(ingestion, col(ingestion.artifact_file_id) == col(artifact.id))
            .join(geometry, col(frame.geometry_id) == col(geometry.id))
            .join(derivation, col(frame.topology_derivation_id) == col(derivation.id))
            .where(
                col(frame.id) == frame_id,
                col(artifact.project_id) == scope.requested_project_id,
                col(artifact.storage_status) != StorageStatus.RETIRED,
                col(ingestion.status) == ArtifactIngestionStatus.SUCCEEDED,
                col(revision.status) == ParseStatus.SUCCEEDED,
                col(geometry.project_id) == scope.requested_project_id,
                col(derivation.project_id) == scope.requested_project_id,
                col(frame.geometry_id).is_not(None),
            )
            .exists()
        )

    frame = aliased(CalculationFrame)
    revision = aliased(ParseRevision)
    artifact = aliased(ArtifactFile)
    ingestion = aliased(ArtifactIngestion)
    geometry = aliased(Geometry)
    segment = aliased(CalculationSegment)
    protocol = aliased(CalculationProtocol)
    derivation = aliased(MolecularTopologyDerivation)
    source = (
        select(1)
        .select_from(frame)
        .join(revision, col(frame.parse_revision_id) == col(revision.id))
        .join(segment, col(frame.segment_id) == col(segment.id))
        .join(artifact, col(revision.artifact_file_id) == col(artifact.id))
        .outerjoin(protocol, col(segment.protocol_id) == col(protocol.id))
        .join(ingestion, col(ingestion.artifact_file_id) == col(artifact.id))
        .join(geometry, col(frame.geometry_id) == col(geometry.id))
        .join(derivation, col(frame.topology_derivation_id) == col(derivation.id))
        .where(
            col(frame.id) == frame_id,
            scope.derived_artifact_predicate(artifact),
            col(ingestion.status) == ArtifactIngestionStatus.SUCCEEDED,
            col(revision.status) == ParseStatus.SUCCEEDED,
            _derived_project_owner_is_visible(scope, col(geometry.project_id)),
            col(geometry.project_id) == col(artifact.project_id),
            _protocol_project_is_visible(
                scope,
                col(segment.protocol_id),
                col(protocol.project_id),
                col(artifact.project_id),
            ),
            _derived_project_owner_is_visible(scope, col(derivation.project_id)),
            col(frame.geometry_id).is_not(None),
        )
    )
    return source.exists()


def frame_id_is_visible(scope: QueryVisibilityScope, frame_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    return _frame_source_is_visible(scope, frame_id)


def _geometry_topology_project_boundary_is_valid(geometry_id: Any) -> Any:
    """Reject historical Geometry rows whose topology chain crosses projects."""

    geometry = aliased(Geometry, name="visibility_geometry_boundary")
    topology = aliased(MolecularTopology, name="visibility_geometry_topology_boundary")
    formula = aliased(MolecularFormula, name="visibility_geometry_formula_boundary")
    return (
        select(1)
        .select_from(geometry)
        .join(topology, col(geometry.topology_id) == col(topology.id))
        .join(formula, col(topology.formula_id) == col(formula.id))
        .where(
            col(geometry.id) == geometry_id,
            col(geometry.project_id).is_not(None),
            col(topology.project_id).is_not(None),
            col(formula.project_id).is_not(None),
            col(geometry.project_id) == col(topology.project_id),
            col(topology.project_id) == col(formula.project_id),
        )
        .exists()
    )


def _topology_formula_project_boundary_is_valid(topology_id: Any) -> Any:
    """Reject historical Topology rows whose formula belongs to another project."""

    topology = aliased(MolecularTopology, name="visibility_topology_boundary")
    formula = aliased(MolecularFormula, name="visibility_topology_formula_boundary")
    return (
        select(1)
        .select_from(topology)
        .join(formula, col(topology.formula_id) == col(formula.id))
        .where(
            col(topology.id) == topology_id,
            col(topology.project_id).is_not(None),
            col(formula.project_id).is_not(None),
            col(topology.project_id) == col(formula.project_id),
        )
        .exists()
    )


def geometry_id_is_visible(scope: QueryVisibilityScope, geometry_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    if scope.uses_project_owned_fast_path:
        return and_(
            _requested_project_owner_is_visible(scope, geometry_id),
            _derived_id_is_visible(scope, Geometry, geometry_id),
            _geometry_topology_project_boundary_is_valid(geometry_id),
            _project_geometry_catalog_source_is_visible(scope, geometry_id),
        )
    if scope.uses_project_geometry_catalog:
        return and_(
            _requested_project_owner_is_visible(scope, geometry_id),
            _derived_id_is_visible(scope, Geometry, geometry_id),
            _geometry_topology_project_boundary_is_valid(geometry_id),
            _project_geometry_catalog_source_is_visible(scope, geometry_id),
            _source_project_isolation_predicate(
                scope,
                _successful_source_project_ids_for_geometry(geometry_id),
            ),
        )
    return and_(
        _requested_project_owner_is_visible(scope, geometry_id),
        _derived_id_is_visible(scope, Geometry, geometry_id),
        _geometry_topology_project_boundary_is_valid(geometry_id),
        geometry_id.in_(visible_geometry_ids(scope)),
        _source_project_isolation_predicate(
            scope,
            _successful_source_project_ids_for_geometry(geometry_id),
        ),
    )


def _profile_state_is_visible(
    scope: QueryVisibilityScope,
    profile: Any,
    state_column: Any,
    *,
    alias_name: str,
    required: bool,
) -> Any:
    """Authorize the source evidence embedded in one profile state.

    New projections carry the selected electronic and thermochemistry frame
    IDs. Profiles written before that provenance existed are handled by a
    deliberately narrower legacy fallback: the selected Geometry itself must
    still be visible. A profile with partial new provenance fails closed.
    """

    selections = (
        func.jsonb_array_elements(col(state_column)["topologies"])
        .table_valued("selection")
        .render_derived(name=alias_name)
    )
    selection = selections.c.selection
    geometry_id = sql_cast(
        selection.op("->>")("geometry_id"),
        PostgreSQLUUID(as_uuid=True),
    )
    electronic_source_frame_id = sql_cast(
        selection.op("->>")("electronic_source_frame_id"),
        PostgreSQLUUID(as_uuid=True),
    )
    thermochemistry_source_frame_id = sql_cast(
        selection.op("->>")("thermochemistry_source_frame_id"),
        PostgreSQLUUID(as_uuid=True),
    )
    has_all_source_provenance = and_(
        selection.op("?")("electronic_source_frame_id"),
        selection.op("?")("thermochemistry_source_frame_id"),
    )
    has_any_source_provenance = or_(
        selection.op("?")("electronic_source_frame_id"),
        selection.op("?")("thermochemistry_source_frame_id"),
    )
    source_is_visible = and_(
        has_all_source_provenance,
        electronic_source_frame_id.is_not(None),
        thermochemistry_source_frame_id.is_not(None),
        electronic_source_frame_id.in_(visible_frame_ids(scope)),
        thermochemistry_source_frame_id.in_(visible_frame_ids(scope)),
    )
    legacy_is_visible = and_(
        ~has_any_source_provenance,
        geometry_id.is_not(None),
        geometry_id.in_(visible_geometry_ids(scope)),
    )
    selection_is_visible = or_(source_is_visible, legacy_is_visible)
    hidden_selection_exists = (
        select(1)
        .select_from(selections)
        .where(or_(geometry_id.is_(None), ~selection_is_visible))
        .correlate(profile)
        .exists()
    )
    if not required:
        return ~hidden_selection_exists
    visible_selection_exists = (
        select(1)
        .select_from(selections)
        .where(geometry_id.is_not(None), selection_is_visible)
        .correlate(profile)
        .exists()
    )
    return and_(visible_selection_exists, ~hidden_selection_exists)


def thermodynamic_profile_is_visible(
    scope: QueryVisibilityScope,
    profile: Any = MappedReactionThermodynamicProfile,
) -> Any:
    """Return a fail-closed source authorization predicate for one profile."""

    if scope.unrestricted:
        return true()
    return and_(
        # The profile has no independent project column.  Its parent is the
        # ownership boundary, so keep this predicate safe even when a caller
        # uses it outside a query that already joins a visible MappedReaction.
        col(profile.mapped_reaction_id).in_(
            select(col(MappedReaction.id)).where(
                _derived_project_owner_is_visible(scope, col(MappedReaction.project_id))
            )
        ),
        _profile_state_is_visible(
            scope,
            profile,
            profile.reactants,
            alias_name="thermodynamic_profile_reactants",
            required=True,
        ),
        _profile_state_is_visible(
            scope,
            profile,
            profile.transition_state,
            alias_name="thermodynamic_profile_transition_state",
            required=False,
        ),
        _profile_state_is_visible(
            scope,
            profile,
            profile.products,
            alias_name="thermodynamic_profile_products",
            required=False,
        ),
    )


def topology_derivation_id_is_visible(scope: QueryVisibilityScope, derivation_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    if scope.uses_project_owned_fast_path:
        return _derived_id_is_visible(
            scope,
            MolecularTopologyDerivation,
            derivation_id,
        )
    return derivation_id.in_(
        select(col(CalculationFrame.topology_derivation_id)).where(
            col(CalculationFrame.id).in_(visible_frame_ids(scope)),
            col(CalculationFrame.topology_derivation_id).in_(
                select(col(MolecularTopologyDerivation.id)).where(
                    _derived_project_owner_is_visible(
                        scope,
                        col(MolecularTopologyDerivation.project_id),
                    )
                )
            ),
        )
    )


def topology_id_is_visible(scope: QueryVisibilityScope, topology_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    if scope.uses_project_owned_fast_path:
        # Topology and formula ownership are both immutable and are checked by
        # the 0036 trigger.  A project-owned topology is safe to return even
        # when its source catalogue is still being rebuilt during import.
        return and_(
            _requested_project_owner_is_visible(scope, topology_id),
            _derived_id_is_visible(scope, MolecularTopology, topology_id),
            _topology_formula_project_boundary_is_valid(topology_id),
        )
    if scope.uses_project_geometry_catalog:
        geometry_source = (
            select(1)
            .select_from(Geometry)
            .where(
                col(Geometry.topology_id) == topology_id,
                _derived_project_owner_is_visible(scope, col(Geometry.project_id)),
                _project_geometry_catalog_source_is_visible(scope, col(Geometry.id)),
            )
            .exists()
        )
        endpoint = aliased(TransitionStateEndpoint, name="visibility_project_endpoint")
        endpoint_source = (
            select(1)
            .select_from(endpoint)
            .where(
                col(endpoint.topology_id) == topology_id,
                _frame_source_is_visible(scope, col(endpoint.calculation_frame_id)),
            )
            .exists()
        )
        logical_participant = aliased(
            LogicalReactionParticipant,
            name="visibility_project_logical_participant",
        )
        mapped_participant = aliased(
            MappedReactionParticipant,
            name="visibility_project_mapped_participant",
        )
        logical_participant_source = (
            select(1)
            .select_from(logical_participant)
            .join(
                mapped_participant,
                col(mapped_participant.logical_reaction_participant_id)
                == col(logical_participant.id),
            )
            .where(
                col(logical_participant.topology_id) == topology_id,
                mapped_reaction_id_is_visible(
                    scope,
                    col(mapped_participant.mapped_reaction_id),
                ),
            )
            .exists()
        )
        concrete_participant_source = (
            select(1)
            .select_from(mapped_participant)
            .where(
                col(mapped_participant.concrete_topology_id) == topology_id,
                col(mapped_participant.concrete_topology_id).is_not(None),
                mapped_reaction_id_is_visible(
                    scope,
                    col(mapped_participant.mapped_reaction_id),
                ),
            )
            .exists()
        )
        return and_(
            _requested_project_owner_is_visible(scope, topology_id),
            _derived_id_is_visible(scope, MolecularTopology, topology_id),
            _topology_formula_project_boundary_is_valid(topology_id),
            or_(
                geometry_source,
                endpoint_source,
                logical_participant_source,
                concrete_participant_source,
            ),
            _source_project_isolation_predicate(
                scope,
                _successful_source_project_ids_for_topology(topology_id),
            ),
        )
    geometry_topology_ids = select(col(Geometry.topology_id)).where(
        geometry_id_is_visible(scope, col(Geometry.id))
    )
    endpoint_topology_ids = select(col(TransitionStateEndpoint.topology_id)).where(
        col(TransitionStateEndpoint.calculation_frame_id).in_(visible_frame_ids(scope))
    )
    logical_participant_topology_ids = (
        select(col(LogicalReactionParticipant.topology_id))
        .join(
            MappedReactionParticipant,
            col(MappedReactionParticipant.logical_reaction_participant_id)
            == col(LogicalReactionParticipant.id),
        )
        .where(
            mapped_reaction_id_is_visible(
                scope,
                col(MappedReactionParticipant.mapped_reaction_id),
            )
        )
    )
    concrete_participant_topology_ids = select(
        col(MappedReactionParticipant.concrete_topology_id)
    ).where(
        col(MappedReactionParticipant.concrete_topology_id).is_not(None),
        mapped_reaction_id_is_visible(
            scope,
            col(MappedReactionParticipant.mapped_reaction_id),
        ),
    )
    return and_(
        _requested_project_owner_is_visible(scope, topology_id),
        _derived_id_is_visible(scope, MolecularTopology, topology_id),
        _topology_formula_project_boundary_is_valid(topology_id),
        topology_id.in_(
            geometry_topology_ids.union(
                endpoint_topology_ids,
                logical_participant_topology_ids,
                concrete_participant_topology_ids,
            )
        ),
        _source_project_isolation_predicate(
            scope,
            _successful_source_project_ids_for_topology(topology_id),
        ),
    )


def formula_id_is_visible(scope: QueryVisibilityScope, formula_id: Any) -> Any:
    """Expose a formula only through visible, project-isolated topologies."""

    if scope.unrestricted:
        return true()
    if scope.uses_project_owned_fast_path:
        return formula_id.in_(
            select(col(MolecularFormula.id)).where(
                col(MolecularFormula.project_id) == scope.requested_project_id,
            )
        )
    return formula_id.in_(
        select(col(MolecularTopology.formula_id)).where(
            topology_id_is_visible(scope, col(MolecularTopology.id)),
            _derived_project_owner_is_visible(scope, col(MolecularTopology.project_id)),
            _derived_project_owner_is_visible(scope, col(MolecularFormula.project_id)),
        ).join(
            MolecularFormula,
            col(MolecularFormula.id) == col(MolecularTopology.formula_id),
        )
    )


def _mapped_reaction_ids_with_calculations(
    scope: QueryVisibilityScope,
    geometry_ids: Any,
) -> Any:
    # Resolve source frames in the same statement as the mapped node. This
    # avoids a nested ``geometry_id IN (SELECT frame ...)`` semi-join for every
    # reaction row after the project-ownership migration.
    statement = (
        select(col(MappedReactionNode.mapped_reaction_id))
        .join(
            MappedReactionNodeGeometry,
            col(MappedReactionNodeGeometry.mapped_reaction_node_id)
            == col(MappedReactionNode.id),
        )
        .join(
            Geometry,
            col(MappedReactionNodeGeometry.geometry_id) == col(Geometry.id),
        )
        .join(
            CalculationFrame,
            col(CalculationFrame.geometry_id) == col(Geometry.id),
        )
        .join(
            ParseRevision,
            col(CalculationFrame.parse_revision_id) == col(ParseRevision.id),
        )
        .join(
            CalculationSegment,
            col(CalculationFrame.segment_id) == col(CalculationSegment.id),
        )
        .join(
            ArtifactFile,
            col(ParseRevision.artifact_file_id) == col(ArtifactFile.id),
        )
        .outerjoin(
            CalculationProtocol,
            col(CalculationSegment.protocol_id) == col(CalculationProtocol.id),
        )
        .join(
            ArtifactIngestion,
            col(ArtifactIngestion.artifact_file_id) == col(ArtifactFile.id),
        )
        .join(
            MolecularTopologyDerivation,
            col(CalculationFrame.topology_derivation_id)
            == col(MolecularTopologyDerivation.id),
        )
        .where(
            scope.derived_artifact_predicate(),
            col(ArtifactIngestion.status) == ArtifactIngestionStatus.SUCCEEDED,
            col(ParseRevision.status) == ParseStatus.SUCCEEDED,
            col(Geometry.project_id) == col(ArtifactFile.project_id),
            _protocol_project_is_visible(
                scope,
                col(CalculationSegment.protocol_id),
                col(CalculationProtocol.project_id),
                col(ArtifactFile.project_id),
            ),
            _derived_project_owner_is_visible(scope, col(Geometry.project_id)),
            _derived_project_owner_is_visible(
                scope,
                col(MolecularTopologyDerivation.project_id),
            ),
            geometry_has_thermodynamic_property_predicate(
                col(MappedReactionNodeGeometry.geometry_id)
            ),
        )
    )
    if scope.uses_project_geometry_catalog:
        return statement.join(
            ProjectGeometryCatalog,
            and_(
                col(ProjectGeometryCatalog.project_id) == scope.requested_project_id,
                col(ProjectGeometryCatalog.geometry_id)
                == col(MappedReactionNodeGeometry.geometry_id),
                col(ProjectGeometryCatalog.has_thermodynamic_property),
            ),
        )
    return statement


def _mapped_reaction_ids_with_inferences(revision_ids: Any) -> Any:
    return select(col(TransitionStateInference.mapped_reaction_id)).where(
        col(TransitionStateInference.mapped_reaction_id).is_not(None),
        col(TransitionStateInference.parse_revision_id).in_(revision_ids),
    )


def _mapped_reaction_id_has_any_source(mapped_reaction_id: Any) -> Any:
    """Check source existence for one reaction without building a global ID set."""

    node = aliased(MappedReactionNode)
    node_geometry = aliased(MappedReactionNodeGeometry)
    inference = aliased(TransitionStateInference)
    calculation_source = (
        select(col(node.id))
        .join(node_geometry, col(node_geometry.mapped_reaction_node_id) == col(node.id))
        .where(
            col(node.mapped_reaction_id) == mapped_reaction_id,
            geometry_has_thermodynamic_property_predicate(col(node_geometry.geometry_id)),
        )
        .exists()
    )
    inference_source = (
        select(col(inference.id))
        .where(col(inference.mapped_reaction_id) == mapped_reaction_id)
        .exists()
    )
    return or_(calculation_source, inference_source)


def _mapped_reaction_id_has_visible_source(
    scope: QueryVisibilityScope,
    mapped_reaction_id: Any,
) -> Any:
    if scope.uses_project_geometry_catalog:
        return _mapped_reaction_id_has_project_source(scope, mapped_reaction_id)
    return or_(
        mapped_reaction_id.in_(
            _mapped_reaction_ids_with_calculations(scope, visible_geometry_ids(scope))
        ),
        mapped_reaction_id.in_(
            _mapped_reaction_ids_with_inferences(visible_parse_revision_ids(scope))
        ),
    )


def _mapped_reaction_id_has_project_source(
    scope: QueryVisibilityScope,
    mapped_reaction_id: Any,
) -> Any:
    """Correlated source check for a permitted project.

    Keep the mapping ID correlated all the way through the source tables.  An
    uncorrelated ``id IN (SELECT ...)`` allows PostgreSQL to materialize all
    project mappings and then compare them against every ordered reaction.
    """

    calculation_source = (
        select(1)
        .select_from(MappedReactionNode)
        .join(
            MappedReactionNodeGeometry,
            col(MappedReactionNodeGeometry.mapped_reaction_node_id) == col(MappedReactionNode.id),
        )
        .join(
            ProjectGeometryCatalog,
            and_(
                col(ProjectGeometryCatalog.project_id) == scope.requested_project_id,
                col(ProjectGeometryCatalog.geometry_id)
                == col(MappedReactionNodeGeometry.geometry_id),
                col(ProjectGeometryCatalog.has_thermodynamic_property),
            ),
        )
        .where(
            col(MappedReactionNode.mapped_reaction_id) == mapped_reaction_id,
            _geometry_source_is_visible(
                scope,
                col(MappedReactionNodeGeometry.geometry_id),
            ),
        )
        .exists()
    )
    inference_source = (
        select(1)
        .select_from(TransitionStateInference)
        .join(
            ParseRevision,
            col(ParseRevision.id) == col(TransitionStateInference.parse_revision_id),
        )
        .join(ArtifactFile, col(ArtifactFile.id) == col(ParseRevision.artifact_file_id))
        .join(
            ArtifactIngestion,
            col(ArtifactIngestion.artifact_file_id) == col(ArtifactFile.id),
        )
        .where(
            col(TransitionStateInference.mapped_reaction_id) == mapped_reaction_id,
            col(ArtifactFile.project_id) == scope.requested_project_id,
            col(ArtifactFile.storage_status) != StorageStatus.RETIRED,
            col(ParseRevision.status) == ParseStatus.SUCCEEDED,
            col(ArtifactIngestion.status) == ArtifactIngestionStatus.SUCCEEDED,
        )
        .exists()
    )
    return or_(calculation_source, inference_source)


def _mapped_reaction_source_isolation_predicate(
    scope: QueryVisibilityScope,
    mapped_reaction_id: Any,
) -> Any:
    return _source_project_isolation_predicate(
        scope,
        _successful_source_project_ids_for_mapped_reaction(mapped_reaction_id),
    )


def _logical_reaction_source_isolation_predicate(
    scope: QueryVisibilityScope,
    logical_reaction_id: Any,
) -> Any:
    return _source_project_isolation_predicate(
        scope,
        _successful_source_project_ids_for_logical_reaction(logical_reaction_id),
    )


def _mapped_reaction_parent_source_isolation_predicate(
    scope: QueryVisibilityScope,
    mapped_reaction_id: Any,
) -> Any:
    """Keep a mapped path hidden if its logical parent spans projects."""

    if scope.requested_project_id is None:
        return true()
    mapped_reaction = aliased(MappedReaction, name="visibility_mapped_parent")
    return select(1).select_from(mapped_reaction).where(
        col(mapped_reaction.id) == mapped_reaction_id,
        _logical_reaction_source_isolation_predicate(
            scope,
            col(mapped_reaction.logical_reaction_id),
        ),
    ).exists()


def _logical_reaction_project_boundary_is_valid(logical_reaction_id: Any) -> Any:
    """Reject logical paths with a participant relation from another project."""

    logical = aliased(LogicalReaction, name="visibility_logical_boundary")
    participant = aliased(
        LogicalReactionParticipant,
        name="visibility_logical_boundary_participant",
    )
    topology = aliased(MolecularTopology, name="visibility_logical_boundary_topology")
    formula = aliased(MolecularFormula, name="visibility_logical_boundary_formula")
    invalid_participant = (
        select(1)
        .select_from(participant)
        .join(logical, col(participant.logical_reaction_id) == col(logical.id))
        .join(topology, col(participant.topology_id) == col(topology.id))
        .join(formula, col(topology.formula_id) == col(formula.id))
        .where(
            col(logical.id) == logical_reaction_id,
            or_(
                col(logical.project_id).is_(None),
                col(topology.project_id).is_(None),
                col(formula.project_id).is_(None),
                col(logical.project_id).is_distinct_from(col(topology.project_id)),
                col(topology.project_id).is_distinct_from(col(formula.project_id)),
            ),
        )
        .exists()
    )

    membership = aliased(
        LogicalParticipantConcreteTopology,
        name="visibility_logical_boundary_membership",
    )
    membership_participant = aliased(
        LogicalReactionParticipant,
        name="visibility_logical_boundary_membership_participant",
    )
    membership_logical = aliased(
        LogicalReaction,
        name="visibility_logical_boundary_membership_logical",
    )
    concrete_topology = aliased(
        MolecularTopology,
        name="visibility_logical_boundary_concrete_topology",
    )
    concrete_formula = aliased(
        MolecularFormula,
        name="visibility_logical_boundary_concrete_formula",
    )
    invalid_membership = (
        select(1)
        .select_from(membership)
        .join(
            membership_participant,
            col(membership.logical_reaction_participant_id)
            == col(membership_participant.id),
        )
        .join(
            membership_logical,
            col(membership_participant.logical_reaction_id) == col(membership_logical.id),
        )
        .join(
            concrete_topology,
            col(membership.concrete_topology_id) == col(concrete_topology.id),
        )
        .join(
            concrete_formula,
            col(concrete_topology.formula_id) == col(concrete_formula.id),
        )
        .where(
            col(membership_logical.id) == logical_reaction_id,
            or_(
                col(membership_logical.project_id).is_(None),
                col(concrete_topology.project_id).is_(None),
                col(concrete_formula.project_id).is_(None),
                col(membership_logical.project_id).is_distinct_from(
                    col(concrete_topology.project_id)
                ),
                col(concrete_topology.project_id).is_distinct_from(
                    col(concrete_formula.project_id)
                ),
            ),
        )
        .exists()
    )
    return and_(~invalid_participant, ~invalid_membership)


def _mapped_reaction_project_boundary_is_valid(mapped_reaction_id: Any) -> Any:
    """Reject mapped paths whose participant/node relations cross projects."""

    mapped = aliased(MappedReaction, name="visibility_mapped_boundary")
    logical = aliased(LogicalReaction, name="visibility_mapped_boundary_logical")
    invalid_parent = (
        select(1)
        .select_from(mapped)
        .join(logical, col(mapped.logical_reaction_id) == col(logical.id))
        .where(
            col(mapped.id) == mapped_reaction_id,
            or_(
                col(mapped.project_id).is_(None),
                col(logical.project_id).is_(None),
                col(mapped.project_id).is_distinct_from(col(logical.project_id)),
                ~_logical_reaction_project_boundary_is_valid(col(logical.id)),
            ),
        )
        .exists()
    )

    participant = aliased(
        MappedReactionParticipant,
        name="visibility_mapped_boundary_participant",
    )
    participant_logical = aliased(
        LogicalReaction,
        name="visibility_mapped_boundary_participant_logical",
    )
    logical_participant = aliased(
        LogicalReactionParticipant,
        name="visibility_mapped_boundary_logical_participant",
    )
    abstract_topology = aliased(
        MolecularTopology,
        name="visibility_mapped_boundary_abstract_topology",
    )
    abstract_formula = aliased(
        MolecularFormula,
        name="visibility_mapped_boundary_abstract_formula",
    )
    concrete_topology = aliased(
        MolecularTopology,
        name="visibility_mapped_boundary_concrete_topology",
    )
    concrete_formula = aliased(
        MolecularFormula,
        name="visibility_mapped_boundary_concrete_formula",
    )
    invalid_participant = (
        select(1)
        .select_from(participant)
        .join(mapped, col(participant.mapped_reaction_id) == col(mapped.id))
        .join(
            logical_participant,
            col(participant.logical_reaction_participant_id) == col(logical_participant.id),
        )
        .join(
            participant_logical,
            col(logical_participant.logical_reaction_id) == col(participant_logical.id),
        )
        .join(
            abstract_topology,
            col(logical_participant.topology_id) == col(abstract_topology.id),
        )
        .join(
            abstract_formula,
            col(abstract_topology.formula_id) == col(abstract_formula.id),
        )
        .outerjoin(
            concrete_topology,
            col(participant.concrete_topology_id) == col(concrete_topology.id),
        )
        .outerjoin(
            concrete_formula,
            col(concrete_topology.formula_id) == col(concrete_formula.id),
        )
        .where(
            col(mapped.id) == mapped_reaction_id,
            or_(
                col(mapped.project_id).is_(None),
                col(participant_logical.project_id).is_(None),
                col(mapped.project_id).is_distinct_from(col(participant_logical.project_id)),
                col(participant_logical.id).is_distinct_from(
                    col(mapped.logical_reaction_id)
                ),
                col(abstract_topology.project_id).is_(None),
                col(abstract_formula.project_id).is_(None),
                col(mapped.project_id).is_distinct_from(col(abstract_topology.project_id)),
                col(abstract_topology.project_id).is_distinct_from(
                    col(abstract_formula.project_id)
                ),
                and_(
                    col(participant.concrete_topology_id).is_not(None),
                    or_(
                        col(concrete_topology.project_id).is_(None),
                        col(concrete_formula.project_id).is_(None),
                        col(mapped.project_id).is_distinct_from(
                            col(concrete_topology.project_id)
                        ),
                        col(concrete_topology.project_id).is_distinct_from(
                            col(concrete_formula.project_id)
                        ),
                    ),
                ),
            ),
        )
        .exists()
    )

    node = aliased(MappedReactionNode, name="visibility_mapped_boundary_node")
    node_geometry = aliased(
        MappedReactionNodeGeometry,
        name="visibility_mapped_boundary_node_geometry",
    )
    geometry = aliased(Geometry, name="visibility_mapped_boundary_geometry")
    geometry_topology = aliased(
        MolecularTopology,
        name="visibility_mapped_boundary_geometry_topology",
    )
    geometry_formula = aliased(
        MolecularFormula,
        name="visibility_mapped_boundary_geometry_formula",
    )
    node_participant = aliased(
        MappedReactionParticipant,
        name="visibility_mapped_boundary_node_participant",
    )
    invalid_node_geometry = (
        select(1)
        .select_from(node_geometry)
        .join(node, col(node_geometry.mapped_reaction_node_id) == col(node.id))
        .join(mapped, col(node.mapped_reaction_id) == col(mapped.id))
        .join(geometry, col(node_geometry.geometry_id) == col(geometry.id))
        .join(geometry_topology, col(geometry.topology_id) == col(geometry_topology.id))
        .join(
            geometry_formula,
            col(geometry_topology.formula_id) == col(geometry_formula.id),
        )
        .outerjoin(
            node_participant,
            col(node_geometry.mapped_reaction_participant_id) == col(node_participant.id),
        )
        .where(
            col(mapped.id) == mapped_reaction_id,
            or_(
                col(mapped.project_id).is_(None),
                col(geometry.project_id).is_(None),
                col(mapped.project_id).is_distinct_from(col(geometry.project_id)),
                col(geometry_topology.project_id).is_(None),
                col(geometry_formula.project_id).is_(None),
                col(geometry.project_id).is_distinct_from(col(geometry_topology.project_id)),
                col(geometry_topology.project_id).is_distinct_from(
                    col(geometry_formula.project_id)
                ),
                and_(
                    col(node_geometry.mapped_reaction_participant_id).is_not(None),
                    or_(
                        col(node_participant.id).is_(None),
                        col(node_participant.mapped_reaction_id).is_distinct_from(
                            col(mapped.id)
                        ),
                    ),
                ),
            ),
        )
        .exists()
    )

    edge = aliased(MappedReactionEdge, name="visibility_mapped_boundary_edge")
    source_node = aliased(MappedReactionNode, name="visibility_mapped_boundary_source_node")
    target_node = aliased(MappedReactionNode, name="visibility_mapped_boundary_target_node")
    transition_node = aliased(
        MappedReactionNode,
        name="visibility_mapped_boundary_transition_node",
    )
    invalid_edge = (
        select(1)
        .select_from(edge)
        .join(mapped, col(edge.mapped_reaction_id) == col(mapped.id))
        .outerjoin(source_node, col(edge.source_node_id) == col(source_node.id))
        .outerjoin(target_node, col(edge.target_node_id) == col(target_node.id))
        .outerjoin(
            transition_node,
            col(edge.transition_state_node_id) == col(transition_node.id),
        )
        .where(
            col(mapped.id) == mapped_reaction_id,
            or_(
                col(source_node.id).is_(None),
                col(target_node.id).is_(None),
                col(source_node.mapped_reaction_id).is_distinct_from(col(mapped.id)),
                col(target_node.mapped_reaction_id).is_distinct_from(col(mapped.id)),
                and_(
                    col(edge.transition_state_node_id).is_not(None),
                    or_(
                        col(transition_node.id).is_(None),
                        col(transition_node.mapped_reaction_id).is_distinct_from(
                            col(mapped.id)
                        ),
                    ),
                ),
            ),
        )
        .exists()
    )

    return and_(
        ~invalid_parent,
        ~invalid_participant,
        ~invalid_node_geometry,
        ~invalid_edge,
    )


def _mapped_reaction_parent_is_project_owned(
    scope: QueryVisibilityScope,
    mapped_reaction_id: Any,
) -> Any:
    """Check the cheap, indexed parent ownership boundary for one mapping."""

    mapped = aliased(MappedReaction, name="visibility_fast_mapped_parent")
    logical = aliased(LogicalReaction, name="visibility_fast_logical_parent")
    return (
        select(1)
        .select_from(mapped)
        .join(logical, col(mapped.logical_reaction_id) == col(logical.id))
        .where(
            col(mapped.id) == mapped_reaction_id,
            col(mapped.project_id) == scope.requested_project_id,
            col(logical.project_id) == scope.requested_project_id,
        )
        .exists()
    )


def mapped_reaction_id_is_visible(scope: QueryVisibilityScope, mapped_reaction_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    if scope.uses_project_owned_fast_path:
        # The database trigger makes the complete participant/node graph
        # project-local.  The direct owner and parent checks retain the
        # security boundary without recursively walking every source frame.
        return and_(
            _derived_id_is_visible(scope, MappedReaction, mapped_reaction_id),
            _mapped_reaction_parent_is_project_owned(scope, mapped_reaction_id),
        )
    mapped_owner = _derived_id_is_visible(scope, MappedReaction, mapped_reaction_id)
    project_boundary = _mapped_reaction_project_boundary_is_valid(mapped_reaction_id)
    if scope.uses_project_geometry_catalog:
        any_source = _mapped_reaction_id_has_any_source(mapped_reaction_id)
        return and_(
            _requested_project_owner_is_visible(scope, mapped_reaction_id),
            mapped_owner,
            project_boundary,
            _mapped_reaction_source_isolation_predicate(scope, mapped_reaction_id),
            _mapped_reaction_parent_source_isolation_predicate(scope, mapped_reaction_id),
            or_(
                _mapped_reaction_id_has_project_source(scope, mapped_reaction_id),
                ~any_source,
            ),
        )
    visible_source = _mapped_reaction_id_has_visible_source(scope, mapped_reaction_id)
    source_isolation = _mapped_reaction_source_isolation_predicate(scope, mapped_reaction_id)
    parent_isolation = _mapped_reaction_parent_source_isolation_predicate(
        scope,
        mapped_reaction_id,
    )
    owner_restriction = _requested_project_owner_is_visible(scope, mapped_reaction_id)
    if scope.requested_project_id is None:
        return and_(mapped_owner, visible_source)
    if not scope.is_authenticated:
        return and_(
            owner_restriction,
            mapped_owner,
            visible_source,
            source_isolation,
            parent_isolation,
        )

    any_source = _mapped_reaction_id_has_any_source(mapped_reaction_id)
    return and_(
        owner_restriction,
        mapped_owner,
        project_boundary,
        source_isolation,
        parent_isolation,
        or_(visible_source, and_(mapped_reaction_id.is_not(None), ~any_source)),
    )


def logical_reaction_id_is_visible(scope: QueryVisibilityScope, logical_reaction_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    if scope.uses_project_owned_fast_path:
        mapped_reaction = aliased(
            MappedReaction,
            name="visibility_fast_logical_mapping",
        )
        return and_(
            _derived_id_is_visible(scope, LogicalReaction, logical_reaction_id),
            select(1)
            .select_from(mapped_reaction)
            .where(
                col(mapped_reaction.logical_reaction_id) == logical_reaction_id,
                col(mapped_reaction.project_id) == scope.requested_project_id,
            )
            .exists(),
        )
    mapped_reaction = aliased(MappedReaction)
    # Correlating on the indexed logical_reaction_id lets PostgreSQL stop at
    # the first visible mapping.  The previous ``IN (SELECT ...)`` shape was
    # planned as a nested semi-join against every reaction when an ORDER BY
    # used the reaction_class index.
    return and_(
        _requested_project_owner_is_visible(scope, logical_reaction_id),
        _derived_id_is_visible(scope, LogicalReaction, logical_reaction_id),
        _logical_reaction_project_boundary_is_valid(logical_reaction_id),
        _logical_reaction_source_isolation_predicate(scope, logical_reaction_id),
        select(1)
        .select_from(mapped_reaction)
        .where(
            col(mapped_reaction.logical_reaction_id) == logical_reaction_id,
            mapped_reaction_id_is_visible(scope, col(mapped_reaction.id)),
        )
        .exists(),
    )


def workflow_manifest_id_is_visible(scope: QueryVisibilityScope, manifest_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    return manifest_id.in_(
        select(col(WorkflowManifest.id)).where(
            derived_artifact_id_is_visible(scope, col(WorkflowManifest.artifact_file_id))
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
            ),
            # A binding is project-owned metadata, even when its target file
            # is a reusable raw-object cache entry.  Do not return the
            # binding's role/hash/path while merely hiding a foreign file ID.
            or_(
                col(ManifestArtifactBinding.artifact_file_id).is_(None),
                derived_artifact_id_is_visible(
                    scope,
                    col(ManifestArtifactBinding.artifact_file_id),
                ),
            ),
        )
    )


def calculation_segment_id_is_visible(scope: QueryVisibilityScope, segment_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    return segment_id.in_(
        select(col(CalculationSegment.id))
        .select_from(CalculationSegment)
        .join(
            ParseRevision,
            col(ParseRevision.id) == col(CalculationSegment.parse_revision_id),
        )
        .join(
            ArtifactFile,
            col(ArtifactFile.id) == col(ParseRevision.artifact_file_id),
        )
        .outerjoin(
            CalculationProtocol,
            col(CalculationSegment.protocol_id) == col(CalculationProtocol.id),
        )
        .where(
            parse_revision_id_is_visible(scope, col(CalculationSegment.parse_revision_id)),
            _protocol_project_is_visible(
                scope,
                col(CalculationSegment.protocol_id),
                col(CalculationProtocol.project_id),
                col(ArtifactFile.project_id),
            ),
        )
    )


def calculation_protocol_id_is_visible(scope: QueryVisibilityScope, protocol_id: Any) -> Any:
    if scope.unrestricted:
        return true()
    return protocol_id.in_(
        select(col(CalculationProtocol.id))
        .select_from(CalculationProtocol)
        .join(
            CalculationSegment,
            col(CalculationSegment.protocol_id) == col(CalculationProtocol.id),
        )
        .join(
            ParseRevision,
            col(ParseRevision.id) == col(CalculationSegment.parse_revision_id),
        )
        .join(
            ArtifactFile,
            col(ArtifactFile.id) == col(ParseRevision.artifact_file_id),
        )
        .where(
            col(CalculationSegment.protocol_id).is_not(None),
            parse_revision_id_is_visible(scope, col(CalculationSegment.parse_revision_id)),
            col(CalculationProtocol.project_id) == col(ArtifactFile.project_id),
            _derived_project_owner_is_visible(
                scope,
                col(CalculationProtocol.project_id),
            ),
        )
    )


__all__ = [
    "QueryVisibilityScope",
    "artifact_id_is_visible",
    "derived_artifact_id_is_visible",
    "calculation_frame_is_visible",
    "calculation_protocol_id_is_visible",
    "calculation_segment_id_is_visible",
    "frame_id_is_visible",
    "formula_id_is_visible",
    "geometry_id_is_visible",
    "logical_reaction_id_is_visible",
    "manifest_binding_id_is_visible",
    "mapped_reaction_id_is_visible",
    "parse_revision_id_is_visible",
    "query_visibility_scope",
    "thermodynamic_profile_is_visible",
    "topology_derivation_id_is_visible",
    "topology_id_is_visible",
    "visible_artifact_ids",
    "visible_frame_ids",
    "visible_geometry_ids",
    "visible_parse_revision_ids",
    "workflow_manifest_id_is_visible",
]
