"""Rollback-only cleanup checks with independently ingested source fixtures."""

import gzip
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session

from tricycle_reaction_db.application.dtos import ArtifactFileRecord
from tricycle_reaction_db.application.services.artifact_parse_replacement import (
    clear_previous_parse_results_batch,
)
from tricycle_reaction_db.application.services.artifact_uploads import (
    _create_pending_ingestion,
    _parse_calculation_output,
    _persist_parsed_artifact,
)
from tricycle_reaction_db.application.services.catalog import persist_artifact_file
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import Project
from tricycle_reaction_db.domain.enums import ArtifactKind, ArtifactVisibility, StorageStatus
from tricycle_reaction_db.domain.identity import DEVELOPMENT_USER_ID, SYSTEM_ORGANIZATION_ID

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="requires an explicitly enabled database",
    ),
]


@contextmanager
def _ingested_connection():
    fixture = (
        Path(__file__).parents[1]
        / "fixtures/da_bench_minimal/complete_set/000000000000_000000403256/00/ts"
        / "000000000000_000000403256_00_conf_01_ts.43b3faa8fcc9.log.gz"
    )
    payload = gzip.decompress(fixture.read_bytes()) + f"\n{uuid4()}\n".encode()
    parsed = _parse_calculation_output(payload, fixture.name.removesuffix(".gz"))
    engine = create_engine(get_settings().database_url)
    connection = engine.connect()
    outer = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            project_id = uuid4()
            session.add(
                Project(
                    id=project_id,
                    organization_id=SYSTEM_ORGANIZATION_ID,
                    name="Cleanup fixture",
                    slug=f"cleanup-{project_id.hex}",
                )
            )
            session.flush()
            now = datetime.now(UTC)
            digest = sha256(payload).hexdigest()
            artifact = persist_artifact_file(
                session,
                ArtifactFileRecord(
                    project_id=project_id,
                    created_by_user_id=DEVELOPMENT_USER_ID,
                    visibility=ArtifactVisibility.PROJECT,
                    bucket="cleanup-fixture",
                    object_key=digest,
                    content_sha256=digest,
                    size_bytes=len(payload),
                    original_filename=fixture.name.removesuffix(".gz"),
                    media_type="text/plain",
                    artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                    storage_status=StorageStatus.AVAILABLE,
                    storage_verified_at=now,
                ),
            )
            ingestion, _ = _create_pending_ingestion(session, artifact=artifact, started_at=now)
            _persist_parsed_artifact(
                session, ingestion_id=ingestion.id, parsed=parsed, started_at=now, completed_at=now
            )
            session.commit()
            artifact_id = artifact.id
        yield connection, artifact_id
    finally:
        outer.rollback()
        connection.close()
        engine.dispose()


def test_cleanup_collects_only_unreferenced_candidates_and_rolls_back():
    with _ingested_connection() as (connection, fixture_artifact):
        transaction = connection.begin_nested()
        try:
            with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                artifact = session.execute(
                    text(
                        "SELECT r.artifact_file_id FROM transition_state_inference i "
                        "JOIN parse_revision r ON r.id=i.parse_revision_id "
                        "WHERE i.mapped_reaction_id IS NOT NULL AND r.artifact_file_id=:artifact"
                    ),
                    {"artifact": fixture_artifact},
                ).scalar_one()
                assert artifact == fixture_artifact
                candidates = set(
                    session.execute(
                        text(
                            "SELECT DISTINCT f.geometry_id FROM calculation_frame f "
                            "JOIN parse_revision r ON r.id=f.parse_revision_id "
                            "WHERE r.artifact_file_id=:artifact"
                        ),
                        {"artifact": artifact},
                    ).scalars()
                )
                shared = set(
                    session.execute(
                        text(
                            "SELECT DISTINCT f.geometry_id FROM calculation_frame f "
                            "JOIN parse_revision r ON r.id=f.parse_revision_id "
                            "WHERE r.artifact_file_id<>:artifact "
                            "AND f.geometry_id=ANY(:candidates)"
                        ),
                        {"artifact": artifact, "candidates": list(candidates)},
                    ).scalars()
                )
                summary = clear_previous_parse_results_batch(session, artifact_file_ids=[artifact])
                assert summary.deleted_revision_count > 0
                remaining = set(
                    session.execute(
                        text("SELECT id FROM geometry WHERE id=ANY(:candidates)"),
                        {"candidates": list(candidates)},
                    ).scalars()
                )
                assert shared <= remaining
                assert len(candidates - remaining) == summary.deleted_geometry_count
                assert (
                    session.execute(
                        text(
                            "SELECT count(*) FROM geometry g WHERE g.id=ANY(:candidates) "
                            "AND NOT EXISTS (SELECT 1 FROM calculation_frame f "
                            "WHERE f.geometry_id=g.id) "
                            "AND NOT EXISTS (SELECT 1 FROM mapped_reaction_node_geometry b "
                            "WHERE b.geometry_id=g.id)"
                        ),
                        {"candidates": list(candidates)},
                    ).scalar_one()
                    == 0
                )
                assert (
                    clear_previous_parse_results_batch(
                        session, artifact_file_ids=[artifact]
                    ).deleted_revision_count
                    == 0
                )
        finally:
            transaction.rollback()
        assert (
            connection.execute(
                text("SELECT count(*) FROM parse_revision WHERE artifact_file_id=:artifact"),
                {"artifact": artifact},
            ).scalar_one()
            > 0
        )


