from nexusx import (  # type: ignore[import-untyped]
    UseCaseAppConfig,
    build_compose_schema,
    create_use_case_router,
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

router = create_use_case_router(config, prefix="/api")
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
