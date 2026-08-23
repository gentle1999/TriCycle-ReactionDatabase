"""Independent NexusX demo-style ASGI applications for each transport."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from nexusx import (  # type: ignore[import-untyped]
    ComposedErManager,
    ErManager,
    create_use_case_voyager,
)
from pydantic import BaseModel
from sqlmodel import SQLModel

from tricycle_reaction_db import __version__
from tricycle_reaction_db.api import core
from tricycle_reaction_db.api.authentication import AuthenticationMiddleware
from tricycle_reaction_db.api.mcp import mcp_dedicated_app
from tricycle_reaction_db.api.nexusx import (
    config,
    paginated_config,
    paginated_schema,
    playground_config,
    playground_schema,
)
from tricycle_reaction_db.api.nexusx import (
    router as use_case_router,
)
from tricycle_reaction_db.api.proxy import ForwardedPrefixMiddleware
from tricycle_reaction_db.api.query_guards import install_query_guards
from tricycle_reaction_db.api.routes.graphql import (
    DIRECT_PLAYGROUND_QUERY,
    PAGINATED_QUERY,
    create_graphql_router,
)
from tricycle_reaction_db.api.routes.health import router as health_router
from tricycle_reaction_db.application import dtos as application_dtos
from tricycle_reaction_db.application.rate_limits import close_rate_limit_clients
from tricycle_reaction_db.application.services.artifact_uploads import (
    close_molop_process_pool,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db import models as db_models
from tricycle_reaction_db.db.session import dispose_engine, session_factory

_VOYAGER_COMPOSITE_RELATIONSHIP_MODELS = {
    "CalculationSegment",
    "ManifestArtifactBinding",
    "MappedReactionEdge",
    "WorkflowManifest",
}
settings = get_settings()


@asynccontextmanager
async def database_lifespan(_: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await close_molop_process_pool()
        await close_rate_limit_clients()
        await dispose_engine()


def _application(title: str, description: str) -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=title,
        description=description,
        version=__version__,
        debug=get_settings().debug,
        lifespan=database_lifespan,
    )
    install_query_guards(app)
    app.add_middleware(ForwardedPrefixMiddleware)
    app.add_middleware(AuthenticationMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.include_router(health_router)
    return app


graphql_playground_app = _application(
    f"{settings.app_name} Direct-list GraphQL",
    "Direct-list NexusX UseCase GraphQL browser",
)
graphql_playground_app.include_router(
    create_graphql_router(
        playground_config,
        playground_schema,
        graphiql_title=f"Direct-list GraphQL - {settings.brand_name}",
        default_query=DIRECT_PLAYGROUND_QUERY,
    )
)


@graphql_playground_app.get("/", tags=["Service"])
async def graphql_playground_info() -> dict[str, str]:
    return {
        "service": "Direct-list GraphQL",
        "graphiql": "/graphql",
        "schema": "/graphql/schema",
    }


core_api_app = _application(
    f"{settings.app_name} Core API",
    "NexusX DefineSubset, ErManager, Resolver, and explicit REST DTOs",
)
core_api_app.include_router(core.router)


@core_api_app.get("/", tags=["Service"])
async def core_api_root() -> dict[str, Any]:
    return core.core_api_info()


paginated_graphql_app = _application(
    f"{settings.app_name} Paginated GraphQL",
    "NexusX UseCase GraphQL with explicit page DTOs",
)
paginated_graphql_app.include_router(
    create_graphql_router(
        paginated_config,
        paginated_schema,
        graphiql_title=f"Paginated GraphQL - {settings.brand_name}",
        default_query=PAGINATED_QUERY,
    )
)


@paginated_graphql_app.get("/", tags=["Service"])
async def paginated_graphql_info() -> dict[str, str]:
    return {
        "service": "Paginated GraphQL",
        "graphiql": "/graphql",
        "schema": "/graphql/schema",
        "pagination": "list methods return items + page",
    }


use_case_rest_app = _application(
    f"{settings.app_name} UseCase REST",
    "NexusX generated FastAPI routes over the canonical UseCase services",
)
use_case_rest_app.include_router(use_case_router)


@use_case_rest_app.get("/", tags=["Service"])
async def use_case_rest_info() -> dict[str, Any]:
    return {
        "service": "UseCase FastAPI",
        "docs": "/docs",
        "services": [service.__name__ for service in config.services],
    }


def database_entities(*, voyager_compatible: bool = False) -> list[type[SQLModel]]:
    """Return exported SQLModel table classes, optionally omitting composite relationships."""

    entities: list[type[SQLModel]] = []
    for export_name in db_models.__all__:
        exported = getattr(db_models, export_name, None)
        if (
            isinstance(exported, type)
            and issubclass(exported, SQLModel)
            and getattr(exported, "__table__", None) is not None
            and not (
                voyager_compatible and exported.__name__ in _VOYAGER_COMPOSITE_RELATIONSHIP_MODELS
            )
        ):
            entities.append(exported)
    return sorted(set(entities), key=lambda entity: entity.__name__)


def database_dtos() -> list[type[BaseModel]]:
    """Return application DTOs owned by the database member in Voyager."""

    dto_classes: list[type[BaseModel]] = []
    for export_name in application_dtos.__all__:
        exported = getattr(application_dtos, export_name, None)
        if isinstance(exported, type) and issubclass(exported, BaseModel):
            dto_classes.append(exported)
    return sorted(set(dto_classes), key=lambda dto: (dto.__module__, dto.__name__))


voyager_database_er_manager = ErManager(
    entities=database_entities(voyager_compatible=True),
    session_factory=session_factory,
    service_name=settings.nexusx_database_cluster_name,
    color=settings.nexusx_database_cluster_color,
    dto_classes=database_dtos(),
)
voyager_er_manager = ComposedErManager(members=[voyager_database_er_manager])
voyager_subapp = create_use_case_voyager(
    services=list(config.services),
    # NexusX 6.1.2 supports LoaderRegistry-compatible composed managers at
    # runtime, while this public parameter is still annotated as ErManager.
    er_manager=cast(Any, voyager_er_manager),
    name=settings.app_name,
    initial_page_policy="first",
    version=__version__,
)
voyager_app = _application(
    f"{settings.app_name} Voyager",
    "NexusX UseCase and SQLModel entity visualization",
)
voyager_app.mount("/voyager", voyager_subapp, name="voyager")


@voyager_app.get("/", tags=["Service"])
async def voyager_info() -> dict[str, str | int]:
    return {
        "service": "Voyager visualization",
        "voyager": "/voyager",
        "entity_count": len(voyager_er_manager.get_all_entities()),
        "composite_relationship_models_omitted": len(_VOYAGER_COMPOSITE_RELATIONSHIP_MODELS),
    }


mcp_app = mcp_dedicated_app

__all__ = [
    "core_api_app",
    "database_dtos",
    "database_entities",
    "graphql_playground_app",
    "mcp_app",
    "paginated_graphql_app",
    "use_case_rest_app",
    "voyager_app",
    "voyager_database_er_manager",
    "voyager_er_manager",
]
