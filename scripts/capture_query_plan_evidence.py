"""Capture representative production query plans as versioned JSON evidence."""

from __future__ import annotations

import argparse
import json
import socket
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast

from sqlalchemy import Connection, create_engine, text

from tricycle_reaction_db.core.config import get_settings

QUERY_PLAN_EVIDENCE_SCHEMA_VERSION = "query-plan-evidence-v1"


class QuerySpec(TypedDict):
    statement: str
    expected_indexes: set[str]


QUERY_SPECS: dict[str, QuerySpec] = {
    "formula_exact_counts": {
        "statement": (
            "SELECT id FROM molecular_formula "
            "WHERE element_count_tokens @> ARRAY['1:2', '6:1']::text[]"
        ),
        "expected_indexes": {"ix_molecular_formula_element_count_tokens_gin"},
    },
    "topology_smarts": {
        "statement": (
            "SELECT id FROM molecular_topology WHERE mol @> qmol_from_smarts('C'::cstring)"
        ),
        "expected_indexes": {"ix_molecular_topology_mol_gist"},
    },
    "topology_fingerprint_knn": {
        "statement": (
            "SELECT id FROM molecular_topology ORDER BY "
            "morgan_bfp <%> morganbv_fp(mol_from_smiles('C'::cstring), 2) LIMIT 20"
        ),
        "expected_indexes": {"ix_molecular_topology_morgan_bfp_gist"},
    },
    "reaction_smarts": {
        "statement": (
            "SELECT id FROM mapped_reaction WHERE reaction @> "
            "reaction_from_smarts('[C:1]=[C:2]>>[C:1]-[C:2]'::cstring)"
        ),
        "expected_indexes": {"ix_mapped_reaction_reaction_gist"},
    },
    "reaction_fingerprint_knn": {
        "statement": (
            "SELECT id FROM mapped_reaction ORDER BY reaction_structural_bfp <%> "
            "reaction_structural_bfp(reaction_from_smiles("
            "'[C:1]=[C:2]>>[C:1]-[C:2]'::cstring), 5) LIMIT 20"
        ),
        "expected_indexes": {"ix_mapped_reaction_structural_bfp_gist"},
    },
    "geometry_topology": {
        "statement": (
            "SELECT id FROM geometry WHERE topology_id = (SELECT topology_id FROM geometry LIMIT 1)"
        ),
        "expected_indexes": {"ix_geometry_topology_id"},
    },
    "frame_topology_derivation": {
        "statement": (
            "SELECT id FROM calculation_frame WHERE topology_derivation_id = "
            "(SELECT topology_derivation_id FROM calculation_frame LIMIT 1)"
        ),
        "expected_indexes": {"ix_calculation_frame_topology_derivation_id"},
    },
    "artifact_filename_contains": {
        "statement": "SELECT id FROM artifact_file WHERE original_filename ILIKE '%log%'",
        "expected_indexes": {"ix_artifact_file_original_filename_trgm"},
    },
    "artifact_project_keyset": {
        "statement": (
            "SELECT id FROM artifact_file "
            "WHERE project_id = '00000000-0000-7000-8000-000000000201' "
            "AND storage_status = 'available' "
            "ORDER BY created_at DESC, id LIMIT 50"
        ),
        "expected_indexes": {
            "ix_artifact_file_project_status_created_id",
            "ix_artifact_file_storage_status_created_at",
        },
    },
    "active_session_listing": {
        "statement": (
            "SELECT id FROM auth_session "
            "WHERE user_id = '00000000-0000-7000-8000-000000000002' "
            "AND revoked_at IS NULL AND expires_at > now() "
            "ORDER BY last_seen_at DESC LIMIT 50"
        ),
        "expected_indexes": {"ix_auth_session_user_active_last_seen"},
    },
}


def _plan_nodes(value: object) -> list[dict[str, object]]:
    if isinstance(value, list):
        return [node for item in value for node in _plan_nodes(item)]
    if not isinstance(value, dict):
        return []
    mapping = cast(dict[object, object], value)
    nodes = [cast(dict[str, object], value)] if "Node Type" in mapping else []
    return nodes + [node for child in mapping.values() for node in _plan_nodes(child)]


