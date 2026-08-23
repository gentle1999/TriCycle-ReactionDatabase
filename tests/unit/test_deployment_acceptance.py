from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from tricycle_reaction_db.dev.deployment_acceptance import (
    DEPLOYMENT_SMOKE_SCHEMA_VERSION,
    DeploymentAcceptanceRecord,
    validate_record,
)

CHECK_NAMES = (
    "public_health",
    "oidc",
    "postgresql",
    "rustfs_s3",
    "redis",
    "smtp_starttls",
)
FAILOVER_NAMES = ("postgresql", "rustfs", "redis", "oidc", "smtp", "edge_api")
WORKFLOW_NAMES = (
    "oidc_pkce_login_logout",
    "session_csrf_state_change",
    "project_invitation",
    "artifact_upload_download",
    "mcp_streaming_switch",
    "cross_project_private_404",
)
MONITORING_NAMES = (
    "public_live_ready",
    "postgresql_rustfs_redis",
    "oidc_smtp",
    "upload_storage_integrity",
    "maintenance_units",
)


def _write_json(root: Path, relative_path: str, payload: object) -> dict[str, object]:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(content)
    return {
        "path": relative_path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def _smoke(node: str) -> dict[str, object]:
    return {
        "schema_version": DEPLOYMENT_SMOKE_SCHEMA_VERSION,
        "checked_at": "2026-08-20T00:00:00Z",
        "node": node,
        "app_name": "Example Chemistry Database",
        "checks": [{"name": name, "succeeded": True, "details": {}} for name in CHECK_NAMES],
        "succeeded": True,
    }


def _valid_record(root: Path) -> dict[str, Any]:
    api_nodes: list[dict[str, object]] = []
    for node in ("API-01", "API-02"):
        api_nodes.append(
            {
                "name": node,
                "before": _write_json(root, f"smoke/{node}-before.json", _smoke(node)),
                "after": _write_json(root, f"smoke/{node}-after.json", _smoke(node)),
            }
        )

    failovers: list[dict[str, object]] = []
    for name in FAILOVER_NAMES:
        failovers.append(
            {
                "dependency": name,
                "stable_endpoint": f"https://{name}.example.test",
                "injection": f"switch {name} writer",
                "started_at": "2026-08-20T00:10:00Z",
                "recovered_at": "2026-08-20T00:10:30Z",
                "client_errors": 0,
                "evidence": _write_json(
                    root,
                    f"failover/{name}.json",
                    {
                        "schema_version": "dependency-failover-v1",
                        "dependency": name,
                        "succeeded": True,
                        "started_at": "2026-08-20T00:10:00Z",
                        "recovered_at": "2026-08-20T00:10:30Z",
                        "client_errors": 0,
                    },
                ),
                "result": "passed",
            }
        )

    workflows: list[dict[str, object]] = []
    for name in WORKFLOW_NAMES:
        workflows.append(
            {
                "name": name,
                "evidence": _write_json(
                    root,
                    f"workflow/{name}.json",
                    {
                        "schema_version": "workflow-check-v1",
                        "name": name,
                        "succeeded": True,
                    },
                ),
                "result": "passed",
            }
        )

    monitoring: list[dict[str, object]] = []
    for name in MONITORING_NAMES:
        monitoring.append(
            {
                "signal": name,
                "trigger_evidence": _write_json(
                    root,
                    f"monitoring/{name}-trigger.json",
                    {
                        "schema_version": "monitoring-check-v1",
                        "signal": name,
                        "triggered": True,
                    },
                ),
                "recovery_evidence": _write_json(
                    root,
                    f"monitoring/{name}-recovery.json",
                    {
                        "schema_version": "monitoring-check-v1",
                        "signal": name,
                        "recovered": True,
                    },
                ),
                "result": "passed",
            }
        )

    restore_fields = {
        "alembic_version": "0001_initial_schema",
        "row_counts": {"artifact_file": 4, "calculation_frame": 45},
        "storage_status_counts": {"available": 4},
        "available_artifact_count": 4,
        "checked_artifact_count": 4,
        "checked_artifact_bytes": 1234,
        "artifact_manifest_digest": "b" * 64,
    }
    restore_payload = {
        "schema_version": "restore-validation-v1",
        "validation_timestamp": "2026-08-20T01:00:00Z",
        "succeeded": True,
        "manifest_mismatches": [],
        "failures": [],
        **restore_fields,
    }
    restore_payload_after = {
        **restore_payload,
        "validation_timestamp": "2026-08-20T02:00:00Z",
    }

    return {
        "schema_version": "deployment-acceptance-v1",
        "release": {
            "deployment_id": "deploy-20260820-01",
            "git_revision": "abcdef1234567",
            "artifact_digest": "a" * 64,
            "public_origin": "https://app.example.test",
            "started_at": "2026-08-20T00:00:00Z",
            "finished_at": "2026-08-20T03:00:00Z",
            "operator": "operator@example.test",
            "reviewer": "reviewer@example.test",
        },
        "api_nodes": api_nodes,
        "failovers": failovers,
        "workflows": workflows,
        "capacity": {
            "dataset_scale": "DA benchmark + target-scale snapshot",
            "api_node_count": 2,
            "workers_per_node": 2,
            "parse_slots_per_worker": 1,
            "n_jobs": 2,
            "benchmark": _write_json(
                root,
                "capacity/upload-benchmark.json",
                {
                    "schema_version": "upload-resource-benchmark-v2",
                    "generated_at": "2026-08-20T00:30:00Z",
                    "node": "API-01",
                    "fixture": "fixtures/target-output.log",
                    "fixture_sha256": "c" * 64,
                    "n_jobs": 2,
                    "batch_sizes": [1, 8, 32],
                    "results": [
                        {
                            "batch_size": size,
                            "n_jobs": 2,
                            "failed_count": 0,
                            "succeeded_count": size,
                            "elapsed_seconds": 0.25,
                            "phase_timings_ms": {
                                "prepare_inputs_ms": 1.0,
                                "molop_parse_ms": 200.0,
                                "total_ms": 201.0,
                            },
                        }
                        for size in (1, 8, 32)
                    ],
                    "succeeded": True,
                },
            ),
            "upload_limit": _write_json(
                root,
                "capacity/upload-limit.json",
                {
                    "schema_version": "upload-limit-probe-v1",
                    "generated_at": "2026-08-20T00:35:00Z",
                    "probe_node": "API-01",
                    "api_origin": "https://app.example.test",
                    "project_id": "00000000-0000-7000-8000-000000000201",
                    "maximum_upload_bytes": 67108864,
                    "attempts": 2,
                    "observations": [
                        {
                            "attempt": attempt,
                            "request_bytes": 67108865,
                            "status_code": 413,
                            "detail": "uploaded artifact exceeds the 67108864-byte limit",
                            "rejection_stage": "preflight",
                        }
                        for attempt in (1, 2)
                    ],
                    "succeeded": True,
                },
            ),
            "query_plans": _write_json(
                root,
                "capacity/query-plans.json",
                {
                    "schema_version": "query-plan-evidence-v1",
                    "generated_at": "2026-08-20T00:40:00Z",
                    "node": "API-01",
                    "dataset_scale": "DA benchmark + target-scale snapshot",
                    "succeeded": True,
                    "all_indexed": True,
                    "max_seq_scan_rows": 10000,
                    "relation_counts": {},
                    "plans": [
                        {
                            "label": label,
                            "accepted": True,
                            "expected_indexes": [f"ix_{label}"],
                            "observed_indexes": [f"ix_{label}"],
                            "expected_index_observed": True,
                            "sequential_scan_exceptions": [],
                            "unexpected_sequential_scans": [],
                            "plan": {
                                "Plan": {"Node Type": "Index Scan", "Index Name": f"ix_{label}"},
                                "Planning Time": 0.01,
                                "Execution Time": 0.02,
                            },
                        }
                        for label in (
                            "formula_exact_counts",
                            "topology_smarts",
                            "topology_fingerprint_knn",
                            "reaction_smarts",
                            "reaction_fingerprint_knn",
                            "geometry_topology",
                            "frame_topology_derivation",
                            "artifact_filename_contains",
                            "artifact_project_keyset",
                            "active_session_listing",
                        )
                    ],
                },
            ),
            "query_plan_expectations_met": True,
            "shared_rate_limit_node_count": 2,
            "shared_rate_limit": _write_json(
                root,
                "capacity/shared-rate-limit.json",
                {
                    "schema_version": "shared-rate-limit-v1",
                    "api_node_count": 2,
                    "api_origins": [
                        "https://api-01.example.test",
                        "https://api-02.example.test",
                    ],
                    "observations": [
                        {
                            "sequence": 1,
                            "origin": "https://api-01.example.test",
                            "status_code": 200,
                            "remaining": 1,
                            "policy": "read",
                        },
                        {
                            "sequence": 2,
                            "origin": "https://api-02.example.test",
                            "status_code": 200,
                            "remaining": 0,
                            "policy": "read",
                        },
                        {
                            "sequence": 3,
                            "origin": "https://api-01.example.test",
                            "status_code": 429,
                            "remaining": 0,
                            "policy": "read",
                        },
                    ],
                    "shared_budget": True,
                    "rejection_observed": True,
                    "all_nodes_observed": True,
                    "tls": True,
                    "succeeded": True,
                },
            ),
            "rate_limit_fail_closed": _write_json(
                root,
                "capacity/rate-limit-fail-closed.json",
                {
                    "schema_version": "rate-limit-fail-closed-v1",
                    "api_node_count": 2,
                    "api_origins": [
                        "https://api-01.example.test",
                        "https://api-02.example.test",
                    ],
                    "observations": [
                        {
                            "origin": "https://api-01.example.test",
                            "status_code": 503,
                            "error_code": "rate_limit_backend_unavailable",
                            "cache_control": "no-store",
                            "retry_after": "1",
                        },
                        {
                            "origin": "https://api-02.example.test",
                            "status_code": 503,
                            "error_code": "rate_limit_backend_unavailable",
                            "cache_control": "no-store",
                            "retry_after": "1",
                        },
                    ],
                    "fail_closed": True,
                    "tls": True,
                    "succeeded": True,
                },
            ),
            "result": "passed",
        },
        "restore": {
            "backup_id": "backup-20260820-00",
            "backup_manifest": _write_json(
                root,
                "restore/backup-manifest.json",
                {
                    "schema_version": "backup-manifest-v1",
                    "backup_id": "backup-20260820-00",
                    "postgresql_snapshot": "snapshot-1",
                    "object_replication_watermark": "version-watermark-1",
                    "oidc_backup": "oidc-export-1",
                    "secret_backup": "secret-snapshot-1",
                    "succeeded": True,
                },
            ),
            "source_manifest": _write_json(root, "restore/source.json", restore_payload),
            "restore_validation": _write_json(root, "restore/restored.json", restore_payload_after),
            "oidc_backup_reference": "secret://backup/oidc",
            "oidc_backup_evidence": _write_json(
                root,
                "restore/oidc-backup-receipt.json",
                {
                    "schema_version": "backup-receipt-v1",
                    "backup_id": "backup-20260820-00",
                    "kind": "oidc",
                    "succeeded": True,
                },
            ),
            "secret_backup_reference": "secret://backup/runtime",
            "secret_backup_evidence": _write_json(
                root,
                "restore/secret-backup-receipt.json",
                {
                    "schema_version": "backup-receipt-v1",
                    "backup_id": "backup-20260820-00",
                    "kind": "runtime-secrets",
                    "succeeded": True,
                },
            ),
            "failure_injected_at": "2026-08-20T01:30:00Z",
            "database_restore_completed_at": "2026-08-20T01:40:00Z",
            "object_restore_completed_at": "2026-08-20T01:45:00Z",
            "application_acceptance_completed_at": "2026-08-20T01:50:00Z",
            "latest_recoverable_at": "2026-08-20T01:29:55Z",
            "measured_rto_seconds": 1200,
            "measured_rpo_seconds": 5,
            "mismatches": [],
            "result": "passed",
        },
        "monitoring": monitoring,
        "release_decision": "approved",
        "approver": "reviewer@example.test",
        "approved_at": "2026-08-20T03:05:00Z",
    }


def test_acceptance_record_validates_all_required_evidence(tmp_path: Path) -> None:
    record_path = tmp_path / "acceptance-record.json"
    record_path.write_text(json.dumps(_valid_record(tmp_path)), encoding="utf-8")

    assert validate_record(record_path) == []


def test_acceptance_record_rejects_benchmark_without_phase_timings(tmp_path: Path) -> None:
    record = _valid_record(tmp_path)
    payload_path = tmp_path / "capacity/upload-benchmark.json"
    payload = json.loads(payload_path.read_text())
    del payload["results"][0]["phase_timings_ms"]["molop_parse_ms"]
    content = (json.dumps(payload, sort_keys=True) + "\n").encode()
    payload_path.write_bytes(content)
    record["capacity"]["benchmark"]["sha256"] = hashlib.sha256(content).hexdigest()
    record["capacity"]["benchmark"]["size_bytes"] = len(content)
    record_path = tmp_path / "acceptance-record.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")

    errors = validate_record(record_path)

    assert any("phase_timings_ms is missing" in error for error in errors)


