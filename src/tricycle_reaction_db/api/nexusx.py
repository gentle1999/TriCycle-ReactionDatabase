from fastapi import Depends
from nexusx import (  # type: ignore[import-untyped]
    UseCaseAppConfig,
    build_compose_schema,
    create_use_case_router,
)

from tricycle_reaction_db.api.query_guards import (
    project_scoped_use_case_methods,
    require_project_query_scope,
)
from tricycle_reaction_db.application.services import (
    ArtifactIngestionQueryService,
    ArtifactQueryService,
    CalculationProtocolQueryService,
    CalculationQueryService,
    CalculationResultQueryService,
    CalculationSegmentQueryService,
    GeometryQueryService,
    GraphQLCatalogService,
    LogicalReactionQueryService,
    MappedReactionQueryService,
    MolecularFormulaDetailQueryService,
    MolecularFormulaQueryService,
    MolecularTopologyDerivationQueryService,
    MolecularTopologyDetailQueryService,
    MolecularTopologyQueryService,
    ParseRevisionQueryService,
    ReactionCommandService,
    ReactionEnergyQueryService,
    ScientificArrayQueryService,
    StorageGarbageCollectionQueryService,
    SystemService,
    TransitionStateInferenceQueryService,
    WorkflowManifestQueryService,
)
from tricycle_reaction_db.core.config import get_settings

settings = get_settings()

config = UseCaseAppConfig(
    name=settings.nexusx_app_name,
    description="Topology-first cycloaddition reaction calculation database",
    services=[
        SystemService,
        ArtifactQueryService,
        ArtifactIngestionQueryService,
        MolecularFormulaQueryService,
        MolecularFormulaDetailQueryService,
        MolecularTopologyQueryService,
        MolecularTopologyDetailQueryService,
        MolecularTopologyDerivationQueryService,
        GeometryQueryService,
        CalculationProtocolQueryService,
        CalculationSegmentQueryService,
        ParseRevisionQueryService,
        TransitionStateInferenceQueryService,
        ScientificArrayQueryService,
        StorageGarbageCollectionQueryService,
        ReactionEnergyQueryService,
        LogicalReactionQueryService,
        MappedReactionQueryService,
        CalculationQueryService,
        CalculationResultQueryService,
        WorkflowManifestQueryService,
        ReactionCommandService,
    ],
    enable_mutation=True,
)

playground_config = UseCaseAppConfig(
    name=settings.nexusx_playground_name,
    description="Small read-only GraphQL browser with direct-list results",
    services=[
        SystemService,
        GraphQLCatalogService,
    ],
    enable_mutation=False,
)

paginated_config = config

_project_scoped_methods = project_scoped_use_case_methods(config)
_project_scoped_route_options = {
    f"{service_name}.{method_name}": {
        "dependencies": [Depends(require_project_query_scope)],
    }
    for service_name, method_names in _project_scoped_methods.items()
    for method_name in method_names
}

router = create_use_case_router(
    config,
    prefix="/api",
    route_options=_project_scoped_route_options,
)
schema = build_compose_schema(config)
playground_schema = build_compose_schema(playground_config)
paginated_schema = schema

__all__ = [
    "config",
    "paginated_config",
    "paginated_schema",
    "playground_config",
    "playground_schema",
    "router",
    "schema",
]
