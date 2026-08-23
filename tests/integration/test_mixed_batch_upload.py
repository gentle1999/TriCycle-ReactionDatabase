import asyncio
import gzip
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from tricycle_reaction_db.application.services import artifact_uploads as uploads
from tricycle_reaction_db.application.services.artifact_uploads import (
    ArtifactUploadError,
    ArtifactUploadPayload,
    ArtifactUploadService,
)
from tricycle_reaction_db.db.models import ArtifactIngestion, ParseRevision
from tricycle_reaction_db.db.session import engine
from tricycle_reaction_db.domain.enums import ArtifactIngestionStatus, ArtifactKind
from tricycle_reaction_db.domain.identity import DEVELOPMENT_USER_ID, SYSTEM_PROJECT_ID
from tricycle_reaction_db.storage.rustfs import RustFSObjectStore, RustFSSettings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.rustfs,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1"
        or os.getenv("TRICYCLE_RUN_RUSTFS_TESTS") != "1",
        reason="set database and RustFS integration flags to run mixed upload tests",
    ),
]

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures"
GAUSSIAN_FIXTURE = (
    FIXTURE_ROOT / "da_bench_minimal/complete_set/000000000000_000000403256/00/ts/"
    "000000000000_000000403256_00_conf_01_ts.43b3faa8fcc9.log.gz"
)
ORCA_FIXTURE = FIXTURE_ROOT / "qm/minimal_orca_water_sp.orcaout"


@pytest.fixture(autouse=True)
def close_molop_pool_after_test() -> None:
    yield
    asyncio.run(uploads.close_molop_process_pool())


async def _ingestion_snapshot(
    factory: async_sessionmaker[AsyncSession],
    *,
    ingestion_id: UUID,
    artifact_id: UUID,
) -> tuple[object, ...]:
    async with factory() as session:
        ingestion = await session.get(ArtifactIngestion, ingestion_id)
        assert ingestion is not None
        revisions = (
            await session.exec(
                select(ParseRevision)
                .where(ParseRevision.artifact_file_id == artifact_id)
                .order_by(col(ParseRevision.revision_number))
            )
        ).all()
        return (
            ingestion.status,
            ingestion.source_frame_count,
            ingestion.transition_state_frame_count,
            ingestion.completed_at,
            ingestion.error_code,
            ingestion.error_message,
            ingestion.parser_metadata,
            tuple((revision.id, revision.revision_number) for revision in revisions),
        )


