import os
from collections.abc import AsyncIterator, Callable
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, event
from sqlmodel import col

from tricycle_reaction_db.application.services.queries import ArtifactQueryService
from tricycle_reaction_db.db.models import ArtifactFile
from tricycle_reaction_db.db.session import engine, session_factory
from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    ArtifactVisibility,
    StorageStatus,
)
from tricycle_reaction_db.domain.identity import SYSTEM_PROJECT_ID, SYSTEM_USER_ID

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


@pytest_asyncio.fixture
async def cursor_artifacts() -> AsyncIterator[tuple[str, set[UUID]]]:
    suffix = uuid4().hex
    artifact_ids = {uuid4() for _ in range(3)}
    async with session_factory() as session:
        session.add_all(
            [
                ArtifactFile(
                    id=artifact_id,
                    project_id=SYSTEM_PROJECT_ID,
                    created_by_user_id=SYSTEM_USER_ID,
                    visibility=ArtifactVisibility.PUBLIC,
                    bucket="cursor-test",
                    object_key=f"cursor-test/{suffix}/{artifact_id}",
                    content_sha256=sha256(f"{suffix}:{artifact_id}".encode()).hexdigest(),
                    size_bytes=1,
                    original_filename=f"cursor-{suffix}-{index}.log",
                    media_type="text/plain",
                    artifact_kind=ArtifactKind.AUXILIARY,
                    storage_status=StorageStatus.AVAILABLE,
                )
                for index, artifact_id in enumerate(artifact_ids)
            ]
        )
        await session.commit()
    try:
        yield suffix, artifact_ids
    finally:
        async with session_factory() as session:
            await session.execute(
                delete(ArtifactFile).where(col(ArtifactFile.id).in_(artifact_ids))
            )
            await session.commit()


def _statement_capture() -> tuple[
    list[str],
    Callable[[object, object, str, object, object, bool], None],
]:
    statements: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    return statements, capture


@pytest.mark.asyncio
async def test_artifact_keyset_first_page_skips_count_and_cursor_pages_skip_offset(
    cursor_artifacts: tuple[str, set[UUID]],
) -> None:
    suffix, expected_ids = cursor_artifacts
    statements, capture = _statement_capture()
    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        first_page = await ArtifactQueryService.list_artifacts(
            original_filename_contains=suffix,
            limit=1,
            offset=0,
            cursor="",
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    assert len(statements) == 1
    assert statements[0].lstrip().upper().startswith("SELECT")
    assert "COUNT(" not in statements[0].upper()
    assert first_page.page.total == -1
    assert first_page.page.next_cursor is not None

    observed_ids = {first_page.items[0].id}
    cursor = first_page.page.next_cursor
    offset = 1
    while cursor is not None:
        statements.clear()
        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        try:
            page = await ArtifactQueryService.list_artifacts(
                original_filename_contains=suffix,
                limit=1,
                offset=offset,
                cursor=cursor,
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", capture)
        assert len(statements) == 1
        assert " OFFSET " not in statements[0].upper()
        assert page.page.total == -1
        observed_ids.update(item.id for item in page.items)
        cursor = page.page.next_cursor
        offset += 1

    assert observed_ids == expected_ids


@pytest.mark.asyncio
async def test_artifact_offset_compatibility_path_keeps_exact_total(
    cursor_artifacts: tuple[str, set[UUID]],
) -> None:
    suffix, expected_ids = cursor_artifacts
    statements, capture = _statement_capture()
    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        page = await ArtifactQueryService.list_artifacts(
            original_filename_contains=suffix,
            limit=10,
            offset=0,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    assert len(statements) == 2
    assert any("COUNT(" in statement.upper() for statement in statements)
    assert page.page.total == len(expected_ids)
    assert {item.id for item in page.items} == expected_ids
