import logging
from collections.abc import AsyncIterator
from time import perf_counter
from typing import Any, TypedDict

from sqlalchemy import event, text
from sqlalchemy.engine import ExceptionContext
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from tricycle_reaction_db.application.query_cost import QueryStatementTimeout
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.core.observability import STATEMENT_TIMEOUTS


class DatabaseStatus(TypedDict):
    database: str
    postgresql_version: str
    rdkit_extension_version: str


settings = get_settings()
logger = logging.getLogger(__name__)
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    connect_args={"options": f"-c statement_timeout={settings.query_statement_timeout_ms}"},
)
session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "before_cursor_execute")
def _record_query_start(
    _connection: Any,
    _cursor: Any,
    _statement: str,
    _parameters: Any,
    context: Any,
    _executemany: bool,
) -> None:
    context._tricycle_query_started_at = perf_counter()


@event.listens_for(engine.sync_engine, "after_cursor_execute")
def _log_slow_query(
    _connection: Any,
    _cursor: Any,
    statement: str,
    _parameters: Any,
    context: Any,
    _executemany: bool,
) -> None:
    started_at = getattr(context, "_tricycle_query_started_at", None)
    if started_at is None:
        return
    elapsed_ms = (perf_counter() - float(started_at)) * 1000
    if elapsed_ms >= settings.slow_query_threshold_ms:
        statement_preview = " ".join(statement.split())
        if len(statement_preview) > 1_000:
            statement_preview = f"{statement_preview[:997]}..."
        logger.warning(
            "slow database query elapsed_ms=%s statement=%s",
            round(elapsed_ms, 3),
            statement_preview,
            extra={
                "query_elapsed_ms": round(elapsed_ms, 3),
                "query_statement": statement,
            },
        )


@event.listens_for(engine.sync_engine, "handle_error", retval=True)
def _map_statement_timeout(context: ExceptionContext) -> BaseException | None:
    if getattr(context.original_exception, "sqlstate", None) == "57014":
        STATEMENT_TIMEOUTS.inc()
        return QueryStatementTimeout()
    return None


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


async def get_database_status() -> DatabaseStatus:
    statement = text(
        """
        SELECT
            current_database() AS database,
            current_setting('server_version') AS postgresql_version,
            COALESCE(
                (SELECT extversion FROM pg_extension WHERE extname = 'rdkit'),
                ''
            ) AS rdkit_extension_version
        """
    )
    async with engine.connect() as connection:
        row = (await connection.execute(statement)).mappings().one()
    return DatabaseStatus(
        database=str(row["database"]),
        postgresql_version=str(row["postgresql_version"]),
        rdkit_extension_version=str(row["rdkit_extension_version"]),
    )


async def dispose_engine() -> None:
    await engine.dispose()
