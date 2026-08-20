from __future__ import annotations

import importlib.util
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import cast

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests/fixtures/da_bench_minimal"
SCRIPT_PATH = REPOSITORY_ROOT / "scripts/validate_da_bench_fixture.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("validate_da_bench_fixture", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validator() -> Callable[[Path], list[str]]:
    return cast(Callable[[Path], list[str]], _load_script().validate_fixture)


def _copy_fixture(tmp_path: Path) -> Path:
    destination = tmp_path / "da_bench_minimal"
    shutil.copytree(FIXTURE_ROOT, destination)
    return destination


def test_checked_in_da_bench_fixture_matches_manifest() -> None:
    assert _validator()(FIXTURE_ROOT) == []


def test_fixture_validator_reports_log_and_metadata_drift(tmp_path: Path) -> None:
    fixture_root = _copy_fixture(tmp_path)
    manifest_path = fixture_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["logs"][0]["gzip_sha256"] = "0" * 64
    manifest["logs"][1]["source_size_bytes"] += 1
    manifest["logs"][2]["source_sha256"] = "0" * 64
    manifest["metadata_files"][0]["size_bytes"] += 1
    manifest["metadata_files"][1]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    errors = _validator()(fixture_root)

    assert any("gzip SHA-256 mismatch" in error for error in errors)
    assert any("source size mismatch" in error for error in errors)
    assert any("source SHA-256 mismatch" in error for error in errors)
    assert any("metadata_files[0] size mismatch" in error for error in errors)
    assert any("metadata_files[1] SHA-256 mismatch" in error for error in errors)


def test_fixture_validator_rejects_schema_and_traversal_drift(tmp_path: Path) -> None:
    fixture_root = _copy_fixture(tmp_path)
    manifest_path = fixture_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = "da-bench-fixture-v2"
    manifest["logs"][0]["relative_path"] = "../manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    errors = _validator()(fixture_root)

    assert any("schema_version" in error for error in errors)
    assert any("escapes fixture root" in error for error in errors)
