"""Migration submission must use the shared queue and survive interrupted writes."""

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _module():
    path = Path(__file__).parents[2] / "scripts/run_catalogue_migration.py"
    spec = importlib.util.spec_from_file_location("catalogue_migration_runner_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_submission_resumes_fixed_pages_and_never_runs_a_parser(tmp_path, monkeypatch):
    module = _module()
    identifiers = [uuid4() for _ in range(600)]
    calls = []

    async def manifest(directory):
        return identifiers

    async def enqueue(ids, *, user_id, request_key):
        calls.append((ids, request_key))
        if len(calls) == 2:
            raise RuntimeError("queue interrupted")
        return [SimpleNamespace(id=uuid4())]

    monkeypatch.setattr(module, "_manifest", manifest)
    monkeypatch.setattr(module.UploadBatchService, "enqueue_reparse_batch", enqueue)
    args = SimpleNamespace(state_directory=tmp_path, user_id=uuid4())
    with pytest.raises(RuntimeError, match="queue interrupted"):
        asyncio.run(module._run(args))
    assert asyncio.run(module._run(args)) == 0
    assert [len(ids) for ids, _ in calls] == [256, 256, 256, 88]
    assert calls[1] == calls[2]
    assert asyncio.run(module._run(args)) == 0
    assert len(calls) == 4
    progress = json.loads((tmp_path / "queue-progress.json").read_text())
    assert progress["phase"] == "submitted"
    assert progress["executor"] == "upload-worker"
    assert progress["submitted"] == 600
