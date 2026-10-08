"""PostgreSQL COPY rollback/isolation, using connection-local tables only."""

import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, select
from sqlalchemy.orm import registry
from sqlmodel import Session

from tricycle_reaction_db.application.services._persistence import (
    _attach_pending_entities,
    _copy_rows_to_postgresql,
    _entity_identity_key,
    _fast_pending_entity_count,
    _queue_fast_pending_entity,
    _session_entity_for_identity,
    _truncate_fast_pending_entities,
)
from tricycle_reaction_db.application.services.artifact_upload_types import ParsedArtifactTask
from tricycle_reaction_db.application.services.artifact_uploads import ArtifactUploadService
from tricycle_reaction_db.core.config import get_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1", reason="requires PostgreSQL"
    ),
]


@pytest.mark.parametrize("bulk_disabled", [False, True])
def test_deferred_flush_deduplicates_and_preserves_file_rollback(bulk_disabled):
    """Queue optimizations must preserve COPY, ORM fallback and failed-file isolation."""
    mapper_registry = registry()
    table = Table(
        "_deferred_flush_probe",
        MetaData(),
        Column("owner", Integer, primary_key=True),
        Column("position", Integer, primary_key=True),
        Column("value", String, nullable=False),
        prefixes=["TEMPORARY"],
    )

    class Row:
        pass

    mapper_registry.map_imperatively(Row, table)
    engine = create_engine(get_settings().database_url)
    try:
        with engine.connect() as connection, connection.begin():
            table.create(connection)
            with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                session.info["tricycle_fast_insert"] = True
                session.info["tricycle_bulk_insert_disabled"] = bulk_disabled
                first = Row(owner=1, position=0, value="first")
                _queue_fast_pending_entity(session, first)
                _queue_fast_pending_entity(session, first)
                duplicate = Row(owner=1, position=0, value="first")
                _queue_fast_pending_entity(session, duplicate)
                assert _session_entity_for_identity(session, duplicate) is first
                checkpoint = _fast_pending_entity_count(session)
                failed = Row(owner=1, position=1, value="failed")
                _queue_fast_pending_entity(session, failed)
                _truncate_fast_pending_entities(session, checkpoint)
                replacement = Row(owner=1, position=1, value="replacement")
                assert _session_entity_for_identity(session, replacement) is replacement
                _queue_fast_pending_entity(session, replacement)
                for position in range(2, 72):
                    _queue_fast_pending_entity(
                        session, Row(owner=1, position=position, value=str(position))
                    )
                _attach_pending_entities(session)
                session.flush()
                assert _fast_pending_entity_count(session) == 0
                rows = connection.execute(select(table).order_by(table.c.position)).all()
                assert len(rows) == 72
                assert rows[0].value == "first"
                assert rows[1].value == "replacement"
                # A later batch must reuse the loaded canonical object and
                # update it, rather than INSERT its primary key a second time.
                loaded = session.execute(
                    select(Row).where(table.c.owner == 1, table.c.position == 0)
                ).scalar_one()
                _queue_fast_pending_entity(session, Row(owner=1, position=0, value="updated"))
                _attach_pending_entities(session)
                session.flush()
                assert loaded.value == "updated"
                assert (
                    connection.execute(
                        select(table.c.value).where(table.c.owner == 1, table.c.position == 0)
                    ).scalar_one()
                    == "updated"
                )
    finally:
        engine.dispose()
        mapper_registry.dispose()


def test_identity_metadata_cache_reads_current_primary_key_values():
    mapper_registry = registry()
    table = Table(
        "_identity_cache_probe",
        MetaData(),
        Column("owner", Integer, primary_key=True),
        Column("position", Integer, primary_key=True),
    )

    class Row:
        pass

    mapper_registry.map_imperatively(Row, table)
    try:
        row = Row(owner=1)
        assert _entity_identity_key(row) is None
        row.position = 2
        original = _entity_identity_key(row)
        row.position = 3
        assert _entity_identity_key(row) != original
        row.owner = None
        assert _entity_identity_key(row) is None
    finally:
        mapper_registry.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_prefix", [False, True])
async def test_invalid_json_isolated_without_replaying_committed_prefix(monkeypatch, commit_prefix):
    entries = [
        ParsedArtifactTask(
            artifact_id=uuid4(),
            started_at=datetime.now(UTC),
            parsed=SimpleNamespace(source_frame_count=1),
        )
        for _ in range(9)
    ]
    bad_id = entries[4].artifact_id
    calls = []
    url = get_settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as connection:
        await connection.execute(
            "CREATE TEMP TABLE isolation_rows (id uuid PRIMARY KEY, comments jsonb) "
            "ON COMMIT PRESERVE ROWS"
        )

        async def once(subset, **kwargs):
            calls.append([task.artifact_id for task in subset])
            results = {}
            windows = [subset[:3], subset[3:]] if commit_prefix and len(calls) == 1 else [subset]
            for window in windows:
                async with connection.transaction():
                    await _copy_rows_to_postgresql(
                        connection,
                        "COPY isolation_rows (id, comments) FROM STDIN",
                        [
                            (
                                task.artifact_id,
                                json.dumps(
                                    {"text": "\udce2" if task.artifact_id == bad_id else "ok"}
                                ),
                            )
                            for task in window
                        ],
                    )
                for task in window:
                    results[task.artifact_id] = object()
                kwargs["_completed_results"].update(results)
            return results

        monkeypatch.setattr(ArtifactUploadService, "_persist_parsed_microbatch_once", once)
        result = await ArtifactUploadService.persist_parsed_microbatch(
            entries, project_id=uuid4(), user_id=uuid4()
        )
        assert len(result) == 9
        assert isinstance(result[bad_id], psycopg.errors.InvalidTextRepresentation)
        assert sum(isinstance(value, Exception) for value in result.values()) == 1
        rows = await (await connection.execute("SELECT id FROM isolation_rows")).fetchall()
        assert {row[0] for row in rows} == {
            task.artifact_id for task in entries if task.artifact_id != bad_id
        }
        if commit_prefix:
            assert not any(task.artifact_id in call for task in entries[:3] for call in calls[1:])
