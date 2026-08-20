from nexusx import UseCaseService, query  # type: ignore[import-untyped]

from tricycle_reaction_db import __version__
from tricycle_reaction_db.application.dtos import SystemInfo
from tricycle_reaction_db.core.config import get_settings


class SystemService(UseCaseService):  # type: ignore[misc]
    @query  # type: ignore[untyped-decorator]
    async def info(cls) -> SystemInfo:
        """Return non-sensitive service metadata."""
        settings = get_settings()
        return SystemInfo(
            name=settings.app_name,
            version=__version__,
            environment=settings.environment,
        )
