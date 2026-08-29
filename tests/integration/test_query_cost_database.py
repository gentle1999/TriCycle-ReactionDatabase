import logging
import os
from time import perf_counter
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from tricycle_reaction_db.application.query_cost import QueryStatementTimeout
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db import session as session_module
from tricycle_reaction_db.db.session import dispose_engine, session_factory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


@pytest.mark.asyncio
async def test_postgresql_timeout_cancels_statement_and_releases_connection() -> None:
    started_at = perf_counter()
    async with session_factory() as session:
        await session.execute(text("SET LOCAL statement_timeout = 50"))
        with pytest.raises(QueryStatementTimeout, match="query_timeout"):
            await session.execute(text("SELECT pg_sleep(0.25)"))
        await session.rollback()
        assert int((await session.execute(text("SELECT 1"))).scalar_one()) == 1

    assert perf_counter() - started_at < 1.0
    await dispose_engine()


@pytest.mark.asyncio
async def test_slow_query_log_omits_bound_values(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module.settings, "slow_query_threshold_ms", 1)
    sensitive = "query-secret-must-not-be-logged"
    caplog.set_level(logging.WARNING, logger=session_module.__name__)
    async with session_factory() as session:
        await session.execute(
            text("SELECT pg_sleep(:delay), CAST(:sensitive AS text)"),
            {"delay": 0.01, "sensitive": sensitive},
        )

    record = next(
        record for record in caplog.records if record.message.startswith("slow database query")
    )
    query_elapsed_ms = float(record.__dict__["query_elapsed_ms"])
    query_statement = str(record.__dict__["query_statement"])
    assert query_elapsed_ms >= 1
    assert "pg_sleep" in query_statement
    assert sensitive not in query_statement
    assert sensitive not in caplog.text
    await dispose_engine()


def _plan_nodes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = [plan]
    for child in plan.get("Plans", []):
        nodes.extend(_plan_nodes(child))
    return nodes


def test_representative_query_plans_execute_with_buffers_and_expected_indexes() -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    statements = {
        "formula exact counts": (
            "SELECT id FROM molecular_formula "
            "WHERE element_count_tokens @> ARRAY['1:2', '6:1']::text[]"
        ),
        "topology SMARTS": (
            "SELECT id FROM molecular_topology WHERE mol @> qmol_from_smarts('C'::cstring)"
        ),
        "topology fingerprint KNN": (
            "SELECT id FROM molecular_topology ORDER BY "
            "morgan_bfp <%> morganbv_fp(mol_from_smiles('C'::cstring), 2) LIMIT 20"
        ),
        "reaction SMARTS": (
            "SELECT id FROM mapped_reaction WHERE reaction @> "
            "reaction_from_smarts('[C:1]=[C:2]>>[C:1]-[C:2]'::cstring)"
        ),
        "reaction fingerprint KNN": (
            "SELECT id FROM mapped_reaction ORDER BY reaction_structural_bfp <%> "
            "reaction_structural_bfp(reaction_from_smiles("
            "'[C:1]=[C:2]>>[C:1]-[C:2]'::cstring), 5) LIMIT 20"
        ),
        "geometry topology": (
            "SELECT id FROM geometry WHERE topology_id = (SELECT topology_id FROM geometry LIMIT 1)"
        ),
        "frame topology derivation": (
            "SELECT id FROM calculation_frame WHERE topology_derivation_id = "
            "(SELECT topology_derivation_id FROM calculation_frame LIMIT 1)"
        ),
        "artifact filename contains": (
            "SELECT id FROM artifact_file WHERE original_filename ILIKE '%log%'"
        ),
        "artifact project keyset": (
            "SELECT id FROM artifact_file "
            "WHERE project_id = '00000000-0000-7000-8000-000000000201' "
            "AND storage_status = 'available' "
            "ORDER BY created_at DESC, id LIMIT 50"
        ),
        "active session listing": (
            "SELECT id FROM auth_session "
            "WHERE user_id = '00000000-0000-7000-8000-000000000002' "
            "AND revoked_at IS NULL AND expires_at > now() "
            "ORDER BY last_seen_at DESC LIMIT 50"
        ),
    }
    expected_indexes = {
        "formula exact counts": {"ix_molecular_formula_element_count_tokens_gin"},
        "topology SMARTS": {"ix_molecular_topology_mol_gist"},
        "topology fingerprint KNN": {"ix_molecular_topology_morgan_bfp_gist"},
        "reaction SMARTS": {"ix_mapped_reaction_reaction_gist"},
        "reaction fingerprint KNN": {"ix_mapped_reaction_structural_bfp_gist"},
        "geometry topology": {"ix_geometry_topology_id"},
        "frame topology derivation": {"ix_calculation_frame_topology_derivation_id"},
        "artifact filename contains": {"ix_artifact_file_original_filename_trgm"},
        # Both plans are valid on tiny fixtures: PostgreSQL may scan the
        # status/time index and filter project_id when almost every row is
        # in the system project. The scope-leading index definition is
        # asserted separately below.
        "artifact project keyset": {
            "ix_artifact_file_project_status_created_id",
            "ix_artifact_file_storage_status_created_at",
        },
        "active session listing": {"ix_auth_session_user_active_last_seen"},
    }
    try:
        with engine.connect() as connection:
            connection.execute(text("SET LOCAL enable_seqscan = off"))
            for label, statement in statements.items():
                # Keep the representative keyset plan on its scope-leading
                # index; bitmap paths obscure the index contract on tiny
                # fixtures. GIN/RDKit predicates still need bitmap paths.
                connection.execute(
                    text(
                        "SET LOCAL enable_bitmapscan = "
                        + ("off" if label == "artifact project keyset" else "on")
                    )
                )
                if label in {"artifact project keyset", "active session listing"}:
                    connection.execute(text("SET LOCAL enable_sort = off"))
                explain = connection.execute(
                    text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {statement}")
                ).scalar_one()[0]
                nodes = _plan_nodes(explain["Plan"])
                index_names = {
                    str(node["Index Name"]) for node in nodes if node.get("Index Name") is not None
                }
                assert expected_indexes[label] & index_names, (
                    f"{label} indexes: {sorted(index_names)}; "
                    f"expected one of {sorted(expected_indexes[label])}"
                )
                assert float(explain["Execution Time"]) < 500.0, (
                    f"{label} took {explain['Execution Time']} ms"
                )
                assert any(
                    "Shared Hit Blocks" in node or "Shared Read Blocks" in node for node in nodes
                ), f"{label} did not report buffer activity"
            project_index = connection.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname = current_schema() "
                    "AND indexname = 'ix_artifact_file_project_status_created_id'"
                )
            ).scalar_one()
            assert "(project_id, storage_status, created_at, id)" in project_index
    finally:
        engine.dispose()
