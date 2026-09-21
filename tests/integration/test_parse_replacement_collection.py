"""Rollback-only checks against existing ingestion evidence in a test database."""

import os

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session

from tricycle_reaction_db.application.services.artifact_parse_replacement import (
    clear_previous_parse_results_batch,
)
from tricycle_reaction_db.core.config import get_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="requires an explicitly enabled database",
    ),
]


def test_cleanup_collects_only_unreferenced_candidates_and_rolls_back():
    engine = create_engine(get_settings().database_url)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                    artifact = session.execute(
                        text(
                            "SELECT r.artifact_file_id FROM transition_state_inference i "
                            "JOIN parse_revision r ON r.id=i.parse_revision_id "
                            "WHERE i.mapped_reaction_id IS NOT NULL LIMIT 1"
                        )
                    ).scalar_one_or_none()
                    if artifact is None:
                        pytest.skip("requires one ingested reaction fixture")
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
                    summary = clear_previous_parse_results_batch(
                        session, artifact_file_ids=[artifact]
                    )
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
    finally:
        engine.dispose()
