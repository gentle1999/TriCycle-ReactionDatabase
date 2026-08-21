"""Authenticated durable upload-batch routes."""

import tempfile
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status

from tricycle_reaction_db.api.authentication import get_authenticated_principal
from tricycle_reaction_db.application.dtos import (
    UploadBatchCreate,
    UploadBatchItemPage,
    UploadBatchItemView,
    UploadBatchPage,
    UploadBatchStatusUpdate,
    UploadBatchView,
)
from tricycle_reaction_db.application.services.authentication import AuthenticatedPrincipal
from tricycle_reaction_db.application.services.authorization import ProjectAccessDeniedError
from tricycle_reaction_db.application.services.transition_state_uploads import (
    ArtifactUploadConflictError,
    ArtifactUploadError,
    ArtifactUploadLimitError,
    ArtifactUploadPayload,
)
from tricycle_reaction_db.application.services.upload_batches import (
    UploadBatchConflictError,
    UploadBatchLimitError,
    UploadBatchNotFoundError,
    UploadBatchService,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.domain.enums import UploadBatchItemStatus

router = APIRouter(prefix="/api/upload-batches", tags=["artifact upload queues"])
Principal = Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)]
UPLOAD_PREFLIGHT_HEADERS = {"X-Upload-Rejection-Stage": "preflight"}


async def _spool_upload(file: UploadFile, path: Path, *, maximum: int) -> int:
    size = 0
    with path.open("wb") as spool:
        while chunk := await file.read(min(1024 * 1024, maximum + 1 - size)):
            spool.write(chunk)
            size += len(chunk)
            if size > maximum:
                break
    return size


@router.post("", response_model=UploadBatchView, status_code=status.HTTP_201_CREATED)
async def create_upload_batch(payload: UploadBatchCreate, principal: Principal) -> UploadBatchView:
    try:
        return await UploadBatchService.create(payload, user_id=principal.user_id)
    except ProjectAccessDeniedError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except UploadBatchLimitError as error:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(error),
            headers=UPLOAD_PREFLIGHT_HEADERS,
        ) from error
    except UploadBatchConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.get("", response_model=UploadBatchPage)
async def list_upload_batches(
    principal: Principal,
    project_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> UploadBatchPage:
    return await UploadBatchService.list_batches(
        user_id=principal.user_id,
        project_id=project_id,
        limit=limit,
        offset=offset,
    )


@router.get("/{batch_id}", response_model=UploadBatchView)
async def get_upload_batch(batch_id: UUID, principal: Principal) -> UploadBatchView:
    try:
        return await UploadBatchService.get(batch_id, user_id=principal.user_id)
    except UploadBatchNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.post("/{batch_id}/recover", response_model=UploadBatchView)
async def recover_upload_batch(batch_id: UUID, principal: Principal) -> UploadBatchView:
    try:
        return await UploadBatchService.recover_interrupted(
            batch_id,
            user_id=principal.user_id,
        )
    except UploadBatchNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except UploadBatchConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.get("/{batch_id}/items", response_model=UploadBatchItemPage)
async def list_upload_batch_items(
    batch_id: UUID,
    principal: Principal,
    item_status: UploadBatchItemStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> UploadBatchItemPage:
    try:
        return await UploadBatchService.list_items(
            batch_id,
            user_id=principal.user_id,
            status=item_status,
            limit=limit,
            offset=offset,
        )
    except UploadBatchNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.patch("/{batch_id}", response_model=UploadBatchView)
async def update_upload_batch_status(
    batch_id: UUID,
    payload: UploadBatchStatusUpdate,
    principal: Principal,
) -> UploadBatchView:
    try:
        return await UploadBatchService.set_status(batch_id, payload, user_id=principal.user_id)
    except UploadBatchNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except UploadBatchConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.delete("/{batch_id}", response_model=UploadBatchView)
async def cancel_upload_batch(batch_id: UUID, principal: Principal) -> UploadBatchView:
    try:
        return await UploadBatchService.cancel(batch_id, user_id=principal.user_id)
    except UploadBatchNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except UploadBatchConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/{batch_id}/retry-failed", response_model=UploadBatchView)
async def retry_failed_upload_batch_items(
    batch_id: UUID,
    principal: Principal,
) -> UploadBatchView:
    try:
        return await UploadBatchService.retry_failed(batch_id, user_id=principal.user_id)
    except UploadBatchNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except UploadBatchConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post(
    "/{batch_id}/items/{client_file_id}/retry",
    response_model=UploadBatchItemView,
)
async def retry_upload_batch_item(
    batch_id: UUID,
    client_file_id: UUID,
    principal: Principal,
) -> UploadBatchItemView:
    try:
        return await UploadBatchService.retry_item(
            batch_id,
            client_file_id,
            user_id=principal.user_id,
        )
    except UploadBatchNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except UploadBatchConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/{batch_id}/files/{client_file_id}", response_model=UploadBatchItemView)
async def upload_batch_file(
    batch_id: UUID,
    client_file_id: UUID,
    principal: Principal,
    file: Annotated[UploadFile, File()],
) -> UploadBatchItemView:
    filename = file.filename or ""
    if not filename.strip():
        await file.close()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="uploaded artifact requires a filename",
        )
    maximum = get_settings().max_upload_bytes
    payload = await file.read(maximum + 1)
    await file.close()
    if len(payload) > maximum:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"uploaded artifact exceeds the {maximum}-byte limit",
            headers=UPLOAD_PREFLIGHT_HEADERS,
        )
    try:
        return await UploadBatchService.upload_item(
            batch_id,
            client_file_id,
            payload=payload,
            filename=filename,
            media_type=file.content_type or "application/octet-stream",
            user_id=principal.user_id,
        )
    except UploadBatchNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ProjectAccessDeniedError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except (UploadBatchConflictError, ArtifactUploadConflictError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ArtifactUploadLimitError as error:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(error),
        ) from error
    except ArtifactUploadError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@router.post("/{batch_id}/files", response_model=list[UploadBatchItemView])
