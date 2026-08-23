import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tricycle_reaction_db.core.config import Settings
from tricycle_reaction_db.storage.rustfs import RustFSSettings

REPOSITORY_ROOT = Path(__file__).parents[2]


def _dotenv_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        values[name] = value
    return values


def test_maintenance_timers_run_oneshot_commands_outside_api_workers() -> None:
    systemd = REPOSITORY_ROOT / "infra/systemd"
    session_service = (systemd / "reaction-database-session-cleanup.service").read_text()
    gc_service = (systemd / "reaction-database-rustfs-gc.service").read_text()
    session_timer = (systemd / "reaction-database-session-cleanup.timer").read_text()
    gc_timer = (systemd / "reaction-database-rustfs-gc.timer").read_text()

    assert "Type=oneshot" in session_service
    assert "tricycle-auth-session-cleanup" in session_service
    assert "Type=oneshot" in gc_service
    assert "tricycle-rustfs-gc" in gc_service
    assert "Persistent=true" in session_timer
    assert "Persistent=true" in gc_timer


def test_api_systemd_unit_runs_one_hardened_process_per_host() -> None:
    service = (REPOSITORY_ROOT / "infra/systemd/reaction-database-api.service").read_text()

    assert "Type=exec" in service
    assert "ExecStart=/opt/reaction-database/.venv/bin/tricycle-api" in service
    assert "Restart=on-failure" in service
    assert "NoNewPrivileges=true" in service
    assert "PrivateTmp=true" in service
    assert "ProtectSystem=strict" in service


def test_multi_host_api_environment_is_a_valid_production_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in tuple(os.environ):
        if name.startswith("TRICYCLE_"):
            monkeypatch.delenv(name)
    environment_file = REPOSITORY_ROOT / "infra/deployment/multi-host-api.env.example"

    settings = Settings(_env_file=environment_file)
    storage = RustFSSettings(_env_file=environment_file)

    assert settings.environment == "production"
    assert settings.app_name == "Example Chemistry Database"
    assert settings.nexusx_database_cluster_name == "example-chemistry-postgresql"
    assert settings.nexusx_database_cluster_color == "#E3F2FD"
    assert urlsplit(settings.database_url).hostname == "db-rw.internal.example"
    assert urlsplit(settings.rate_limit_redis_url or "").hostname == ("redis-rw.internal.example")
    assert urlsplit(storage.endpoint_url).hostname == "s3.internal.example"
    assert storage.bucket == "example-chemistry-raw-files"


def test_frontend_csrf_example_matches_backend_runtime_names() -> None:
    backend = _dotenv_values(REPOSITORY_ROOT / ".env.example")
    frontend = _dotenv_values(REPOSITORY_ROOT / "frontend/.env.example")

    assert frontend["VITE_CSRF_COOKIE_NAME"] == backend["TRICYCLE_CSRF_COOKIE_NAME"]
    assert frontend["VITE_CSRF_HEADER_NAME"] == backend["TRICYCLE_CSRF_HEADER_NAME"]


def test_monitoring_rules_cover_external_dependencies_and_maintenance() -> None:
    rules = (REPOSITORY_ROOT / "infra/monitoring/prometheus-rules.yml").read_text()

    for signal in (
        "reaction-database-live",
        "reaction-database-ready",
        "reaction-database-postgresql",
        "reaction-database-rustfs",
        "reaction-database-oidc-discovery",
        "reaction-database-smtp-starttls",
        "reaction-database-api.service",
        "reaction-database-session-cleanup.service",
        "reaction-database-rustfs-gc.service",
    ):
        assert signal in rules


def test_monitoring_rules_cover_application_runtime_failures() -> None:
    rules = (REPOSITORY_ROOT / "infra/monitoring/prometheus-rules.yml").read_text()

    for metric in (
        "tricycle_database_pool_connections",
        "tricycle_database_statement_timeouts_total",
        "tricycle_upload_operations_total",
        'tricycle_artifact_storage_rows{status="pending"}',
        'tricycle_artifact_ingestion_rows{status="failed"}',
        'tricycle_artifact_storage_rows{status=~"missing|corrupt"}',
        "tricycle_storage_failures_total",
        "tricycle_oidc_callbacks_total",
        "tricycle_smtp_deliveries_total",
        "tricycle_mcp_active_connections",
        "tricycle_rate_limit_decisions_total",
    ):
        assert metric in rules
    assert '>= ignoring(state) tricycle_database_pool_connections{state="size"}' in rules


