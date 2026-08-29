from datetime import UTC, datetime
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from tricycle_reaction_db.api.app import create_app
from tricycle_reaction_db.application.dtos import (
    UploadBatchCreate,
    UploadBatchItemView,
    UploadBatchView,
)
from tricycle_reaction_db.application.services.upload_batches import UploadBatchService
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactKind,
    UploadBatchItemStatus,
    UploadBatchStatus,
)
from tricycle_reaction_db.domain.identity import DEVELOPMENT_USER_ID

PROJECT_ID = UUID("00000000-0000-7000-8000-000000000201")
BATCH_ID = UUID("00000000-0000-7000-8000-000000000701")
ITEM_ID = UUID("00000000-0000-7000-8000-000000000702")
CLIENT_FILE_ID = UUID("00000000-0000-7000-8000-000000000703")
ARTIFACT_ID = UUID("00000000-0000-7000-8000-000000000704")
SECOND_CLIENT_FILE_ID = UUID("00000000-0000-7000-8000-000000000705")
SECOND_ARTIFACT_ID = UUID("00000000-0000-7000-8000-000000000706")
NOW = datetime(2026, 8, 20, tzinfo=UTC)


def _batch_view() -> UploadBatchView:
    return UploadBatchView(
        id=BATCH_ID,
        created_at=NOW,
        updated_at=NOW,
        project_id=PROJECT_ID,
        created_by_user_id=DEVELOPMENT_USER_ID,
        artifact_kind=ArtifactKind.AUXILIARY,
        status=UploadBatchStatus.ACTIVE,
        shared_metadata={"campaign": "screen-42"},
        total_count=1,
        total_bytes=4,
        succeeded_count=0,
        failed_count=0,
        cancelled_count=0,
        uploading_count=0,
    )


def _item_view(
    *,
    client_file_id: UUID = CLIENT_FILE_ID,
    artifact_id: UUID = ARTIFACT_ID,
    filename: str = "notes.txt",
    position: int = 0,
    ingestion_status: ArtifactIngestionStatus | None = ArtifactIngestionStatus.PENDING,
) -> UploadBatchItemView:
    return UploadBatchItemView(
        id=ITEM_ID,
        created_at=NOW,
        updated_at=NOW,
        client_file_id=client_file_id,
        position=position,
        original_filename=filename,
        relative_path=f"run-1/{filename}",
        size_bytes=4,
        media_type="text/plain",
        status=UploadBatchItemStatus.SUCCEEDED,
        attempt_count=1,
        artifact_file_id=artifact_id,
        ingestion_status=ingestion_status,
        metadata={"campaign": "screen-42"},
    )


def test_upload_batch_manifest_rejects_duplicate_client_keys_and_unsafe_paths() -> None:
    base = {
        "project_id": PROJECT_ID,
        "artifact_kind": "auxiliary",
        "shared_metadata": {},
    }
    with pytest.raises(ValidationError, match="client_file_id must be unique"):
        UploadBatchCreate.model_validate(
            {
                **base,
                "files": [
                    {
                        "client_file_id": CLIENT_FILE_ID,
                        "original_filename": "one.txt",
                        "relative_path": "one.txt",
                        "size_bytes": 1,
                    },
                    {
                        "client_file_id": CLIENT_FILE_ID,
                        "original_filename": "two.txt",
                        "relative_path": "two.txt",
                        "size_bytes": 1,
                    },
                ],
            }
        )
    with pytest.raises(ValidationError, match="relative_path must stay"):
        UploadBatchCreate.model_validate(
            {
                **base,
                "files": [
                    {
                        "client_file_id": CLIENT_FILE_ID,
                        "original_filename": "one.txt",
                        "relative_path": "../one.txt",
                        "size_bytes": 1,
                    }
                ],
            }
        )


