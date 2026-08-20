"""Validate a production deployment acceptance record and its evidence files."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, cast

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from tricycle_reaction_db.dev.deployment_smoke import DEPLOYMENT_SMOKE_SCHEMA_VERSION
from tricycle_reaction_db.dev.restore_validation import RESTORE_VALIDATION_SCHEMA_VERSION

DEPLOYMENT_ACCEPTANCE_SCHEMA_VERSION = "deployment-acceptance-v1"
SHA256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

EXPECTED_FAILOVERS = frozenset({"postgresql", "rustfs", "redis", "oidc", "smtp", "edge_api"})
EXPECTED_WORKFLOWS = frozenset(
    {
        "oidc_pkce_login_logout",
        "session_csrf_state_change",
        "project_invitation",
        "artifact_upload_download",
        "mcp_streaming_switch",
        "cross_project_private_404",
    }
)
EXPECTED_MONITORING = frozenset(
    {
        "public_live_ready",
        "postgresql_rustfs_redis",
        "oidc_smtp",
        "upload_storage_integrity",
        "maintenance_units",
    }
)
EXPECTED_BATCH_SIZES = frozenset({1, 8, 32})
EXPECTED_QUERY_PLAN_LABELS = frozenset(
    {
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
    }
)
RESTORE_COMPARISON_FIELDS = (
    "alembic_version",
    "row_counts",
    "storage_status_counts",
    "available_artifact_count",
    "checked_artifact_count",
    "checked_artifact_bytes",
    "artifact_manifest_digest",
)


def _evidence_plan_nodes(value: object) -> list[dict[str, object]]:
    """Flatten the nested PostgreSQL JSON plan tree for evidence checks."""

    if isinstance(value, list):
        return [node for item in value for node in _evidence_plan_nodes(item)]
    if not isinstance(value, dict):
        return []
    nodes: list[dict[str, object]] = []
    if isinstance(value.get("Node Type"), str):
        nodes.append(value)
    for child in value.values():
        nodes.extend(_evidence_plan_nodes(child))
    return nodes


class _AcceptanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def reject_pending_placeholder(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().upper() == "PENDING":
            raise ValueError("PENDING placeholders are not accepted")
        return value


class EvidenceFile(_AcceptanceModel):
    path: str = Field(min_length=1)
    sha256: SHA256
    size_bytes: int = Field(gt=0)


class ReleaseIdentity(_AcceptanceModel):
    deployment_id: str = Field(min_length=1)
    git_revision: str = Field(min_length=7)
    artifact_digest: SHA256
    public_origin: HttpUrl
    started_at: AwareDatetime
    finished_at: AwareDatetime
    operator: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_timing(self) -> ReleaseIdentity:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if self.public_origin.scheme != "https":
            raise ValueError("public_origin must use HTTPS")
        return self


class ApiNodeEvidence(_AcceptanceModel):
    name: str = Field(min_length=1)
    before: EvidenceFile
    after: EvidenceFile

    @model_validator(mode="after")
    def validate_distinct_evidence(self) -> ApiNodeEvidence:
        if self.before.path == self.after.path:
            raise ValueError("before and after smoke evidence must be different files")
        return self


class FailoverEvidence(_AcceptanceModel):
    dependency: Literal["postgresql", "rustfs", "redis", "oidc", "smtp", "edge_api"]
    stable_endpoint: str = Field(min_length=1)
    injection: str = Field(min_length=1)
    started_at: AwareDatetime
    recovered_at: AwareDatetime
    client_errors: int = Field(ge=0)
    evidence: EvidenceFile
    result: Literal["passed"]

    @model_validator(mode="after")
    def validate_timing(self) -> FailoverEvidence:
        if self.recovered_at < self.started_at:
            raise ValueError("recovered_at must not precede started_at")
        return self


class WorkflowEvidence(_AcceptanceModel):
    name: str = Field(min_length=1)
    evidence: EvidenceFile
    result: Literal["passed"]


class MonitoringEvidence(_AcceptanceModel):
    signal: str = Field(min_length=1)
    trigger_evidence: EvidenceFile
    recovery_evidence: EvidenceFile
    result: Literal["passed"]

    @model_validator(mode="after")
    def validate_distinct_evidence(self) -> MonitoringEvidence:
        if self.trigger_evidence.path == self.recovery_evidence.path:
            raise ValueError("trigger and recovery evidence must be different files")
        return self


class CapacityEvidence(_AcceptanceModel):
    dataset_scale: str = Field(min_length=1)
    api_node_count: int = Field(ge=2)
    workers_per_node: int = Field(ge=1)
    parse_slots_per_worker: int = Field(ge=1)
    n_jobs: int = Field(ge=1)
    benchmark: EvidenceFile
    upload_limit: EvidenceFile
    query_plans: EvidenceFile
    query_plan_expectations_met: Literal[True]
    shared_rate_limit_node_count: int = Field(ge=2)
    shared_rate_limit: EvidenceFile
    rate_limit_fail_closed: EvidenceFile
    result: Literal["passed"]


class RestoreEvidence(_AcceptanceModel):
    backup_id: str = Field(min_length=1)
    backup_manifest: EvidenceFile
    source_manifest: EvidenceFile
    restore_validation: EvidenceFile
    oidc_backup_reference: str = Field(min_length=1)
    oidc_backup_evidence: EvidenceFile
    secret_backup_reference: str = Field(min_length=1)
    secret_backup_evidence: EvidenceFile
    failure_injected_at: AwareDatetime
    database_restore_completed_at: AwareDatetime
    object_restore_completed_at: AwareDatetime
    application_acceptance_completed_at: AwareDatetime
    latest_recoverable_at: AwareDatetime
    measured_rto_seconds: float = Field(gt=0)
    measured_rpo_seconds: float = Field(ge=0)
    mismatches: list[str]
    result: Literal["passed"]

    @model_validator(mode="after")
    def validate_timing(self) -> RestoreEvidence:
        milestones = (
            self.failure_injected_at,
            self.database_restore_completed_at,
            self.object_restore_completed_at,
            self.application_acceptance_completed_at,
        )
        if any(later < earlier for earlier, later in zip(milestones, milestones[1:], strict=False)):
            raise ValueError("restore milestones must be chronological")
        if self.latest_recoverable_at > self.failure_injected_at:
            raise ValueError("latest_recoverable_at must not be after failure_injected_at")
        expected_rto = (
            self.application_acceptance_completed_at - self.failure_injected_at
        ).total_seconds()
        expected_rpo = (self.failure_injected_at - self.latest_recoverable_at).total_seconds()
        if abs(self.measured_rto_seconds - expected_rto) > 0.001:
            raise ValueError("measured_rto_seconds does not match the recorded timestamps")
        if abs(self.measured_rpo_seconds - expected_rpo) > 0.001:
            raise ValueError("measured_rpo_seconds does not match the recorded timestamps")
        if self.mismatches:
            raise ValueError("mismatches must be empty for a passed restore")
        return self


class DeploymentAcceptanceRecord(_AcceptanceModel):
    schema_version: Literal["deployment-acceptance-v1"]
    release: ReleaseIdentity
    api_nodes: list[ApiNodeEvidence] = Field(min_length=2)
    failovers: list[FailoverEvidence] = Field(min_length=6)
    workflows: list[WorkflowEvidence] = Field(min_length=6)
    capacity: CapacityEvidence
    restore: RestoreEvidence
    monitoring: list[MonitoringEvidence] = Field(min_length=5)
    release_decision: Literal["approved"]
    approver: str = Field(min_length=1)
    approved_at: AwareDatetime

    @model_validator(mode="after")
    def validate_complete(self) -> DeploymentAcceptanceRecord:
        node_names = [node.name for node in self.api_nodes]
        if len(set(node_names)) != len(node_names):
            raise ValueError("api_nodes must have unique names")
        failovers = {item.dependency for item in self.failovers}
        if failovers != EXPECTED_FAILOVERS or len(self.failovers) != len(EXPECTED_FAILOVERS):
            raise ValueError(f"failovers must contain exactly {sorted(EXPECTED_FAILOVERS)}")
        workflows = {item.name for item in self.workflows}
        if workflows != EXPECTED_WORKFLOWS or len(self.workflows) != len(EXPECTED_WORKFLOWS):
            raise ValueError(f"workflows must contain exactly {sorted(EXPECTED_WORKFLOWS)}")
        monitoring = {item.signal for item in self.monitoring}
        if monitoring != EXPECTED_MONITORING or len(self.monitoring) != len(EXPECTED_MONITORING):
            raise ValueError(f"monitoring must contain exactly {sorted(EXPECTED_MONITORING)}")
        if self.capacity.api_node_count != len(self.api_nodes):
            raise ValueError("capacity.api_node_count must equal the number of api_nodes")
        if self.capacity.shared_rate_limit_node_count != len(self.api_nodes):
            raise ValueError(
                "capacity.shared_rate_limit_node_count must equal the number of api_nodes"
            )
        if self.approved_at < self.release.finished_at:
            raise ValueError("approved_at must not precede release.finished_at")
        if self.approver != self.release.reviewer:
            raise ValueError("approver must match release.reviewer")
        return self


def _object(value: object, label: str, errors: list[str]) -> dict[str, object] | None:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        errors.append(f"{label} must be an object with string keys")
        return None
    return cast(dict[str, object], value)


def _read_json(path: Path, label: str, errors: list[str]) -> object | None:
    try:
        return cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        errors.append(f"{label} is not readable JSON: {error}")
        return None


def _resolve_evidence(
    root: Path,
    evidence: EvidenceFile,
    label: str,
    errors: list[str],
) -> Path | None:
    fragment = Path(evidence.path)
    if fragment.is_absolute() or ".." in fragment.parts:
        errors.append(f"{label}.path escapes the acceptance record directory")
        return None
    path = (root / fragment).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        errors.append(f"{label}.path escapes the acceptance record directory")
        return None
    try:
        payload = path.read_bytes()
    except OSError as error:
        errors.append(f"{label} cannot be read: {error}")
        return None
    if len(payload) != evidence.size_bytes:
        errors.append(f"{label}.size_bytes expected={evidence.size_bytes} got={len(payload)}")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != evidence.sha256:
        errors.append(f"{label}.sha256 expected={evidence.sha256} got={digest}")
    return path


def _evidence_files(record: DeploymentAcceptanceRecord) -> Iterator[tuple[str, EvidenceFile]]:
    for index, node in enumerate(record.api_nodes):
        yield f"api_nodes[{index}].before", node.before
        yield f"api_nodes[{index}].after", node.after
    for index, item in enumerate(record.failovers):
        yield f"failovers[{index}].evidence", item.evidence
    for index, workflow in enumerate(record.workflows):
        yield f"workflows[{index}].evidence", workflow.evidence
    yield "capacity.benchmark", record.capacity.benchmark
    yield "capacity.upload_limit", record.capacity.upload_limit
    yield "capacity.query_plans", record.capacity.query_plans
    yield "capacity.shared_rate_limit", record.capacity.shared_rate_limit
    yield "capacity.rate_limit_fail_closed", record.capacity.rate_limit_fail_closed
    yield "restore.backup_manifest", record.restore.backup_manifest
    yield "restore.source_manifest", record.restore.source_manifest
    yield "restore.restore_validation", record.restore.restore_validation
    yield "restore.oidc_backup_evidence", record.restore.oidc_backup_evidence
    yield "restore.secret_backup_evidence", record.restore.secret_backup_evidence
    for index, monitoring in enumerate(record.monitoring):
        yield f"monitoring[{index}].trigger_evidence", monitoring.trigger_evidence
        yield f"monitoring[{index}].recovery_evidence", monitoring.recovery_evidence


def _validate_smoke(path: Path, expected_node: str, label: str, errors: list[str]) -> None:
    payload = _object(_read_json(path, label, errors), label, errors)
    if payload is None:
        return
    if payload.get("schema_version") != DEPLOYMENT_SMOKE_SCHEMA_VERSION:
        errors.append(f"{label}.schema_version must be {DEPLOYMENT_SMOKE_SCHEMA_VERSION}")
    if payload.get("succeeded") is not True:
        errors.append(f"{label}.succeeded must be true")
    if payload.get("node") != expected_node:
        errors.append(f"{label}.node must match {expected_node!r}")
    checks = payload.get("checks")
    if not isinstance(checks, list):
        errors.append(f"{label}.checks must be an array")
        return
    names: list[str] = []
    for index, raw_check in enumerate(checks):
        check = _object(raw_check, f"{label}.checks[{index}]", errors)
        if check is None:
            continue
        name = check.get("name")
        if not isinstance(name, str):
            errors.append(f"{label}.checks[{index}].name must be a string")
        else:
            names.append(name)
        if check.get("succeeded") is not True:
            errors.append(f"{label}.checks[{index}].succeeded must be true")
    expected_checks = {"public_health", "oidc", "postgresql", "rustfs_s3", "redis", "smtp_starttls"}
    if set(names) != expected_checks or len(names) != len(expected_checks):
        errors.append(f"{label}.checks must contain exactly {sorted(expected_checks)}")


def _validate_restore(source_path: Path, restore_path: Path, errors: list[str]) -> None:
    source = _object(
        _read_json(source_path, "restore.source_manifest", errors),
        "restore.source_manifest",
        errors,
    )
    restored = _object(
        _read_json(restore_path, "restore.restore_validation", errors),
        "restore.restore_validation",
        errors,
    )
    if source is None or restored is None:
        return
    for label, payload in (
        ("restore.source_manifest", source),
        ("restore.restore_validation", restored),
    ):
        if payload.get("schema_version") != RESTORE_VALIDATION_SCHEMA_VERSION:
            errors.append(f"{label}.schema_version must be {RESTORE_VALIDATION_SCHEMA_VERSION}")
        if payload.get("succeeded") is not True:
            errors.append(f"{label}.succeeded must be true")
        if payload.get("manifest_mismatches") != []:
            errors.append(f"{label}.manifest_mismatches must be empty")
        if payload.get("failures") != []:
            errors.append(f"{label}.failures must be empty")
    for field in RESTORE_COMPARISON_FIELDS:
        if source.get(field) != restored.get(field):
            errors.append(f"restore.{field} differs between source and restored manifests")


def _validate_benchmark(path: Path, capacity: CapacityEvidence, errors: list[str]) -> None:
    payload = _object(
        _read_json(path, "capacity.benchmark", errors),
        "capacity.benchmark",
        errors,
    )
    if payload is None:
        return
    if payload.get("schema_version") != "upload-resource-benchmark-v1":
        errors.append("capacity.benchmark.schema_version must be upload-resource-benchmark-v1")
    if payload.get("succeeded") is not True:
        errors.append("capacity.benchmark.succeeded must be true")
    if payload.get("n_jobs") != capacity.n_jobs:
        errors.append("capacity.benchmark.n_jobs does not match capacity.n_jobs")
    batch_sizes = payload.get("batch_sizes")
    if (
        not isinstance(batch_sizes, list)
        or any(not isinstance(value, int) or isinstance(value, bool) for value in batch_sizes)
        or set(batch_sizes) != EXPECTED_BATCH_SIZES
        or len(batch_sizes) != len(EXPECTED_BATCH_SIZES)
    ):
        errors.append(
            f"capacity.benchmark.batch_sizes must contain exactly {sorted(EXPECTED_BATCH_SIZES)}"
        )
    fixture_digest = payload.get("fixture_sha256")
    if not isinstance(fixture_digest, str) or len(fixture_digest) != 64:
        errors.append("capacity.benchmark.fixture_sha256 must be a SHA-256 digest")
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        errors.append("capacity.benchmark.results must be an array")
        return
    observed: set[int] = set()
    for index, item in enumerate(raw_results):
        result = _object(item, f"capacity.benchmark.results[{index}]", errors)
        if result is None:
            continue
        batch_size = result.get("batch_size")
        if not isinstance(batch_size, int) or isinstance(batch_size, bool):
            errors.append(f"capacity.benchmark.results[{index}].batch_size must be an integer")
            continue
        observed.add(batch_size)
        if result.get("failed_count") != 0:
            errors.append(f"capacity.benchmark.results[{index}].failed_count must be zero")
        if result.get("succeeded_count") != batch_size:
            errors.append(
                f"capacity.benchmark.results[{index}].succeeded_count must equal batch_size"
            )
        if result.get("n_jobs") != capacity.n_jobs:
            errors.append(
                f"capacity.benchmark.results[{index}].n_jobs does not match capacity.n_jobs"
            )
    if observed != EXPECTED_BATCH_SIZES:
        errors.append(f"capacity.benchmark must contain exactly {sorted(EXPECTED_BATCH_SIZES)}")


def _validate_upload_limit(path: Path, errors: list[str]) -> None:
    payload = _object(
        _read_json(path, "capacity.upload_limit", errors),
        "capacity.upload_limit",
        errors,
    )
    if payload is None:
        return
    if payload.get("schema_version") != "upload-limit-probe-v1":
        errors.append("capacity.upload_limit.schema_version must be upload-limit-probe-v1")
    maximum = payload.get("maximum_upload_bytes")
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 1:
        errors.append("capacity.upload_limit.maximum_upload_bytes must be a positive integer")
    if payload.get("succeeded") is not True:
        errors.append("capacity.upload_limit.succeeded must be true")
    observations = payload.get("observations")
    if not isinstance(observations, list) or len(observations) < 2:
        errors.append("capacity.upload_limit.observations must contain at least two attempts")
        return
    attempts = payload.get("attempts")
    if attempts != len(observations):
        errors.append("capacity.upload_limit.attempts must equal the observation count")
    for index, raw_observation in enumerate(observations):
        observation = _object(
            raw_observation, f"capacity.upload_limit.observations[{index}]", errors
        )
        if observation is None:
            continue
        if observation.get("status_code") != 413:
            errors.append(f"capacity.upload_limit.observations[{index}].status_code must be 413")
        if observation.get("rejection_stage") != "preflight":
            errors.append(
                f"capacity.upload_limit.observations[{index}].rejection_stage must be preflight"
            )
        request_bytes = observation.get("request_bytes")
        if (
            not isinstance(request_bytes, int)
            or isinstance(request_bytes, bool)
            or not isinstance(maximum, int)
            or request_bytes <= maximum
        ):
            errors.append(
                f"capacity.upload_limit.observations[{index}].request_bytes must exceed "
                "maximum_upload_bytes"
            )
        detail = observation.get("detail")
        if not isinstance(detail, str) or not detail:
            errors.append(f"capacity.upload_limit.observations[{index}].detail must be non-empty")
        elif isinstance(maximum, int) and f"{maximum}-byte limit" not in detail:
            errors.append(
                f"capacity.upload_limit.observations[{index}].detail must name the "
                "configured byte limit"
            )
    first = observations[0]
    if isinstance(first, dict):
        for _index, raw_observation in enumerate(observations[1:], start=1):
            if not isinstance(raw_observation, dict):
                continue
            if raw_observation.get("status_code") != first.get("status_code"):
                errors.append("capacity.upload_limit status_code is not stable across attempts")
            if raw_observation.get("detail") != first.get("detail"):
                errors.append("capacity.upload_limit detail is not stable across attempts")
            if raw_observation.get("rejection_stage") != first.get("rejection_stage"):
                errors.append("capacity.upload_limit rejection_stage is not stable across attempts")


def _validate_query_plans(
    path: Path,
    capacity: CapacityEvidence,
    errors: list[str],
) -> None:
    payload = _object(
        _read_json(path, "capacity.query_plans", errors),
        "capacity.query_plans",
        errors,
    )
    if payload is None:
        return
    if payload.get("schema_version") != "query-plan-evidence-v1":
        errors.append("capacity.query_plans.schema_version must be query-plan-evidence-v1")
    if payload.get("dataset_scale") != capacity.dataset_scale:
        errors.append("capacity.query_plans.dataset_scale does not match capacity.dataset_scale")
    if payload.get("succeeded") is not True:
        errors.append("capacity.query_plans.succeeded must be true")
    all_indexed = payload.get("all_indexed")
    if not isinstance(all_indexed, bool):
        errors.append("capacity.query_plans.all_indexed must be a boolean")
    max_seq_scan_rows = payload.get("max_seq_scan_rows")
    if (
        not isinstance(max_seq_scan_rows, int)
        or isinstance(max_seq_scan_rows, bool)
        or max_seq_scan_rows < 0
    ):
        errors.append("capacity.query_plans.max_seq_scan_rows must be a non-negative integer")
    relation_counts = payload.get("relation_counts")
    if not isinstance(relation_counts, dict) or not all(
        isinstance(key, str)
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
        for key, value in relation_counts.items()
    ):
        errors.append(
            "capacity.query_plans.relation_counts must map relation names to non-negative integers"
        )
        relation_counts = {}
    plans = payload.get("plans")
    if not isinstance(plans, list) or not plans:
        errors.append("capacity.query_plans.plans must be a non-empty array")
        return
    labels: list[str] = []
    derived_all_indexed = True
    for index, raw_plan in enumerate(plans):
        plan = _object(raw_plan, f"capacity.query_plans.plans[{index}]", errors)
        if plan is None:
            continue
        label = plan.get("label")
        if isinstance(label, str):
            labels.append(label)
        else:
            errors.append(f"capacity.query_plans.plans[{index}].label must be a string")
        if plan.get("accepted") is not True:
            errors.append(f"capacity.query_plans.plans[{index}].accepted must be true")
        expected_indexes = plan.get("expected_indexes")
        observed_indexes = plan.get("observed_indexes")
        if not isinstance(expected_indexes, list) or not all(
            isinstance(value, str) and value for value in expected_indexes
        ):
            errors.append(
                f"capacity.query_plans.plans[{index}].expected_indexes must be a string array"
            )
            expected_indexes = []
        if not isinstance(observed_indexes, list) or not all(
            isinstance(value, str) and value for value in observed_indexes
        ):
            errors.append(
                f"capacity.query_plans.plans[{index}].observed_indexes must be a string array"
            )
            observed_indexes = []
        expected_index_observed = bool(set(expected_indexes) & set(observed_indexes))
        if plan.get("expected_index_observed") is not expected_index_observed:
            errors.append(
                f"capacity.query_plans.plans[{index}].expected_index_observed does not match "
                "expected/observed indexes"
            )
        derived_all_indexed = derived_all_indexed and expected_index_observed

        raw_plan_tree = plan.get("plan")
        if not isinstance(raw_plan_tree, dict) or not isinstance(raw_plan_tree.get("Plan"), dict):
            errors.append(f"capacity.query_plans.plans[{index}].plan must contain a Plan object")
            plan_nodes: list[dict[str, object]] = []
        else:
            root_node = raw_plan_tree["Plan"]
            if not isinstance(root_node.get("Node Type"), str):
                errors.append(
                    f"capacity.query_plans.plans[{index}].plan.Plan.Node Type must be a string"
                )
            for timing_key in ("Planning Time", "Execution Time"):
                timing = raw_plan_tree.get(timing_key)
                if not isinstance(timing, int | float) or isinstance(timing, bool) or timing < 0:
                    errors.append(
                        f"capacity.query_plans.plans[{index}].plan.{timing_key} must be "
                        "non-negative"
                    )
            plan_nodes = _evidence_plan_nodes(raw_plan_tree)
        sequential_relations = {
            str(node["Relation Name"])
            for node in plan_nodes
            if node.get("Node Type") == "Seq Scan" and isinstance(node.get("Relation Name"), str)
        }
        exceptions = plan.get("sequential_scan_exceptions")
        unexpected = plan.get("unexpected_sequential_scans")
        if not isinstance(exceptions, list):
            errors.append(
                f"capacity.query_plans.plans[{index}].sequential_scan_exceptions must be an array"
            )
            exceptions = []
        if not isinstance(unexpected, list):
            errors.append(
                f"capacity.query_plans.plans[{index}].unexpected_sequential_scans must be an array"
            )
            unexpected = []
        exception_relations: set[str] = set()
        for exception_index, raw_exception in enumerate(exceptions):
            exception = _object(
                raw_exception,
                f"capacity.query_plans.plans[{index}].sequential_scan_exceptions[{exception_index}]",
                errors,
            )
            if exception is None:
                continue
            relation = exception.get("relation")
            estimated_rows = exception.get("estimated_rows")
            maximum_allowed = exception.get("maximum_allowed_rows")
            if not isinstance(relation, str) or not relation:
                errors.append("sequential scan exception relation must be a non-empty string")
                continue
            exception_relations.add(relation)
            if (
                not isinstance(estimated_rows, int)
                or isinstance(estimated_rows, bool)
                or estimated_rows < 0
            ):
                errors.append(
                    f"sequential scan exception {relation!r} estimated_rows must be non-negative"
                )
            if (
                not isinstance(maximum_allowed, int)
                or isinstance(maximum_allowed, bool)
                or maximum_allowed < 0
            ):
                errors.append(
                    f"sequential scan exception {relation!r} maximum_allowed_rows must be "
                    "non-negative"
                )
            elif isinstance(estimated_rows, int) and estimated_rows > maximum_allowed:
                errors.append(
                    f"sequential scan exception {relation!r} exceeds maximum_allowed_rows"
                )
            if relation not in relation_counts:
                errors.append(
                    f"sequential scan exception {relation!r} is missing from relation_counts"
                )
            elif isinstance(estimated_rows, int) and relation_counts[relation] != estimated_rows:
                errors.append(
                    f"sequential scan exception {relation!r} estimated_rows does not match "
                    "relation_counts"
                )
            if (
                isinstance(maximum_allowed, int)
                and isinstance(max_seq_scan_rows, int)
                and maximum_allowed != max_seq_scan_rows
            ):
                errors.append(
                    f"sequential scan exception {relation!r} maximum_allowed_rows does not "
                    "match max_seq_scan_rows"
                )
        unexpected_relations: set[str] = set()
        for unexpected_index, raw_unexpected in enumerate(unexpected):
            item = _object(
                raw_unexpected,
                f"capacity.query_plans.plans[{index}].unexpected_sequential_scans[{unexpected_index}]",
                errors,
            )
            if item is None:
                continue
            relation = item.get("relation")
            estimated_rows = item.get("estimated_rows")
            if not isinstance(relation, str) or not relation:
                errors.append("unexpected sequential scan relation must be a non-empty string")
                continue
            unexpected_relations.add(relation)
            if (
                not isinstance(estimated_rows, int)
                or isinstance(estimated_rows, bool)
                or estimated_rows < 0
            ):
                errors.append(
                    f"unexpected sequential scan {relation!r} estimated_rows must be non-negative"
                )
            elif relation in relation_counts and relation_counts[relation] != estimated_rows:
                errors.append(
                    f"unexpected sequential scan {relation!r} estimated_rows does not "
                    "match relation_counts"
                )
        if sequential_relations != exception_relations | unexpected_relations:
            errors.append(
                f"capacity.query_plans.plans[{index}] sequential scan evidence does not "
                "match the plan"
            )
        if unexpected_relations:
            errors.append(
                f"capacity.query_plans.plans[{index}].unexpected_sequential_scans must be empty"
            )
        if not expected_index_observed and not exception_relations:
            errors.append(
                f"capacity.query_plans.plans[{index}] must observe an expected index "
                "or record a bounded sequential-scan exception"
            )
    if set(labels) != EXPECTED_QUERY_PLAN_LABELS or len(labels) != len(EXPECTED_QUERY_PLAN_LABELS):
        errors.append(
            f"capacity.query_plans.plans must contain exactly {sorted(EXPECTED_QUERY_PLAN_LABELS)}"
        )
    if isinstance(all_indexed, bool) and all_indexed is not derived_all_indexed:
        errors.append("capacity.query_plans.all_indexed does not match plan index observations")
    if any(isinstance(plan, dict) and plan.get("accepted") is not True for plan in plans):
        errors.append("capacity.query_plans.succeeded requires every plan to be accepted")


def _validate_result_attachment(
    path: Path,
    *,
    label: str,
    schema_version: str,
    identity_key: str,
    identity: str,
    errors: list[str],
) -> None:
    payload = _object(_read_json(path, label, errors), label, errors)
    if payload is None:
        return
    if payload.get("schema_version") != schema_version:
        errors.append(f"{label}.schema_version must be {schema_version}")
    if payload.get(identity_key) != identity:
        errors.append(f"{label}.{identity_key} must match {identity!r}")
    if payload.get("succeeded") is not True:
        errors.append(f"{label}.succeeded must be true")


def _attachment_datetime(
    value: object,
    *,
    label: str,
    errors: list[str],
) -> datetime | None:
    if not isinstance(value, str):
        errors.append(f"{label} must be an ISO-8601 timestamp")
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        errors.append(f"{label} must be an ISO-8601 timestamp")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        errors.append(f"{label} must include a timezone")
        return None
    return parsed


def validate_record(record_path: Path) -> list[str]:
    """Return all structural, attachment, and cross-evidence violations."""

    errors: list[str] = []
    record_path = record_path.resolve()
    try:
        payload: object = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return [f"cannot read acceptance record {record_path}: {error}"]
    try:
        record = DeploymentAcceptanceRecord.model_validate(payload)
    except ValidationError as error:
        for detail in error.errors(include_url=False, include_input=False):
            location = ".".join(str(part) for part in detail["loc"])
            errors.append(f"{location}: {detail['msg']}")
        return errors

    root = record_path.parent
    evidence_paths: dict[str, Path] = {}
    labels_by_path: dict[str, list[str]] = {}
    for label, evidence in _evidence_files(record):
        labels_by_path.setdefault(evidence.path, []).append(label)
        path = _resolve_evidence(root, evidence, label, errors)
        if path is not None:
            evidence_paths[label] = path
    for path_name, labels in labels_by_path.items():
        if len(labels) > 1:
            errors.append(f"evidence path {path_name!r} is reused by {labels}")

    for index, node in enumerate(record.api_nodes):
        for phase in ("before", "after"):
            label = f"api_nodes[{index}].{phase}"
            path = evidence_paths.get(label)
            if path is not None:
                _validate_smoke(path, node.name, label, errors)

    for index, failover in enumerate(record.failovers):
        label = f"failovers[{index}].evidence"
        path = evidence_paths.get(label)
        if path is not None:
            _validate_result_attachment(
                path,
                label=label,
                schema_version="dependency-failover-v1",
                identity_key="dependency",
                identity=failover.dependency,
                errors=errors,
            )
            payload = _object(_read_json(path, label, errors), label, errors)
            if payload is not None:
                started_at = _attachment_datetime(
                    payload.get("started_at"),
                    label=f"{label}.started_at",
                    errors=errors,
                )
                recovered_at = _attachment_datetime(
                    payload.get("recovered_at"),
                    label=f"{label}.recovered_at",
                    errors=errors,
                )
                if started_at is not None and started_at != failover.started_at:
                    errors.append(f"{label}.started_at does not match the failover record")
                if recovered_at is not None and recovered_at != failover.recovered_at:
                    errors.append(f"{label}.recovered_at does not match the failover record")
                if payload.get("client_errors") != failover.client_errors:
                    errors.append(f"{label}.client_errors does not match the failover record")

    for index, workflow in enumerate(record.workflows):
        label = f"workflows[{index}].evidence"
        path = evidence_paths.get(label)
        if path is not None:
            _validate_result_attachment(
                path,
                label=label,
                schema_version="workflow-check-v1",
                identity_key="name",
                identity=workflow.name,
                errors=errors,
            )

    benchmark_path = evidence_paths.get("capacity.benchmark")
    if benchmark_path is not None:
        _validate_benchmark(benchmark_path, record.capacity, errors)
    upload_limit_path = evidence_paths.get("capacity.upload_limit")
    if upload_limit_path is not None:
        _validate_upload_limit(upload_limit_path, errors)
    plans_path = evidence_paths.get("capacity.query_plans")
    if plans_path is not None:
        _validate_query_plans(plans_path, record.capacity, errors)
    rate_limit_path = evidence_paths.get("capacity.shared_rate_limit")
    if rate_limit_path is not None:
        payload = _object(
            _read_json(rate_limit_path, "capacity.shared_rate_limit", errors),
            "capacity.shared_rate_limit",
            errors,
        )
        if payload is not None:
            if payload.get("schema_version") != "shared-rate-limit-v1":
                errors.append(
                    "capacity.shared_rate_limit.schema_version must be shared-rate-limit-v1"
                )
            if payload.get("api_node_count") != record.capacity.shared_rate_limit_node_count:
                errors.append(
                    "capacity.shared_rate_limit.api_node_count does not match "
                    "shared_rate_limit_node_count"
                )
            origins = payload.get("api_origins")
            observations = payload.get("observations")
            if not isinstance(origins, list) or not all(
                isinstance(origin, str) and origin.startswith("https://") for origin in origins
            ):
                errors.append("capacity.shared_rate_limit.api_origins must be HTTPS strings")
                origins = []
            if len(origins) != record.capacity.shared_rate_limit_node_count or len(
                set(origins)
            ) != len(origins):
                errors.append(
                    "capacity.shared_rate_limit.api_origins must match the node count and be unique"
                )
            if not isinstance(observations, list) or not observations:
                errors.append("capacity.shared_rate_limit.observations must be a non-empty array")
                observations = []
            seen_origins: set[str] = set()
            remaining_values: list[int] = []
            policies: set[str] = set()
            for observation_index, raw_observation in enumerate(observations):
                observation = _object(
                    raw_observation,
                    f"capacity.shared_rate_limit.observations[{observation_index}]",
                    errors,
                )
                if observation is None:
                    continue
                origin = observation.get("origin")
                if not isinstance(origin, str) or origin not in origins:
                    errors.append(
                        "capacity.shared_rate_limit observation origin is not in api_origins"
                    )
                else:
                    seen_origins.add(origin)
                remaining = observation.get("remaining")
                if not isinstance(remaining, int) or isinstance(remaining, bool) or remaining < 0:
                    errors.append(
                        "capacity.shared_rate_limit observation remaining must be non-negative"
                    )
                else:
                    remaining_values.append(remaining)
                policy = observation.get("policy")
                if not isinstance(policy, str) or not policy:
                    errors.append("capacity.shared_rate_limit observation policy must be non-empty")
                else:
                    policies.add(policy)
                if observation.get("status_code") not in (200, 429):
                    errors.append(
                        "capacity.shared_rate_limit observation status_code must be 200 or 429"
                    )
            rejection_observed = any(
                isinstance(item, dict) and item.get("status_code") == 429 for item in observations
            )
            all_nodes_observed = seen_origins == set(origins)
            non_rejection_remaining = [
                int(item["remaining"])
                for item in observations
                if isinstance(item, dict)
                and item.get("status_code") != 429
                and isinstance(item.get("remaining"), int)
            ]
            shared_budget = bool(
                non_rejection_remaining
                and rejection_observed
                and non_rejection_remaining[-1] == 0
                and all(
                    current == previous - 1
                    for previous, current in zip(
                        non_rejection_remaining,
                        non_rejection_remaining[1:],
                        strict=False,
                    )
                )
                and len(policies) == 1
            )
            for key, expected in (
                ("shared_budget", shared_budget),
                ("rejection_observed", rejection_observed),
                ("all_nodes_observed", all_nodes_observed),
            ):
                if payload.get(key) is not expected:
                    errors.append(f"capacity.shared_rate_limit.{key} does not match observations")
            for key in ("tls", "succeeded"):
                if payload.get(key) is not True:
                    errors.append(f"capacity.shared_rate_limit.{key} must be true")
            if payload.get("succeeded") is True and not (shared_budget and all_nodes_observed):
                errors.append(
                    "capacity.shared_rate_limit.succeeded requires valid shared observations"
                )

    fail_closed_path = evidence_paths.get("capacity.rate_limit_fail_closed")
    if fail_closed_path is not None:
        payload = _object(
            _read_json(fail_closed_path, "capacity.rate_limit_fail_closed", errors),
            "capacity.rate_limit_fail_closed",
            errors,
        )
        if payload is not None:
            if payload.get("schema_version") != "rate-limit-fail-closed-v1":
                errors.append(
                    "capacity.rate_limit_fail_closed.schema_version must be "
                    "rate-limit-fail-closed-v1"
                )
            if payload.get("api_node_count") != record.capacity.shared_rate_limit_node_count:
                errors.append(
                    "capacity.rate_limit_fail_closed.api_node_count does not match "
                    "shared_rate_limit_node_count"
                )
            origins = payload.get("api_origins")
            observations = payload.get("observations")
            if not isinstance(origins, list) or not all(
                isinstance(origin, str) and origin.startswith("https://") for origin in origins
            ):
                errors.append("capacity.rate_limit_fail_closed.api_origins must be HTTPS strings")
                origins = []
            if len(origins) != record.capacity.shared_rate_limit_node_count or len(
                set(origins)
            ) != len(origins):
                errors.append(
                    "capacity.rate_limit_fail_closed.api_origins must match the node count "
                    "and be unique"
                )
            if not isinstance(observations, list) or len(observations) != len(origins):
                errors.append(
                    "capacity.rate_limit_fail_closed.observations must contain one item per "
                    "API origin"
                )
                observations = []
            observed_origins: set[str] = set()
            for observation_index, raw_observation in enumerate(observations):
                observation = _object(
                    raw_observation,
                    f"capacity.rate_limit_fail_closed.observations[{observation_index}]",
                    errors,
                )
                if observation is None:
                    continue
                origin = observation.get("origin")
                if not isinstance(origin, str) or origin not in origins:
                    errors.append(
                        "capacity.rate_limit_fail_closed observation origin is not in api_origins"
                    )
                else:
                    observed_origins.add(origin)
                if observation.get("status_code") != 503:
                    errors.append(
                        "capacity.rate_limit_fail_closed observations must return HTTP 503"
                    )
                if observation.get("error_code") != "rate_limit_backend_unavailable":
                    errors.append(
                        "capacity.rate_limit_fail_closed observations must expose the "
                        "backend error code"
                    )
                if observation.get("cache_control") != "no-store":
                    errors.append(
                        "capacity.rate_limit_fail_closed observations must set Cache-Control: "
                        "no-store"
                    )
                if observation.get("retry_after") != "1":
                    errors.append(
                        "capacity.rate_limit_fail_closed observations must set Retry-After: 1"
                    )
            fail_closed = len(observed_origins) == len(origins) and len(observed_origins) == len(
                set(origins)
            )
            if payload.get("fail_closed") is not fail_closed:
                errors.append(
                    "capacity.rate_limit_fail_closed.fail_closed does not match observations"
                )
            for key in ("tls", "succeeded"):
                if payload.get(key) is not True:
                    errors.append(f"capacity.rate_limit_fail_closed.{key} must be true")
            if payload.get("succeeded") is True and not fail_closed:
                errors.append(
                    "capacity.rate_limit_fail_closed.succeeded requires every node to fail closed"
                )

    backup_path = evidence_paths.get("restore.backup_manifest")
    if backup_path is not None:
        payload = _object(
            _read_json(backup_path, "restore.backup_manifest", errors),
            "restore.backup_manifest",
            errors,
        )
        if payload is not None:
            if payload.get("schema_version") != "backup-manifest-v1":
                errors.append("restore.backup_manifest.schema_version must be backup-manifest-v1")
            if payload.get("backup_id") != record.restore.backup_id:
                errors.append("restore.backup_manifest.backup_id does not match restore.backup_id")
            for key in (
                "postgresql_snapshot",
                "object_replication_watermark",
                "oidc_backup",
                "secret_backup",
            ):
                if not isinstance(payload.get(key), str) or not payload[key]:
                    errors.append(f"restore.backup_manifest.{key} must be a non-empty string")
            if payload.get("succeeded") is not True:
                errors.append("restore.backup_manifest.succeeded must be true")

    source_path = evidence_paths.get("restore.source_manifest")
    restore_path = evidence_paths.get("restore.restore_validation")
    if source_path is not None and restore_path is not None:
        _validate_restore(source_path, restore_path, errors)

    for label, expected_kind in (
        ("restore.oidc_backup_evidence", "oidc"),
        ("restore.secret_backup_evidence", "runtime-secrets"),
    ):
        path = evidence_paths.get(label)
        if path is None:
            continue
        payload = _object(_read_json(path, label, errors), label, errors)
        if payload is None:
            continue
        if payload.get("schema_version") != "backup-receipt-v1":
            errors.append(f"{label}.schema_version must be backup-receipt-v1")
        if payload.get("backup_id") != record.restore.backup_id:
            errors.append(f"{label}.backup_id does not match restore.backup_id")
        if payload.get("kind") != expected_kind:
            errors.append(f"{label}.kind must be {expected_kind!r}")
        if payload.get("succeeded") is not True:
            errors.append(f"{label}.succeeded must be true")

    for index, monitoring in enumerate(record.monitoring):
        for phase, key in (("trigger_evidence", "triggered"), ("recovery_evidence", "recovered")):
            label = f"monitoring[{index}].{phase}"
            path = evidence_paths.get(label)
            if path is None:
                continue
            payload = _object(_read_json(path, label, errors), label, errors)
            if payload is None:
                continue
            if payload.get("schema_version") != "monitoring-check-v1":
                errors.append(f"{label}.schema_version must be monitoring-check-v1")
            if payload.get("signal") != monitoring.signal:
                errors.append(f"{label}.signal must match {monitoring.signal!r}")
            if payload.get(key) is not True:
                errors.append(f"{label}.{key} must be true")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path, nargs="?", help="JSON deployment acceptance record")
    parser.add_argument(
        "--print-schema",
        action="store_true",
        help="print the deployment-acceptance-v1 JSON Schema and exit",
    )
    arguments = parser.parse_args()
    if arguments.print_schema:
        print(json.dumps(DeploymentAcceptanceRecord.model_json_schema(), indent=2, sort_keys=True))
        return 0
    if arguments.record is None:
        parser.error("record is required unless --print-schema is used")
    record_path = arguments.record.resolve()
    errors = validate_record(record_path)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    record = DeploymentAcceptanceRecord.model_validate(payload)
    print(
        json.dumps(
            {
                "record": str(record_path),
                "schema_version": record.schema_version,
                "deployment_id": record.release.deployment_id,
                "api_nodes": len(record.api_nodes),
                "failovers": len(record.failovers),
                "measured_rto_seconds": record.restore.measured_rto_seconds,
                "measured_rpo_seconds": record.restore.measured_rpo_seconds,
                "release_decision": record.release_decision,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
