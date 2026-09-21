"""Real PostgreSQL COPY rollback/isolation, using connection-local tables only."""

import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest

from tricycle_reaction_db.application.services._persistence import _copy_rows_to_postgresql
from tricycle_reaction_db.application.services.artifact_upload_types import ParsedArtifactTask
from tricycle_reaction_db.application.services.artifact_uploads import ArtifactUploadService
from tricycle_reaction_db.core.config import get_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1", reason="requires PostgreSQL"
    ),
]


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
