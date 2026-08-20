"""Explicit binary access to deferred scientific-array payloads."""

from dataclasses import dataclass
from io import BytesIO
from typing import Any, cast
from uuid import UUID

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import undefer
from sqlmodel import col

from tricycle_reaction_db.application.services.authorization import ProjectPermission
from tricycle_reaction_db.application.services.query_visibility import (
    frame_id_is_visible,
    query_visibility_scope,
)
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    CalculationFrame,
    ParseRevision,
    ScientificArray,
)
from tricycle_reaction_db.db.session import session_factory


class ScientificArrayNotFoundError(LookupError):
    pass


class ScientificArrayPayloadTooLargeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ScientificArrayDownload:
    array_id: UUID
    filename: str
    content: bytes
    payload_sha256: str
    dtype: str
    shape: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ScientificArrayPreviewData:
    array_id: UUID
    kind: str
    unit: str
    dtype: str
    shape: tuple[int, ...]
    total_elements: int
    values: list[Any]
    truncated: bool


class ScientificArrayContentService:
    """Load one deferred array deliberately and serialize it as NumPy NPY."""

    @classmethod
    async def _load_array(cls, array_id: UUID) -> ScientificArray:
        scope = await query_visibility_scope(ProjectPermission.ARTIFACT_DOWNLOAD)
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(ScientificArray)
                    .options(undefer(cast(Any, ScientificArray.data)))
                    .join(
                        CalculationFrame,
                        col(ScientificArray.frame_id) == col(CalculationFrame.id),
                    )
                    .join(
                        ParseRevision,
                        col(CalculationFrame.parse_revision_id) == col(ParseRevision.id),
                    )
                    .join(
                        ArtifactFile,
                        col(ParseRevision.artifact_file_id) == col(ArtifactFile.id),
                    )
                    .where(
                        col(ScientificArray.id) == array_id,
                        frame_id_is_visible(scope, col(ScientificArray.frame_id)),
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            raise ScientificArrayNotFoundError("scientific array not found")
        return cast(ScientificArray, row)

    @classmethod
    async def load_npy(
        cls,
        array_id: UUID,
        *,
        max_bytes: int,
    ) -> ScientificArrayDownload:
        array = await cls._load_array(array_id)
        if array.array_nbytes > max_bytes:
            raise ScientificArrayPayloadTooLargeError(
                f"array payload is {array.array_nbytes} bytes; limit is {max_bytes}"
            )
        output = BytesIO()
        np.save(output, np.asarray(array.data), allow_pickle=False)
        content = output.getvalue()
        return ScientificArrayDownload(
            array_id=array_id,
            filename=f"{array.kind.value}-{array.ordinal}-{array_id}.npy",
            content=content,
            payload_sha256=array.payload_sha256,
            dtype=array.dtype,
            shape=tuple(array.shape),
        )

    @classmethod
    async def preview(
        cls,
        array_id: UUID,
        *,
        max_elements: int,
    ) -> ScientificArrayPreviewData:
        array = await cls._load_array(array_id)
        values = np.asarray(array.data).reshape(-1)
        preview_values = [_json_value(value) for value in values[:max_elements]]
        return ScientificArrayPreviewData(
            array_id=array_id,
            kind=array.kind.value,
            unit=array.unit,
            dtype=array.dtype,
            shape=tuple(array.shape),
            total_elements=int(values.size),
            values=preview_values,
            truncated=values.size > max_elements,
        )


def _json_value(value: Any) -> Any:
    """Convert NumPy scalars into JSON-safe values for the bounded preview."""
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, complex):
        return {"real": value.real, "imag": value.imag}
    if isinstance(value, float) and not np.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


__all__ = [
    "ScientificArrayContentService",
    "ScientificArrayDownload",
    "ScientificArrayPreviewData",
    "ScientificArrayNotFoundError",
    "ScientificArrayPayloadTooLargeError",
]