@pytest.mark.asyncio
async def test_mixed_raw_batch_persists_independently_and_failed_reparse_preserves_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_marker = str(uuid4()).encode()
    gaussian_payload = gzip.decompress(GAUSSIAN_FIXTURE.read_bytes()) + b"\n" + run_marker + b"\n"
    orca_payload = ORCA_FIXTURE.read_bytes() + b"\n" + run_marker + b"\n"
    invalid_payload = b"this is not a supported quantum chemistry output\n" + run_marker + b"\n"
    written_keys: set[str] = set()
    parsed_gaussian: list[uploads._ParsedArtifact] = []
    completed_result_queries: list[str] = []
    individual_ingestion_reads: list[str] = []
    individual_revision_id_reads: list[str] = []
    parse_revision_selects: list[str] = []
    original_parse_batch = uploads._parse_calculation_outputs_batch
    original_store = ArtifactUploadService._store_payload

    def track_parse_batch(
        files: list[tuple[bytes, str]], *, n_jobs: int
    ) -> dict[int, uploads._ParsedArtifact | Exception]:
        parsed_items = original_parse_batch(files, n_jobs=n_jobs)
        parsed_gaussian.extend(
            parsed
            for parsed in parsed_items.values()
            if isinstance(parsed, uploads._ParsedArtifact) and parsed.source_format == "g16log"
        )
        return parsed_items

    def track_store(
        settings: RustFSSettings,
        object_key: str,
        payload: bytes,
        media_type: str,
    ) -> object:
        stored = original_store(settings, object_key, payload, media_type)
        written_keys.add(object_key)
        return stored

    monkeypatch.setattr(uploads, "_parse_calculation_outputs_batch", track_parse_batch)
    monkeypatch.setattr(ArtifactUploadService, "_store_payload", staticmethod(track_store))

    def capture_completed_result_query(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = statement.lower().replace('"', "")
        if "from artifact_ingestion" in normalized and "artifact_ingestion.id =" in normalized:
            individual_ingestion_reads.append(statement)
        compact = "".join(normalized.split())
        if "from parse_revision" in normalized:
            parse_revision_selects.append(statement)
        if "selectparse_revision.idfromparse_revision" in compact:
            individual_revision_id_reads.append(statement)
        if (
            (
                "from artifact_ingestion" in normalized
                and "left outer join artifact_file" in normalized
            )
            or (
                "from transition_state_inference" in normalized
                and "artifact_ingestion_id in" in normalized
            )
        ):
            completed_result_queries.append(statement)

    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            isolated_factory = async_sessionmaker(
                bind=connection,
                class_=AsyncSession,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            monkeypatch.setattr(uploads, "session_factory", isolated_factory)
            try:
                event.listen(
                    engine.sync_engine,
                    "before_cursor_execute",
                    capture_completed_result_query,
                )
                try:
                    batch = await ArtifactUploadService.upload_batch(
                        files=[
                            ArtifactUploadPayload(
                                "unstructured-gaussian.bin",
                                "application/octet-stream",
                                gaussian_payload,
                            ),
                            ArtifactUploadPayload(
                                "unstructured-invalid.bin",
                                "application/octet-stream",
                                invalid_payload,
                            ),
                            ArtifactUploadPayload(
                                "unstructured-orca.bin",
                                "application/octet-stream",
                                orca_payload,
                            ),
                        ],
                        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                        project_id=SYSTEM_PROJECT_ID,
                        user_id=DEVELOPMENT_USER_ID,
                    )
                finally:
                    event.remove(
                        engine.sync_engine,
                        "before_cursor_execute",
                        capture_completed_result_query,
                    )

                assert (batch.total_count, batch.succeeded_count, batch.failed_count) == (3, 2, 1)
                gaussian_item, invalid_item, orca_item = batch.items
                assert gaussian_item.succeeded is True
                assert gaussian_item.result is not None
                assert gaussian_item.result.ingestion_status is ArtifactIngestionStatus.SUCCEEDED
                assert gaussian_item.result.source_frame_count == 23
                assert gaussian_item.result.transition_state_frame_count == 1
                assert gaussian_item.result.inferred_reaction_count == 1
                assert invalid_item.succeeded is False
                assert invalid_item.result is not None
                assert invalid_item.result.ingestion_status is ArtifactIngestionStatus.FAILED
                assert invalid_item.error_code == "molop_parse_failed"
                assert orca_item.succeeded is True
                assert orca_item.result is not None
                assert orca_item.result.ingestion_status is ArtifactIngestionStatus.SUCCEEDED
                assert orca_item.result.source_frame_count == 1
                assert orca_item.result.transition_state_frame_count == 0
                assert batch.source_frame_count == 24
                assert batch.transition_state_frame_count == 1
                assert batch.inferred_reaction_count == 1
                # One ingestion/artifact preload, then one ingestion/artifact
                # result read and one inference result read cover every item.
                assert len(completed_result_queries) == 3
                assert len(parsed_gaussian) == 1
                expected_timing_phases = {
                    "validate_budget_ms",
                    "authorize_ms",
                    "prepare_db_ms",
                    "storage_ms",
                    "storage_db_ms",
                    "parse_ms",
                    "persist_preload_db_ms",
                    "persist_write_db_ms",
                    "persist_result_db_ms",
                    "persist_commit_db_ms",
                    "persist_db_ms",
                    "total_ms",
                }
                assert expected_timing_phases <= batch.timings_ms.keys()
                assert all(
                    batch.timings_ms[phase] >= 0 for phase in expected_timing_phases
                )
                assert not individual_ingestion_reads
                assert not individual_revision_id_reads
                assert len(parse_revision_selects) == 2
                assert all(
                    "artifact_file_id IN" in statement for statement in parse_revision_selects
                )

                ingestion_id = gaussian_item.result.ingestion_id
                artifact_id = gaussian_item.result.artifact_id
                assert ingestion_id is not None
                before = await _ingestion_snapshot(
                    isolated_factory,
                    ingestion_id=ingestion_id,
                    artifact_id=artifact_id,
                )

                async def fail_parse(*_: object, **__: object) -> uploads._ParsedArtifact:
                    raise RuntimeError("forced reparse failure")

                monkeypatch.setattr(uploads, "_run_molop_file_parser", fail_parse)
                with pytest.raises(ArtifactUploadError, match="forced reparse failure"):
                    await ArtifactUploadService.reparse(
                        artifact_id=artifact_id,
                        user_id=DEVELOPMENT_USER_ID,
                    )
                assert (
                    await _ingestion_snapshot(
                        isolated_factory,
                        ingestion_id=ingestion_id,
                        artifact_id=artifact_id,
                    )
                    == before
                )

                monkeypatch.setattr(
                    uploads,
                    "_run_molop_file_parser",
                    lambda *_args, **_kwargs: asyncio.sleep(0, result=parsed_gaussian[0]),
                )

                def fail_persistence(*_: object, **__: object) -> tuple[object, bool]:
                    raise RuntimeError("forced reparse persistence failure")

                monkeypatch.setattr(uploads, "_persist_parsed_artifact", fail_persistence)
                with pytest.raises(
                    ArtifactUploadError,
                    match="forced reparse persistence failure",
                ):
                    await ArtifactUploadService.reparse(
                        artifact_id=artifact_id,
                        user_id=DEVELOPMENT_USER_ID,
                    )
                assert (
                    await _ingestion_snapshot(
                        isolated_factory,
                        ingestion_id=ingestion_id,
                        artifact_id=artifact_id,
                    )
                    == before
                )
            finally:
                await transaction.rollback()
    finally:
        settings = RustFSSettings()
        with RustFSObjectStore(settings) as store:
            for object_key in written_keys:
                if store.exists(object_key):
                    store.delete(object_key)


@pytest.mark.asyncio
async def test_batch_prepare_sql_round_trips_do_not_scale_with_new_file_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New content uses set-based locks/lookups and executemany inserts."""

    async with engine.connect() as connection:
        transaction = await connection.begin()
        isolated_factory = async_sessionmaker(
            bind=connection,
            class_=AsyncSession,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        monkeypatch.setattr(uploads, "session_factory", isolated_factory)
        try:
            async def capture_prepare(file_count: int) -> list[str]:
                marker = str(uuid4()).encode()
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

                event.listen(engine.sync_engine, "before_cursor_execute", capture)
                try:
                    prepared, items = await ArtifactUploadService._prepare_upload_batch(
                        files=[
                            ArtifactUploadPayload(
                                f"prepare-{index}.log",
                                "text/plain",
                                marker + str(index).encode(),
                            )
                            for index in range(file_count)
                        ],
                        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                        project_id=SYSTEM_PROJECT_ID,
                        user_id=DEVELOPMENT_USER_ID,
                    )
                finally:
                    event.remove(engine.sync_engine, "before_cursor_execute", capture)
                assert not items
                assert len(prepared) == file_count
                return [
                    statement
                    for statement in statements
                    if not statement.lstrip().upper().startswith(("SAVEPOINT", "RELEASE"))
                ]

            single = await capture_prepare(1)
            batch = await capture_prepare(4)
            assert len(batch) == len(single)
            assert sum("INSERT INTO artifact_file" in statement for statement in batch) == 1
            assert sum("INSERT INTO artifact_ingestion" in statement for statement in batch) == 1
        finally:
            await transaction.rollback()