def test_cleanup_collects_detached_ts_geometry_for_unreferenced_reaction_and_rolls_back():
    with _ingested_connection() as (connection, fixture_artifact):
        transaction = connection.begin_nested()
        try:
            with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                candidate = session.execute(
                    text(
                        "SELECT r.artifact_file_id, i.logical_reaction_id, "
                        "i.mapped_reaction_id "
                        "FROM transition_state_inference i "
                        "JOIN parse_revision r ON r.id=i.parse_revision_id "
                        "WHERE r.artifact_file_id=:artifact AND i.status='succeeded' "
                        "AND i.logical_reaction_id IS NOT NULL "
                        "AND i.mapped_reaction_id IS NOT NULL "
                        "AND (SELECT count(*) FROM transition_state_inference other "
                        "WHERE other.logical_reaction_id=i.logical_reaction_id)=1 "
                        "AND (SELECT count(*) FROM transition_state_inference other "
                        "WHERE other.mapped_reaction_id=i.mapped_reaction_id)=1 "
                        "AND EXISTS (SELECT 1 FROM mapped_reaction_node n "
                        "JOIN mapped_reaction_node_geometry g "
                        "ON g.mapped_reaction_node_id=n.id "
                        "WHERE n.mapped_reaction_id=i.mapped_reaction_id) "
                        "LIMIT 1"
                    ),
                    {"artifact": fixture_artifact},
                ).one()
                artifact_id, logical_reaction_id, mapped_reaction_id = candidate
                geometry_ids = set(
                    session.execute(
                        text(
                            "SELECT g.geometry_id FROM mapped_reaction_node_geometry g "
                            "JOIN mapped_reaction_node n "
                            "ON n.id=g.mapped_reaction_node_id "
                            "WHERE n.mapped_reaction_id=:mapped_reaction_id"
                        ),
                        {"mapped_reaction_id": mapped_reaction_id},
                    ).scalars()
                )

                summary = clear_previous_parse_results_batch(
                    session,
                    artifact_file_ids=[artifact_id],
                )

                assert summary.deleted_node_geometry_count > 0
                assert summary.deleted_mapped_reaction_count > 0
                assert summary.deleted_logical_reaction_count > 0
                assert (
                    session.execute(
                        text("SELECT count(*) FROM logical_reaction WHERE id=:id"),
                        {"id": logical_reaction_id},
                    ).scalar_one()
                    == 0
                )
                assert (
                    session.execute(
                        text("SELECT count(*) FROM mapped_reaction WHERE id=:id"),
                        {"id": mapped_reaction_id},
                    ).scalar_one()
                    == 0
                )
                assert (
                    session.execute(
                        text(
                            "SELECT count(*) FROM geometry g WHERE g.id=ANY(:geometry_ids) "
                            "AND NOT EXISTS (SELECT 1 FROM calculation_frame f "
                            "WHERE f.geometry_id=g.id) "
                            "AND NOT EXISTS (SELECT 1 FROM mapped_reaction_node_geometry b "
                            "WHERE b.geometry_id=g.id)"
                        ),
                        {"geometry_ids": list(geometry_ids)},
                    ).scalar_one()
                    == 0
                )
        finally:
            transaction.rollback()
