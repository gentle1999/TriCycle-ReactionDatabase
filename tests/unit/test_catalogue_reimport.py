"""A forced catalogue migration verifies source bytes before replacing rows."""

import asyncio
import importlib.util
import json
import sys
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _module():
    path = Path(__file__).parents[2] / "scripts/reimport_artifact_objects.py"
    spec = importlib.util.spec_from_file_location("catalogue_reimport_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("corrupt", [False, True])
def test_forced_catalogue_reimport_verifies_bytes_before_replacement(
    tmp_path, monkeypatch, corrupt
):
    module = _module()
    payload = b"source calculation"
    artifact = module.ArtifactObject(
        id=uuid4(),
        project_id=uuid4(),
        bucket="fixture",
        object_key="exact/key",
        version_id="pinned-version",
        content_sha256=sha256(payload).hexdigest(),
        size_bytes=len(payload),
        original_filename="sample.log",
        media_type="text/plain",
    )
    called = []

    async def artifacts(*args, **kwargs):
        return [artifact]

    async def no_partial(*args, **kwargs):
        return set()

    async def noop(*args, **kwargs):
        pass

    class Store:
        def __init__(self, settings):
            pass

        def get_bytes(self, key, *, version_id):
            assert key == artifact.object_key and version_id == artifact.version_id
            return b"X" * len(payload) if corrupt else payload

        def close(self):
            pass

    async def upload(**kwargs):
        assert kwargs["force_reparse"] is True
        assert kwargs["files"][0].spool_path.read_bytes() == payload
        called.append(True)
        result = SimpleNamespace(
            ingestion_status=module.ArtifactIngestionStatus.SUCCEEDED,
            source_frame_count=1,
            transition_state_frame_count=0,
            inferred_reaction_count=0,
            ingestion_id=uuid4(),
            parse_revision_id=uuid4(),
        )
        return SimpleNamespace(
            items=[SimpleNamespace(result=result)],
            succeeded_count=1,
            failed_count=0,
            source_frame_count=1,
            transition_state_frame_count=0,
            inferred_reaction_count=0,
        )

    monkeypatch.setattr(module, "_load_artifacts", artifacts)
    monkeypatch.setattr(module, "_load_partial_artifact_ids", no_partial)
    monkeypatch.setattr(module, "RustFSObjectStore", Store)
    monkeypatch.setattr(module, "refresh_project_statistics", noop)
    monkeypatch.setattr(module, "close_molop_process_pool", noop)
    monkeypatch.setattr(module.ArtifactUploadService, "upload_batch", upload)
    state = tmp_path / "state.jsonl"
    args = module._parser().parse_args(
        [
            "--catalogue-only",
            "--force-reparse",
            "--user-id",
            str(uuid4()),
            "--state-file",
            str(state),
            "--progress-file",
            str(tmp_path / "progress.json"),
        ]
    )
    assert asyncio.run(module._run(args)) == int(corrupt)
    assert bool(called) is not corrupt
    progress = json.loads((tmp_path / "progress.json").read_text())
    assert progress["phase"] == ("failed" if corrupt else "complete")
    assert progress["pending"] == 1
    checkpoint = json.loads(state.read_text().splitlines()[0])
    assert checkpoint["status"] == ("failed" if corrupt else "succeeded")
    if not corrupt:
        # A resumed run must not clear and parse an already completed file.
        assert asyncio.run(module._run(args)) == 0
        assert len(called) == 1


def test_partial_checkpoint_lookup_stays_below_postgresql_parameter_limit(monkeypatch):
    module = _module()
    identifiers = [uuid4() for _ in range(70_000)]
    observed = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, statement):
            values = statement.compile().params["artifact_file_id_1"]
            assert len(values) <= 4096
            observed.extend(values)
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: values[:1]))

    monkeypatch.setattr(module, "session_factory", Session)
    result = asyncio.run(module._load_partial_artifact_ids(identifiers))
    assert observed == identifiers
    assert result == set(identifiers[::4096])