def test_acceptance_record_rejects_attachment_hash_drift(tmp_path: Path) -> None:
    record = _valid_record(tmp_path)
    record_path = tmp_path / "acceptance-record.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    smoke_path = tmp_path / "smoke/API-01-before.json"
    smoke_path.write_text(smoke_path.read_text() + "drift\n", encoding="utf-8")

    errors = validate_record(record_path)

    assert any("api_nodes[0].before.sha256" in error for error in errors)


def test_acceptance_record_rejects_inconsistent_rto_rpo_timestamps(tmp_path: Path) -> None:
    record = _valid_record(tmp_path)
    record["restore"]["measured_rto_seconds"] = 1
    record_path = tmp_path / "acceptance-record.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")

    errors = validate_record(record_path)

    assert any("measured_rto_seconds" in error for error in errors)


def test_acceptance_record_rejects_shared_trigger_and_recovery_attachment(tmp_path: Path) -> None:
    record = _valid_record(tmp_path)
    monitoring = record["monitoring"][0]
    assert isinstance(monitoring, dict)
    monitoring["recovery_evidence"] = monitoring["trigger_evidence"]
    record_path = tmp_path / "acceptance-record.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")

    errors = validate_record(record_path)

    assert any("trigger and recovery evidence" in error for error in errors)


