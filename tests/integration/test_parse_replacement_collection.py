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


def test_cleanup_collects_detached_ts_geometry_for_unreferenced_reaction_and_rolls_back():
    engine = create_engine(get_settings().database_url)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                    candidate = session.execute(
                        text(
                            "SELECT r.artifact_file_id, i.logical_reaction_id, "
                            "i.mapped_reaction_id "
                            "FROM transition_state_inference i "
                            "JOIN parse_revision r ON r.id=i.parse_revision_id "
                            "WHERE i.status='succeeded' AND i.logical_reaction_id IS NOT NULL "
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
                        )
                    ).one_or_none()
                    if candidate is None:
                        pytest.skip("requires an unshared inferred reaction with TS geometry")
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
    finally:
        engine.dispose()
