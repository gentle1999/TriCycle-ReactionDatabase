from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from tricycle_reaction_db import __version__
from tricycle_reaction_db.api.apps import voyager_subapp
from tricycle_reaction_db.api.authentication import (
    AuthenticationMiddleware,
    get_authenticated_principal,
    get_optional_principal,
)
from tricycle_reaction_db.api.core import router as core_router
from tricycle_reaction_db.api.mcp import mcp_http_app
from tricycle_reaction_db.api.nexusx import (
    playground_config,
    playground_schema,
)
from tricycle_reaction_db.api.nexusx import (
    router as nexusx_router,
)
from tricycle_reaction_db.api.proxy import ForwardedPrefixMiddleware
from tricycle_reaction_db.api.query_guards import install_query_guards
from tricycle_reaction_db.api.routes import (
    auth_router,
    depiction_router,
    graphql_router,
    health_router,
    metrics_router,
    organization_router,
    project_router,
    upload_batch_router,
    upload_router,
    user_router,
)
from tricycle_reaction_db.api.routes.graphql import DIRECT_PLAYGROUND_QUERY, create_graphql_router
from tricycle_reaction_db.application.rate_limits import close_rate_limit_clients
from tricycle_reaction_db.application.services.artifact_uploads import (
    close_molop_process_pool,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.session import dispose_engine


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    try:
        async with mcp_http_app.lifespan(mcp_http_app):
            yield
    finally:
        await close_molop_process_pool()
        await close_rate_limit_clients()
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title=settings.app_name,
        version=__version__,
        debug=settings.debug,
        lifespan=lifespan,
    )
    install_query_guards(application)
    application.add_middleware(ForwardedPrefixMiddleware)
    application.add_middleware(AuthenticationMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", settings.csrf_header_name],
        allow_credentials=True,
    )
    application.include_router(health_router)
    application.include_router(metrics_router)
    application.include_router(auth_router)
    application.include_router(project_router)
    application.include_router(organization_router)
    application.include_router(user_router)
    application.include_router(core_router)
    application.include_router(upload_router)
    application.include_router(upload_batch_router)
    authenticated = [Depends(get_authenticated_principal)]
    optional_principal = [Depends(get_optional_principal)]
    application.include_router(nexusx_router, dependencies=authenticated)
    application.include_router(
        create_graphql_router(
            playground_config,
            playground_schema,
            prefix="/graphql-playground",
            graphiql_prefix="/graphql",
            graphiql_title=f"Direct-list GraphQL - {settings.brand_name}",
            default_query=DIRECT_PLAYGROUND_QUERY,
        ),
        dependencies=authenticated,
    )
    application.include_router(graphql_router, dependencies=authenticated)
    application.include_router(depiction_router, dependencies=optional_principal)
    application.mount("/mcp", mcp_http_app, name="mcp")
    application.mount("/voyager", voyager_subapp, name="voyager")
    return application


app = create_app()
