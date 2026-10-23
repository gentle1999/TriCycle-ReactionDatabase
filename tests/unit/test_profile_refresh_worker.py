import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tricycle_reaction_db.application.services.thermodynamic_profile_recovery import (
    ProfileDependencyAudit,
)
from tricycle_reaction_db.dev import profile_refresh_worker as worker


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_first_audit", [False, True])
async def test_worker_audits_without_queue_and_bounds_draining(monkeypatch, fail_first_audit):
    stop = asyncio.Event()
    next_id = uuid4()
    seen_cursors = []
    limits = []
    commits = []

    def recover(session, *, after_id, batch_size):
        seen_cursors.append(after_id)
        assert batch_size == 256
        if fail_first_audit and len(seen_cursors) == 1:
            raise RuntimeError("temporary database failure")
        return ProfileDependencyAudit(256, 0, 0, next_id)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def run_sync(self, callback):
            return callback(self)

        async def commit(self):
            commits.append(True)

    async def drain(**kwargs):
        limits.append(kwargs["limit"])
        if len(limits) == 2:
            stop.set()
        return True

    monkeypatch.setattr(worker, "session_factory", Session)
    monkeypatch.setattr(worker, "recover_profile_refresh_dependencies", recover)
    monkeypatch.setattr(worker, "refresh_pending_mapped_reaction_profiles", drain)
    monkeypatch.setattr(worker, "dispose_engine", AsyncMock())
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(
            profile_refresh_worker_audit_batch_size=256,
            upload_worker_profile_refresh_batch_size=32,
            profile_refresh_worker_poll_interval_seconds=0.001,
        ),
    )
    await worker.ThermodynamicProfileRefreshWorker().run(stop)
    assert seen_cursors == [None, None if fail_first_audit else next_id]
    assert limits == [128, 128]
    assert len(commits) == (1 if fail_first_audit else 2)
    worker.dispose_engine.assert_awaited_once()