def _relation_counts(connection: Connection, relations: set[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for relation in sorted(relations):
        # Relation names originate only from PostgreSQL EXPLAIN output. Resolve
        # through regclass rather than interpolating the identifier into SQL.
        count = connection.execute(
            text(
                "SELECT COALESCE(c.reltuples, -1)::bigint "
                "FROM pg_class c WHERE c.oid = to_regclass(:relation)"
            ),
            {"relation": relation},
        ).scalar_one_or_none()
        counts[relation] = int(count or 0)
    return counts


def _capture_one(
    connection: Connection,
    *,
    label: str,
    spec: QuerySpec,
    max_seq_scan_rows: int,
) -> dict[str, object]:
    raw = connection.execute(
        text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {spec['statement']}")
    ).scalar_one()
    explain = cast(Mapping[str, object], raw[0])
    nodes = _plan_nodes(explain)
    index_names = sorted(
        {str(node["Index Name"]) for node in nodes if isinstance(node.get("Index Name"), str)}
    )
    expected_indexes = sorted(spec["expected_indexes"])
    expected_index_observed = bool(spec["expected_indexes"] & set(index_names))
    sequential_relations = {
        str(node["Relation Name"])
        for node in nodes
        if node.get("Node Type") == "Seq Scan" and isinstance(node.get("Relation Name"), str)
    }
    counts = _relation_counts(connection, sequential_relations)
    exceptions = [
        {
            "relation": relation,
            "estimated_rows": counts[relation],
            "maximum_allowed_rows": max_seq_scan_rows,
            "reason": (
                "planner selected a sequential scan for a relation below the capacity threshold"
            ),
        }
        for relation in sorted(sequential_relations)
        if 0 <= counts[relation] <= max_seq_scan_rows
    ]
    unexpected_scans = [
        {"relation": relation, "estimated_rows": counts[relation]}
        for relation in sorted(sequential_relations)
        if counts[relation] < 0 or counts[relation] > max_seq_scan_rows
    ]
    accepted = expected_index_observed or (bool(sequential_relations) and not unexpected_scans)
    execution_time = explain.get("Execution Time", 0.0)
    if not isinstance(execution_time, int | float):
        raise TypeError("EXPLAIN Execution Time must be numeric")
    return {
        "label": label,
        "statement": spec["statement"],
        "expected_indexes": expected_indexes,
        "observed_indexes": index_names,
        "expected_index_observed": expected_index_observed,
        "sequential_scan_exceptions": exceptions,
        "unexpected_sequential_scans": unexpected_scans,
        "execution_time_ms": float(execution_time),
        "accepted": accepted,
        "plan": explain,
    }


def capture_query_plan_evidence(
    *,
    dataset_scale: str,
    max_seq_scan_rows: int,
) -> dict[str, object]:
    settings = get_settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            database_info = connection.execute(
                text(
                    "SELECT current_database(), current_setting('server_version'), "
                    "(SELECT count(*) FROM molecular_topology), "
                    "(SELECT count(*) FROM artifact_file), "
                    "(SELECT count(*) FROM geometry), "
                    "(SELECT count(*) FROM mapped_reaction)"
                )
            ).one()
            plans = [
                _capture_one(
                    connection,
                    label=label,
                    spec=spec,
                    max_seq_scan_rows=max_seq_scan_rows,
                )
                for label, spec in QUERY_SPECS.items()
            ]
    finally:
        engine.dispose()
    return {
        "schema_version": QUERY_PLAN_EVIDENCE_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "node": socket.gethostname(),
        "dataset_scale": dataset_scale,
        "database": str(database_info[0]),
        "postgresql_version": str(database_info[1]),
        "relation_counts": {
            "molecular_topology": int(database_info[2]),
            "artifact_file": int(database_info[3]),
            "geometry": int(database_info[4]),
            "mapped_reaction": int(database_info[5]),
        },
        "max_seq_scan_rows": max_seq_scan_rows,
        "plans": plans,
        "all_indexed": all(bool(plan["expected_index_observed"]) for plan in plans),
        "succeeded": all(bool(plan["accepted"]) for plan in plans),
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset-scale",
        required=True,
        help="operator-owned description or immutable snapshot ID for the measured dataset",
    )
    parser.add_argument(
        "--max-seq-scan-rows",
        type=int,
        default=10_000,
        help="largest relation estimate allowed as an explicit sequential-scan capacity exception",
    )
    parser.add_argument("--output", type=Path, default=None)
    arguments = parser.parse_args()
    if arguments.max_seq_scan_rows < 0:
        parser.error("--max-seq-scan-rows must be non-negative")
    return arguments


def main() -> None:
    arguments = _arguments()
    evidence = capture_query_plan_evidence(
        dataset_scale=arguments.dataset_scale,
        max_seq_scan_rows=arguments.max_seq_scan_rows,
    )
    serialized = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    if arguments.output is None:
        print(serialized, end="")
    else:
        arguments.output.write_text(serialized, encoding="utf-8")
    if evidence["succeeded"] is not True:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