def test_acceptance_record_rejects_forged_rate_limit_booleans(tmp_path: Path) -> None:
    record = _valid_record(tmp_path)
    payload_path = tmp_path / "capacity/shared-rate-limit.json"
    payload = json.loads(payload_path.read_text())
    payload["shared_budget"] = True
    payload["observations"][1]["remaining"] = 1
    content = (json.dumps(payload, sort_keys=True) + "\n").encode()
    payload_path.write_bytes(content)
    record["capacity"]["shared_rate_limit"]["sha256"] = hashlib.sha256(content).hexdigest()
    record["capacity"]["shared_rate_limit"]["size_bytes"] = len(content)
    record_path = tmp_path / "acceptance-record.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")

    errors = validate_record(record_path)

    assert any("shared_budget does not match observations" in error for error in errors)


def test_acceptance_record_rejects_missing_upload_limit_probe(tmp_path: Path) -> None:
    record = _valid_record(tmp_path)
    record["capacity"].pop("upload_limit")
    record_path = tmp_path / "acceptance-record.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")

    errors = validate_record(record_path)

    assert any("upload_limit" in error for error in errors)


def test_acceptance_record_rejects_pending_or_incomplete_release(tmp_path: Path) -> None:
    record_path = tmp_path / "acceptance-record.json"
    record_path.write_text(
        json.dumps(
            {
                "schema_version": "deployment-acceptance-v1",
                "release_decision": "PENDING",
            }
        ),
        encoding="utf-8",
    )

    errors = validate_record(record_path)

    assert any("release" in error for error in errors)
    assert any("release_decision" in error for error in errors)


def test_acceptance_record_exposes_a_json_schema() -> None:
    schema = DeploymentAcceptanceRecord.model_json_schema()

    assert schema["properties"]["schema_version"]["const"] == "deployment-acceptance-v1"
    assert "restore" in schema["required"]
