"""Construct application configurations with explicit functional domains."""

from pathlib import Path
from typing import Any

from tricycle_reaction_db.core.config import (
    ApplicationSettings,
    IdentityEmailSettings,
    IngestionWorkerSettings,
    McpExportSettings,
    RuntimeSettings,
    StorageClientSettings,
)


def application_settings(
    *,
    _env_file: Path | str | None = None,
    runtime: dict[str, Any] | None = None,
    mcp_export: dict[str, Any] | None = None,
    ingestion: dict[str, Any] | None = None,
    identity: dict[str, Any] | None = None,
    storage: dict[str, Any] | None = None,
) -> ApplicationSettings:
    return ApplicationSettings(
        runtime=RuntimeSettings(_env_file=_env_file, **(runtime or {})),
        mcp_export=McpExportSettings(_env_file=_env_file, **(mcp_export or {})),
        ingestion=IngestionWorkerSettings(_env_file=_env_file, **(ingestion or {})),
        identity=IdentityEmailSettings(_env_file=_env_file, **(identity or {})),
        storage=StorageClientSettings(_env_file=_env_file, **(storage or {})),
    )


def copied_application_settings(
    settings: ApplicationSettings | None = None,
    **domains: dict[str, Any],
) -> ApplicationSettings:
    """Apply domain-specific test overrides without revalidating intentional test states."""

    base = settings or application_settings()
    return base.model_copy(
        update={
            domain: getattr(base, domain).model_copy(update=updates)
            for domain, updates in domains.items()
        }
    )
