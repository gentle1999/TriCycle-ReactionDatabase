from io import BytesIO
from types import SimpleNamespace
from uuid import UUID

import numpy as np
import pytest

from tricycle_reaction_db.application.services.scientific_array_content import (
    ScientificArrayContentService,
    ScientificArrayPayloadTooLargeError,
)


@pytest.mark.asyncio
async def test_npy_download_supports_unlimited_bytes_and_explicit_transport_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    array_id = UUID("00000000-0000-7000-8000-000000000790")
    project_id = UUID("00000000-0000-7000-8000-000000000792")
    data = np.arange(128 * 1024, dtype=np.float64)

    async def load_array(requested_array_id: UUID, project_id: UUID) -> SimpleNamespace:
        assert requested_array_id == array_id
        return SimpleNamespace(
            data=data,
            array_nbytes=data.nbytes,
            kind=SimpleNamespace(value="coordinates"),
            ordinal=0,
            payload_sha256="array-hash",
            unit="angstrom",
            dtype=str(data.dtype),
            shape=data.shape,
        )

    monkeypatch.setattr(ScientificArrayContentService, "_load_array", staticmethod(load_array))
    download = await ScientificArrayContentService.load_npy(
        array_id, max_bytes=None, project_id=project_id
    )
    assert len(download.content) > 512 * 1024
    np.testing.assert_array_equal(np.load(BytesIO(download.content), allow_pickle=False), data)

    with pytest.raises(ScientificArrayPayloadTooLargeError):
        await ScientificArrayContentService.load_npy(
            array_id, max_bytes=512 * 1024, project_id=project_id
        )
