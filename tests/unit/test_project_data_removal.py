from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql
from sqlmodel import col, select

from tricycle_reaction_db.application.services import project_data_removal as removal
from tricycle_reaction_db.db.models import ArtifactFile, UnitsTsDatasetExportJob

PROJECT_ID = UUID("00000000-0000-7000-8000-000000000301")
ARTIFACT_ID = UUID("00000000-0000-7000-8000-000000000302")
EXPORT_JOB_ID = UUID("00000000-0000-7000-8000-000000000303")


class _Result:
    def __init__(self, rows: list[tuple[object, ...]] | None = None, *, rowcount: int = 0):
        self._rows = rows or []
        self.rowcount = rowcount

    def all(self) -> list[tuple[object, ...]]:
        return self._rows


@pytest.mark.asyncio
async def test_project_cleanup_collects_export_objects_and_artifact_objects() -> None:
    class _Session:
        def __init__(self) -> None:
            self._responses = [
                [("artifact-bucket", "artifact-key", "artifact-version")],
                [("export-bucket", "dataset-key"), (None, None)],
            ]

        async def exec(self, _statement: object) -> _Result:
            return _Result(self._responses.pop(0))

    references = await removal._project_object_references(
        _Session(),  # type: ignore[arg-type]
        PROJECT_ID,
        select(col(ArtifactFile.id)).where(col(ArtifactFile.id) == ARTIFACT_ID),
    )

    assert references == (
        removal._ObjectReference(
            bucket="artifact-bucket",
            object_key="artifact-key",
            version_id="artifact-version",
        ),
        removal._ObjectReference(
            bucket="export-bucket",
            object_key="dataset-key",
            version_id=None,
        ),
    )


@pytest.mark.asyncio
async def test_project_cleanup_deletes_export_job_rows_only_for_the_target_project() -> None:
    class _Session:
        statement: object | None = None

        async def exec(self, statement: object) -> _Result:
            self.statement = statement
            return _Result(rowcount=2)

    session = _Session()

    deleted_count = await removal._delete_project_dataset_export_jobs(
        session,  # type: ignore[arg-type]
        PROJECT_ID,
    )

    assert deleted_count == 2
    statement = session.statement
    assert statement is not None
    assert statement.table.name == UnitsTsDatasetExportJob.__table__.name  # type: ignore[attr-defined]
    compiled = statement.compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    assert PROJECT_ID in compiled.params.values()
    assert "project_id" in str(statement.whereclause)  # type: ignore[attr-defined]


def test_project_cleanup_deletes_collected_export_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted_by_bucket: list[tuple[str, list[tuple[str, str | None]]]] = []

    class _Settings:
        def __init__(self, bucket: str = "default") -> None:
            self.bucket = bucket

        def model_copy(self, *, update: dict[str, str]) -> _Settings:
            return _Settings(bucket=update["bucket"])

    class _ObjectStore:
        def __init__(self, settings: _Settings) -> None:
            self.bucket = settings.bucket

        def __enter__(self) -> _ObjectStore:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def delete_many(self, objects: list[tuple[str, str | None]]) -> tuple[int, int]:
            deleted_by_bucket.append((self.bucket, objects))
            return len(objects), 0

    monkeypatch.setattr(removal, "RustFSObjectStore", _ObjectStore)

    deleted_count, pending_count, error_message = removal._delete_objects(
        _Settings(),  # type: ignore[arg-type]
        (
            removal._ObjectReference(
                bucket="export-bucket",
                object_key="dataset-key",
                version_id=None,
            ),
        ),
    )

    assert (deleted_count, pending_count, error_message) == (1, 0, None)
    assert deleted_by_bucket == [("export-bucket", [("dataset-key", None)])]
