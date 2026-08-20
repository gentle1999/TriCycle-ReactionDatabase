"""Prometheus endpoint for private monitoring networks."""

from typing import Any, cast

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from tricycle_reaction_db.core.observability import (
    ARTIFACT_INGESTION_ROWS,
    ARTIFACT_STORAGE_ROWS,
    DATABASE_POOL_CONNECTIONS,
    METRICS_COLLECTION_FAILURES,
)
from tricycle_reaction_db.db.session import engine, session_factory
from tricycle_reaction_db.domain.enums import ArtifactIngestionStatus, StorageStatus

router = APIRouter(prefix="/internal", include_in_schema=False)


def _refresh_pool_metrics() -> None:
    pool: Any = engine.sync_engine.pool
    for state, method_name in (
        ("checked_in", "checkedin"),
        ("checked_out", "checkedout"),
        ("overflow", "overflow"),
        ("size", "size"),
    ):
        method = getattr(pool, method_name, None)
        if callable(method):
            value = cast(int, method())
            DATABASE_POOL_CONNECTIONS.labels(state=state).set(float(max(0, value)))


async def _refresh_database_row_metrics() -> None:
    for storage_status in StorageStatus:
        ARTIFACT_STORAGE_ROWS.labels(status=storage_status.value).set(0)
    for ingestion_status in ArtifactIngestionStatus:
        ARTIFACT_INGESTION_ROWS.labels(status=ingestion_status.value).set(0)

    try:
        async with session_factory() as session:
            storage_rows = await session.execute(
                text("SELECT storage_status, COUNT(*) FROM artifact_file GROUP BY storage_status")
            )
            ingestion_rows = await session.execute(
                text("SELECT status, COUNT(*) FROM artifact_ingestion GROUP BY status")
            )
    except SQLAlchemyError:
        METRICS_COLLECTION_FAILURES.labels(component="database").inc()
        return

    for storage_status, count in storage_rows:
        ARTIFACT_STORAGE_ROWS.labels(status=str(storage_status)).set(float(count))
    for ingestion_status, count in ingestion_rows:
        ARTIFACT_INGESTION_ROWS.labels(status=str(ingestion_status)).set(float(count))


@router.get("/metrics")
async def prometheus_metrics() -> Response:
    _refresh_pool_metrics()
    await _refresh_database_row_metrics()
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


__all__ = ["router"]
