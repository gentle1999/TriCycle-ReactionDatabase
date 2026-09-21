import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg.errors import InvalidTextRepresentation

from tricycle_reaction_db.application.services.artifact_upload_types import ParsedArtifactTask
from tricycle_reaction_db.application.services.artifact_uploads import ArtifactUploadService


def tasks(count):
    return [
        ParsedArtifactTask(
            artifact_id=uuid4(),
            started_at=datetime.now(UTC),
            parsed=SimpleNamespace(source_frame_count=1),
        )
        for _ in range(count)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_indices", [set(), {4}, {0, 8}, set(range(9))])
async def test_data_errors_isolate_files_without_reparsing(monkeypatch, bad_indices):
    entries = tasks(9)
    bad = {entries[i].artifact_id for i in bad_indices}
    successes = {t.artifact_id: object() for t in entries}
    project, user = uuid4(), uuid4()
    leases = {t.artifact_id: uuid4() for t in entries}
    calls = []

    async def once(subset, **kwargs):
        calls.append(tuple(t.artifact_id for t in subset))
        assert kwargs["project_id"] == project
        assert kwargs["user_id"] == user
        assert kwargs["worker_lease_by_artifact_id"] is leases
        assert all(any(t is source for source in entries) for t in subset)
        if any(t.artifact_id in bad for t in subset):
            raise InvalidTextRepresentation("invalid input syntax for type json")
        return {t.artifact_id: successes[t.artifact_id] for t in subset}

    monkeypatch.setattr(ArtifactUploadService, "_persist_parsed_microbatch_once", once)
    result = await ArtifactUploadService.persist_parsed_microbatch(
        entries, project_id=project, user_id=user, worker_lease_by_artifact_id=leases
    )
    assert set(result) == {t.artifact_id for t in entries}
    assert len(calls) <= 2 * len(entries) - 1
    if not bad:
        assert len(calls) == 1
    for t in entries:
        if t.artifact_id in bad:
            assert isinstance(result[t.artifact_id], InvalidTextRepresentation)
        else:
            assert result[t.artifact_id] is successes[t.artifact_id]


@pytest.mark.asyncio
async def test_committed_prefix_is_not_retried_after_later_window_fails(monkeypatch):
    entries = tasks(4)
    committed = object()
    calls = []

    async def once(subset, **kwargs):
        calls.append(subset)
        if len(calls) == 1:
            kwargs["_completed_results"][entries[0].artifact_id] = committed
            raise InvalidTextRepresentation("later COPY window failed")
        assert entries[0] not in subset
        return {t.artifact_id: object() for t in subset}

    monkeypatch.setattr(ArtifactUploadService, "_persist_parsed_microbatch_once", once)
    result = await ArtifactUploadService.persist_parsed_microbatch(
        entries, project_id=uuid4(), user_id=uuid4()
    )
    assert len(result) == 4
    assert result[entries[0].artifact_id] is committed


@pytest.mark.asyncio
async def test_cancellation_propagates_without_split(monkeypatch):
    calls = []

    async def once(subset, **kwargs):
        calls.append(subset)
        raise asyncio.CancelledError()

    monkeypatch.setattr(ArtifactUploadService, "_persist_parsed_microbatch_once", once)
    with pytest.raises(asyncio.CancelledError):
        await ArtifactUploadService.persist_parsed_microbatch(
            tasks(3), project_id=uuid4(), user_id=uuid4()
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_connection_failure_does_not_fan_out_or_erase_committed_results(monkeypatch):
    entries = tasks(3)
    committed = object()
    error = ConnectionError("database unavailable")
    calls = []

    async def once(subset, **kwargs):
        calls.append(subset)
        kwargs["_completed_results"][entries[0].artifact_id] = committed
        raise error

    monkeypatch.setattr(ArtifactUploadService, "_persist_parsed_microbatch_once", once)
    result = await ArtifactUploadService.persist_parsed_microbatch(
        entries, project_id=uuid4(), user_id=uuid4()
    )
    assert len(calls) == 1
    assert result[entries[0].artifact_id] is committed
    assert all(result[t.artifact_id] is error for t in entries[1:])


@pytest.mark.asyncio
async def test_empty_batch_does_not_open_transaction(monkeypatch):
    async def once(*args, **kwargs):
        pytest.fail("empty batch attempted persistence")

    monkeypatch.setattr(ArtifactUploadService, "_persist_parsed_microbatch_once", once)
    assert (
        await ArtifactUploadService.persist_parsed_microbatch(
            [], project_id=uuid4(), user_id=uuid4()
        )
        == {}
    )


@pytest.mark.asyncio
async def test_commit_receipt_and_deferred_recovery_are_wired_to_upload_batch(monkeypatch):
    entries = tasks(3)
    successes = {task.artifact_id: object() for task in entries}
    calls = []

    async def prepare(**kwargs):
        subset = kwargs["parsed_tasks"]
        return (
            [SimpleNamespace(filename=str(t.artifact_id)) for t in subset],
            {
                i: SimpleNamespace(artifact_id=t.artifact_id, size_bytes=1)
                for i, t in enumerate(subset)
            },
            dict(enumerate(subset)),
            {},
        )

    async def upload(**kwargs):
        subset = kwargs["_preparsed_tasks"]
        assert kwargs["_defer_abort_recovery"] is True
        calls.append([t.artifact_id for t in subset.values()])
        if len(calls) > 1:
            assert entries[0].artifact_id not in calls[-1]
        items = []
        for index, task in subset.items():
            item = SimpleNamespace(
                result=successes[task.artifact_id], error_message=None, error_code=None
            )
            await kwargs["on_file_committed"](index, item)
            items.append(item)
            if len(calls) == 1:
                raise InvalidTextRepresentation("later transaction failed")
        return SimpleNamespace(
            items=items,
            timings_ms={},
            total_count=len(items),
            succeeded_count=len(items),
            failed_count=0,
            source_frame_count=len(items),
        )

    monkeypatch.setattr(ArtifactUploadService, "_prepare_preparsed_batch", prepare)
    monkeypatch.setattr(ArtifactUploadService, "upload_batch", upload)
    result = await ArtifactUploadService.persist_parsed_microbatch(
        entries, project_id=uuid4(), user_id=uuid4()
    )
    assert result == successes