async def upload_batch_files(
    batch_id: UUID,
    principal: Principal,
    client_file_ids: Annotated[list[UUID], Form()],
    files: Annotated[list[UploadFile], File()],
) -> list[UploadBatchItemView]:
    """Upload a group of queue files and parse calculation outputs in one MolOP batch."""

    settings = get_settings()
    if not files or len(files) != len(client_file_ids):
        for file in files:
            await file.close()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="client_file_ids must contain one id per uploaded file",
        )
    if len(files) > settings.max_batch_files:
        for file in files:
            await file.close()
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"upload batch exceeds the {settings.max_batch_files}-file limit",
            headers=UPLOAD_PREFLIGHT_HEADERS,
        )
    try:
        with tempfile.TemporaryDirectory(prefix="tricycle-queue-batch-") as spool_directory:
            payloads: list[tuple[UUID, ArtifactUploadPayload]] = []
            total_bytes = 0
            try:
                for index, (client_file_id, file) in enumerate(
                    zip(client_file_ids, files, strict=True)
                ):
                    filename = file.filename or ""
                    if not filename.strip():
                        raise HTTPException(
                            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail="uploaded artifact requires a filename",
                        )
                    spool_path = Path(spool_directory) / f"{index:08d}.upload"
                    size = await _spool_upload(
                        file,
                        spool_path,
                        maximum=settings.max_upload_bytes,
                    )
                    if size > settings.max_upload_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                            detail=(
                                "uploaded artifact exceeds the "
                                f"{settings.max_upload_bytes}-byte limit"
                            ),
                            headers=UPLOAD_PREFLIGHT_HEADERS,
                        )
                    total_bytes += size
                    if total_bytes > settings.max_batch_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                            detail=(
                                f"upload batch exceeds the {settings.max_batch_bytes}-byte limit"
                            ),
                            headers=UPLOAD_PREFLIGHT_HEADERS,
                        )
                    payloads.append(
                        (
                            client_file_id,
                            ArtifactUploadPayload(
                                filename=filename,
                                media_type=file.content_type or "application/octet-stream",
                                payload=None,
                                spool_path=spool_path,
                            ),
                        )
                    )
            finally:
                for file in files:
                    await file.close()
            return await UploadBatchService.upload_items(
                batch_id,
                files=payloads,
                user_id=principal.user_id,
            )
    except UploadBatchNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ProjectAccessDeniedError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except (UploadBatchConflictError, ArtifactUploadConflictError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ArtifactUploadLimitError as error:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(error),
        ) from error
    except ArtifactUploadError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


__all__ = ["router"]