def test_ci_executes_real_operations_and_shared_redis_validators() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()

    assert "caddy:2.10.2-alpine caddy validate" in workflow
    assert "python3 scripts/validate_caddy_runtime.py" in workflow
    assert "promtool check rules infra/monitoring/prometheus-rules.yml" in workflow
    assert "promtool test rules prometheus-rules.test.yml" in workflow
    assert "sudo systemd-analyze verify infra/systemd/*.service infra/systemd/*.timer" in workflow
    assert "TRICYCLE_RUN_REDIS_TESTS=1" in workflow
    assert "TRICYCLE_RATE_LIMIT_REDIS_URL=redis://127.0.0.1:6379/15" in workflow
    assert "uv run --frozen pytest -m redis" in workflow


def test_ci_validates_da_bench_fixture_before_seeding() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()

    validation = "uv run --frozen python scripts/validate_da_bench_fixture.py"
    seed = "uv run --frozen tricycle-seed-da-bench"
    assert validation in workflow
    assert workflow.index(validation) < workflow.index(seed)
    assert "validate-da-bench-fixture" in (REPOSITORY_ROOT / "Makefile").read_text()


def test_multi_host_runbook_exposes_a_repeatable_dependency_smoke_and_evidence_template() -> None:
    project = (REPOSITORY_ROOT / "pyproject.toml").read_text()
    deployment_guide = (REPOSITORY_ROOT / "docs/deployment-configuration.md").read_text()
    operations_runbook = (REPOSITORY_ROOT / "docs/operations-runbook.md").read_text()
    acceptance_record = (
        REPOSITORY_ROOT / "infra/deployment/acceptance-record.example.md"
    ).read_text()

    assert "tricycle-deployment-smoke" in project
    assert "tricycle-deployment-smoke" in deployment_guide
    assert "tricycle-deployment-smoke" in operations_runbook
    assert "PostgreSQL writer" in acceptance_record
    assert "Measured RTO" in acceptance_record
    assert "Measured RPO" in acceptance_record


def test_deployment_acceptance_validator_is_exposed_for_target_evidence() -> None:
    project = (REPOSITORY_ROOT / "pyproject.toml").read_text()
    runbook = (REPOSITORY_ROOT / "docs/operations-runbook.md").read_text()
    makefile = (REPOSITORY_ROOT / "Makefile").read_text()

    assert "tricycle-validate-deployment-acceptance" in project
    assert "tricycle-validate-deployment-acceptance" in runbook
    assert "validate-deployment-acceptance" in makefile
    assert "capture-query-plan-evidence" in makefile
    assert "probe-shared-rate-limit" in makefile
    assert "probe_shared_rate_limit.py" in makefile
    assert "probe-upload-limit" in makefile
    assert "probe_upload_limit.py" in makefile
    assert "query-plan-evidence-v1" in runbook
    assert "upload-resource-benchmark-v2" in runbook
    assert "upload-limit-probe-v1" in runbook


def test_deployment_smoke_and_acceptance_schemas_are_versioned() -> None:
    smoke = (REPOSITORY_ROOT / "src/tricycle_reaction_db/dev/deployment_smoke.py").read_text()
    acceptance = (
        REPOSITORY_ROOT / "src/tricycle_reaction_db/dev/deployment_acceptance.py"
    ).read_text()

    assert 'DEPLOYMENT_SMOKE_SCHEMA_VERSION = "deployment-smoke-v1"' in smoke
    assert 'DEPLOYMENT_ACCEPTANCE_SCHEMA_VERSION = "deployment-acceptance-v1"' in acceptance
    assert (
        'RESTORE_VALIDATION_SCHEMA_VERSION = "restore-validation-v1"'
        in (REPOSITORY_ROOT / "src/tricycle_reaction_db/dev/restore_validation.py").read_text()
    )
