"""Best-effort PostgreSQL statistics refreshes after bulk project changes.

PostgreSQL statistics are maintained per table, not per project.  A project
that is deleted and re-imported can therefore remain badly estimated even
when its row count is large enough to matter, but too small to cross the
table-wide automatic-analyze threshold.  The application knows the end of a
project mutation, so it refreshes the small set of project-facing tables at
that boundary instead of waiting for autovacuum's threshold.

The refresh is deliberately best-effort and post-commit.  A successful
upload, reparse, or project deletion must not be turned into a failed request
because a concurrent DDL operation temporarily prevented ANALYZE.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from contextlib import suppress
from time import perf_counter
from uuid import UUID

from sqlalchemy import text

from tricycle_reaction_db.db.session import engine

logger = logging.getLogger(__name__)

# These are the project-scoped catalogue, queue, parse, frame, geometry, and
# reaction columns used by the high-volume read paths.  Keep this allow-list
# explicit: a project-scoped refresh should not scan every unrelated table or
# every wide scientific column in the database.  ``ANALYZE table (columns)``
# still samples the relation, but avoids the 88-second full-width geometry
# refresh observed on the production-sized catalogue.  ``project_ids``
# identifies the completed mutation for coalescing and observability, not a
# SQL filter; PostgreSQL statistics are not maintained per project.
PROJECT_STATISTICS_ANALYZE_TARGETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("public.artifact_file", ("project_id", "artifact_kind", "storage_status")),
    ("public.artifact_ingestion", ("artifact_file_id", "status")),
    ("public.upload_batch", ("project_id", "status")),
    (
        "public.upload_batch_item",
        ("batch_id", "artifact_file_id", "status", "parse_status", "materialization_status"),
    ),
    ("public.parse_revision", ("artifact_file_id", "status", "revision_number")),
    ("public.calculation_protocol", ("project_id",)),
    ("public.calculation_segment", ("parse_revision_id", "protocol_id")),
    ("public.calculation_frame", ("parse_revision_id", "segment_id", "geometry_id", "frame_role")),
    ("public.geometry", ("project_id", "topology_id")),
    (
        "public.project_geometry_catalog",
        (
            "project_id",
            "geometry_id",
            "has_thermodynamic_property",
            "has_frequency_data",
            "has_imaginary_frequency",
        ),
    ),
    ("public.project_geometry_catalog_count", ("project_id",)),
    ("public.logical_reaction", ("project_id",)),
    ("public.mapped_reaction", ("project_id", "logical_reaction_id")),
    (
        "public.transition_state_inference",
        (
            "artifact_ingestion_id",
            "parse_revision_id",
            "status",
            "logical_reaction_id",
            "mapped_reaction_id",
        ),
    ),
    (
        "public.mapped_reaction_thermodynamic_profile",
        ("mapped_reaction_id", "source_visibility_status"),
    ),
)

PROJECT_STATISTICS_TABLES: tuple[str, ...] = tuple(
    table for table, _columns in PROJECT_STATISTICS_ANALYZE_TARGETS
)

_ANALYZE_STATEMENT_TIMEOUT = "10min"


def _normalized_project_ids(project_ids: Collection[UUID] | None) -> tuple[UUID, ...]:
    return tuple(sorted(set(project_ids or ()), key=str))


async def _refresh_tables(
    targets: Collection[tuple[str, tuple[str, ...]]],
    *,
    project_ids: Collection[UUID] | None,
    reason: str,
) -> bool:
    normalized_project_ids = _normalized_project_ids(project_ids)
    started_at = perf_counter()
    try:
        async with engine.connect() as connection:
            try:
                # The normal application connection has a short statement
                # timeout.  ANALYZE is maintenance work and gets its own
                # bounded transaction-local budget without changing the
                # connection pool default.
                await connection.execute(
                    text(f"SET LOCAL statement_timeout = '{_ANALYZE_STATEMENT_TIMEOUT}'")
                )
                for table, columns in targets:
                    column_list = ", ".join(columns)
                    await connection.execute(text(f"ANALYZE {table} ({column_list})"))
                await connection.commit()
            except BaseException:
                with suppress(Exception):
                    await connection.rollback()
                raise
    except Exception:
        logger.exception(
            "targeted database statistics refresh failed reason=%s projects=%s tables=%d",
            reason,
            ",".join(str(project_id) for project_id in normalized_project_ids) or "all",
            len(targets),
        )
        return False

    logger.info(
        "targeted database statistics refreshed reason=%s projects=%s tables=%d elapsed_ms=%.1f",
        reason,
        ",".join(str(project_id) for project_id in normalized_project_ids) or "all",
        len(targets),
        (perf_counter() - started_at) * 1000,
    )
    return True


async def refresh_project_statistics(
    project_ids: Collection[UUID],
    *,
    reason: str,
) -> bool:
    """Refresh query statistics after one or more project mutations."""

    if not project_ids:
        return True
    return await _refresh_tables(
        PROJECT_STATISTICS_ANALYZE_TARGETS,
        project_ids=project_ids,
        reason=reason,
    )


async def refresh_database_statistics(*, reason: str) -> bool:
    """Refresh the project-facing statistics after a global derived reset."""

    return await _refresh_tables(
        PROJECT_STATISTICS_ANALYZE_TARGETS,
        project_ids=None,
        reason=reason,
    )


__all__ = [
    "PROJECT_STATISTICS_ANALYZE_TARGETS",
    "PROJECT_STATISTICS_TABLES",
    "refresh_database_statistics",
    "refresh_project_statistics",
]
