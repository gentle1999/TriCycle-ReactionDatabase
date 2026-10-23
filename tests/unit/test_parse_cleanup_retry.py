from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg.errors import DeadlockDetected
from sqlalchemy.exc import OperationalError

from tricycle_reaction_db.application.services import artifact_uploads
from tricycle_reaction_db.application.services.artifact_parse_replacement import ParseCleanupSummary
from tricycle_reaction_db.domain.enums import ArtifactKind, StorageStatus


@pytest.mark.asyncio
async def test_cleanup_retries_after_rollback_in_fresh_session(monkeypatch):
    artifact = SimpleNamespace(
        id=uuid4(),
        project_id=uuid4(),
        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
        storage_status=StorageStatus.AVAILABLE,
    )
    events = []
    sessions = []
    cleanup = ParseCleanupSummary(deleted_frame_count=2)

    class Session:
        def __init__(self):
            self.index = len(sessions)
            sessions.append(self)

        async def __aenter__(self):
            events.append((self.index, "open"))
            return self

        async def __aexit__(self, exc_type, exc, tb):
            events.append((self.index, "rollback" if exc else "close"))

        async def exec(self, statement):
            return SimpleNamespace(all=lambda: [artifact])

        async def run_sync(self, operation):
            events.append((self.index, "cleanup"))
            if self.index == 0:
                raise OperationalError("DELETE frame", {}, DeadlockDetected("deadlock"))
            return cleanup

        async def commit(self):
            events.append((self.index, "commit"))

    async def authorize(*args):
        pass

    async def statistics(*args, **kwargs):
        events.append((None, "statistics"))

    monkeypatch.setattr(artifact_uploads, "session_factory", Session)
    monkeypatch.setattr(
        artifact_uploads.AuthorizationService, "require_project_permission", authorize
    )
    monkeypatch.setattr(artifact_uploads, "refresh_project_statistics", statistics)
    result = await artifact_uploads.ArtifactUploadService.clear_previous_parse_results(
        artifact_ids=[artifact.id], user_id=uuid4()
    )
    assert result is cleanup
    assert events == [
        (0, "open"),
        (0, "cleanup"),
        (0, "rollback"),
        (1, "open"),
        (1, "cleanup"),
        (1, "commit"),
        (1, "close"),
        (None, "statistics"),
    ]
