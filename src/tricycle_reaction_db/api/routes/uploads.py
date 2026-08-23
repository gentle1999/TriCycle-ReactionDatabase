"""Authenticated artifact upload routes."""

import tempfile
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from tricycle_reaction_db.api.authentication import get_authenticated_principal
from tricycle_reaction_db.application.dtos import (
    ArtifactBatchUploadResult,
    ArtifactMetadataUpdate,
    ArtifactSummary,
    ArtifactUploadResult,
    ArtifactValidationResult,
)
from tricycle_reaction_db.application.services.artifact_management import (
    ArtifactManagementService,
    ArtifactMetadataConflictError,
    ArtifactRemovalIntegrityError,
    ArtifactRemovalNotFoundError,
    ArtifactRemovalUnavailableError,
)
from tricycle_reaction_db.application.services.artifact_uploads import (
    ArtifactUploadConflictError,
    ArtifactUploadError,
    ArtifactUploadLimitError,
    ArtifactUploadPayload,
    ArtifactUploadService,
)
from tricycle_reaction_db.application.services.authentication import AuthenticatedPrincipal
from tricycle_reaction_db.application.services.authorization import ProjectAccessDeniedError
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.domain.enums import ArtifactKind

router = APIRouter(prefix="/api/artifacts", tags=["artifact ingestion"])
Principal = Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)]
UPLOAD_PREFLIGHT_HEADERS = {"X-Upload-Rejection-Stage": "preflight"}


async def _spool_upload(file: UploadFile, path: Path, *, maximum: int) -> int:
    size = 0
    try:
        with path.open("wb") as target:
            while True:
                chunk = await file.read(min(1024 * 1024, maximum + 1 - size))
                if not chunk:
                    break
                target.write(chunk)
                size += len(chunk)
                if size > maximum:
                    break
    finally:
        await file.close()
    return size


@router.post("", response_model=ArtifactUploadResult)
async def upload_artifact(
    principal: Principal,
    project_id: Annotated[UUID, Form()],
    file: Annotated[UploadFile, File()],
    artifact_kind: Annotated[ArtifactKind, Form()] = ArtifactKind.CALCULATION_OUTPUT,
) -> ArtifactUploadResult:
    """Store any artifact; calculation outputs are parsed frame-by-frame with MolOP."""

    filename = file.filename
    if filename is None or not filename.strip():
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
        return await ArtifactUploadService.upload(
            payload=payload,
            filename=filename,
            media_type=file.content_type or "application/octet-stream",
            artifact_kind=artifact_kind,
            project_id=project_id,
            user_id=principal.user_id,
        )
    except ProjectAccessDeniedError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except ArtifactUploadLimitError as error:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(error),
        ) from error
    except ArtifactUploadConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ArtifactUploadError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@router.post("/batch", response_model=ArtifactBatchUploadResult)
async def upload_artifact_batch(
    principal: Principal,
    project_id: Annotated[UUID, Form()],
    files: Annotated[list[UploadFile], File()],
    artifact_kind: Annotated[ArtifactKind, Form()] = ArtifactKind.CALCULATION_OUTPUT,
) -> ArtifactBatchUploadResult:
    """Upload raw files independently; MolOP probes every calculation file."""

    settings = get_settings()
    maximum = settings.max_upload_bytes
    if len(files) > settings.max_batch_files:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"upload batch exceeds the {settings.max_batch_files}-file limit",
            headers=UPLOAD_PREFLIGHT_HEADERS,
        )
    with tempfile.TemporaryDirectory(prefix="tricycle-upload-batch-") as spool_directory:
        payloads: list[ArtifactUploadPayload] = []
        total_bytes = 0
        for index, file in enumerate(files):
            filename = file.filename or ""
            media_type = file.content_type or "application/octet-stream"
            if not filename.strip():
                await file.close()
                payloads.append(
                    ArtifactUploadPayload(
                        filename="<unnamed>",
                        media_type=media_type,
                        payload=None,
                        error_code="missing_filename",
                        error_message="uploaded artifact requires a filename",
                    )
                )
                continue
            spool_path = Path(spool_directory) / f"{index:08d}.upload"
            size = await _spool_upload(file, spool_path, maximum=maximum)
            if size > maximum:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail=f"uploaded artifact exceeds the {maximum}-byte limit",
                    headers=UPLOAD_PREFLIGHT_HEADERS,
                )
            total_bytes += size
            if total_bytes > settings.max_batch_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail=f"upload batch exceeds the {settings.max_batch_bytes}-byte limit",
                    headers=UPLOAD_PREFLIGHT_HEADERS,
                )
            payloads.append(
                ArtifactUploadPayload(
                    filename=filename,
                    media_type=media_type,
                    payload=None,
                    spool_path=spool_path,
                )
            )
        try:
            return await ArtifactUploadService.upload_batch(
                files=payloads,
                artifact_kind=artifact_kind,
                project_id=project_id,
                user_id=principal.user_id,
            )
        except ProjectAccessDeniedError as error:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
        except ArtifactUploadLimitError as error:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=str(error),
            ) from error


@router.post("/validate", response_model=ArtifactValidationResult)
async def validate_artifact(
    principal: Principal,
    project_id: Annotated[UUID, Form()],
    file: Annotated[UploadFile, File()],
) -> ArtifactValidationResult:
    """Probe a raw calculation file without writing RustFS or PostgreSQL."""

    filename = file.filename
    if filename is None or not filename.strip():
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
        return await ArtifactUploadService.validate(
            payload=payload,
            filename=filename,
            project_id=project_id,
            user_id=principal.user_id,
        )
    except ProjectAccessDeniedError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except ArtifactUploadError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


@router.post("/{artifact_id}/reparse", response_model=ArtifactUploadResult)
async def reparse_artifact(
    artifact_id: UUID,
    principal: Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)],
) -> ArtifactUploadResult:
    """Reparse an available calculation artifact with the current parser identity."""

    try:
        return await ArtifactUploadService.reparse(
            artifact_id=artifact_id,
            user_id=principal.user_id,
        )
    except ProjectAccessDeniedError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except ArtifactUploadError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error


@router.patch("/{artifact_id}", response_model=ArtifactSummary)
async def update_artifact(
    artifact_id: UUID,
    payload: ArtifactMetadataUpdate,
    principal: Principal,
) -> ArtifactSummary:
    """Update project-managed artifact display metadata without replacing bytes."""

    try:
        return await ArtifactManagementService.update_metadata(
            artifact_id,
            payload,
            user_id=principal.user_id,
        )
    except ArtifactRemovalNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ProjectAccessDeniedError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except ArtifactMetadataConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.delete("/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_artifact(artifact_id: UUID, principal: Principal) -> None:
    """Retire an artifact tombstone and remove its immutable RustFS object."""

    try:
        await ArtifactManagementService.retire(artifact_id, user_id=principal.user_id)
    except ArtifactRemovalNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ProjectAccessDeniedError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except ArtifactRemovalIntegrityError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ArtifactRemovalUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error


__all__ = ["router"]