@pytest.mark.asyncio
async def test_create_upload_batch_preserves_shared_metadata_and_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def create(payload: UploadBatchCreate, *, user_id: UUID) -> UploadBatchView:
        assert user_id == DEVELOPMENT_USER_ID
        assert payload.project_id == PROJECT_ID
        assert payload.shared_metadata == {"campaign": "screen-42"}
        assert payload.files[0].relative_path == "run-1/notes.txt"
        return _batch_view()

    monkeypatch.setattr(UploadBatchService, "create", staticmethod(create))
    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/upload-batches",
            json={
                "project_id": str(PROJECT_ID),
                "artifact_kind": "auxiliary",
                "shared_metadata": {"campaign": "screen-42"},
                "files": [
                    {
                        "client_file_id": str(CLIENT_FILE_ID),
                        "original_filename": "notes.txt",
                        "relative_path": "run-1/notes.txt",
                        "size_bytes": 4,
                        "media_type": "text/plain",
                    }
                ],
            },
        )

    assert response.status_code == 201
    assert response.json()["shared_metadata"] == {"campaign": "screen-42"}
    assert response.json()["total_count"] == 1


@pytest.mark.asyncio
async def test_upload_batch_file_uses_client_file_id_as_idempotency_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def upload_item(
        batch_id: UUID,
        client_file_id: UUID,
        **values: object,
    ) -> UploadBatchItemView:
        assert batch_id == BATCH_ID
        assert client_file_id == CLIENT_FILE_ID
        assert values == {
            "payload": b"data",
            "filename": "notes.txt",
            "media_type": "text/plain",
            "user_id": DEVELOPMENT_USER_ID,
        }
        return _item_view()

    monkeypatch.setattr(UploadBatchService, "upload_item", staticmethod(upload_item))
    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/upload-batches/{BATCH_ID}/files/{CLIENT_FILE_ID}",
            files={"file": ("notes.txt", b"data", "text/plain")},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert response.json()["artifact_file_id"] == str(ARTIFACT_ID)
    assert response.json()["ingestion_status"] == "pending"
    assert response.json()["metadata"] == {"campaign": "screen-42"}


@pytest.mark.asyncio
async def test_recover_upload_batch_exposes_interrupted_upload_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def recover_interrupted(batch_id: UUID, *, user_id: UUID) -> UploadBatchView:
        assert batch_id == BATCH_ID
        assert user_id == DEVELOPMENT_USER_ID
        return _batch_view()

    monkeypatch.setattr(
        UploadBatchService,
        "recover_interrupted",
        staticmethod(recover_interrupted),
    )
    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        response = await client.post(f"/api/upload-batches/{BATCH_ID}/recover")

    assert response.status_code == 200
    assert response.json()["id"] == str(BATCH_ID)


@pytest.mark.asyncio
async def test_upload_batch_files_preserves_client_file_order_in_one_service_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def upload_items(
        batch_id: UUID,
        **values: object,
    ) -> list[UploadBatchItemView]:
        nonlocal calls
        calls += 1
        assert batch_id == BATCH_ID
        assert values["user_id"] == DEVELOPMENT_USER_ID
        uploaded = values["files"]
        assert isinstance(uploaded, list)
        assert [client_file_id for client_file_id, _ in uploaded] == [
            CLIENT_FILE_ID,
            SECOND_CLIENT_FILE_ID,
        ]
        assert [payload.spool_path.read_bytes() for _, payload in uploaded] == [
            b"one!",
            b"two!",
        ]
        return [
            _item_view(),
            _item_view(
                client_file_id=SECOND_CLIENT_FILE_ID,
                artifact_id=SECOND_ARTIFACT_ID,
                filename="second.txt",
                position=1,
            ),
        ]

    monkeypatch.setattr(UploadBatchService, "upload_items", staticmethod(upload_items))
    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/upload-batches/{BATCH_ID}/files",
            data={
                "client_file_ids": [str(CLIENT_FILE_ID), str(SECOND_CLIENT_FILE_ID)],
            },
            files=[
                ("files", ("notes.txt", b"one!", "text/plain")),
                ("files", ("second.txt", b"two!", "text/plain")),
            ],
        )

    assert response.status_code == 200
    assert calls == 1
    assert [item["client_file_id"] for item in response.json()] == [
        str(CLIENT_FILE_ID),
        str(SECOND_CLIENT_FILE_ID),
    ]


def test_upload_batch_routes_are_exposed() -> None:
    paths = {route.path for route in create_app().routes}
    assert "/api/upload-batches" in paths
    assert "/api/upload-batches/{batch_id}/items" in paths
    assert "/api/upload-batches/{batch_id}/recover" in paths
    assert "/api/upload-batches/{batch_id}/files" in paths
    assert "/api/upload-batches/{batch_id}/files/{client_file_id}" in paths
    assert "/api/upload-batches/{batch_id}/items/{client_file_id}/retry" in paths
