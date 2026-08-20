"""FastAPI route modules."""

from tricycle_reaction_db.api.routes.auth import router as auth_router
from tricycle_reaction_db.api.routes.depictions import router as depiction_router
from tricycle_reaction_db.api.routes.graphql import router as graphql_router
from tricycle_reaction_db.api.routes.health import router as health_router
from tricycle_reaction_db.api.routes.metrics import router as metrics_router
from tricycle_reaction_db.api.routes.organizations import router as organization_router
from tricycle_reaction_db.api.routes.projects import router as project_router
from tricycle_reaction_db.api.routes.upload_batches import router as upload_batch_router
from tricycle_reaction_db.api.routes.uploads import router as upload_router
from tricycle_reaction_db.api.routes.users import router as user_router

__all__ = [
    "auth_router",
    "depiction_router",
    "graphql_router",
    "health_router",
    "metrics_router",
    "project_router",
    "organization_router",
    "upload_router",
    "upload_batch_router",
    "user_router",
]
