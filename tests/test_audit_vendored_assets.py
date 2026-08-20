from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import cast

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts/audit_vendored_assets.py"
MANIFEST_PATH = REPOSITORY_ROOT / "frontend/public/vendor/chemdoodle/manifest.json"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("audit_vendored_assets", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checked_in_vendor_manifest_matches_assets() -> None:
    module = _load_script()
    audit = cast(Callable[..., list[str]], module.audit_manifest)

    def empty_osv_response(payload: dict[str, object]) -> dict[str, object]:
        queries = cast(list[object], payload["queries"])
        return {"results": [{} for _ in queries]}

    assert (
        audit(
            MANIFEST_PATH,
            osv_client=empty_osv_response,
        )
        == []
    )


def test_vendor_audit_reports_hash_drift(tmp_path: Path) -> None:
    module = _load_script()
    audit = cast(Callable[..., list[str]], module.audit_manifest)
    asset_path = tmp_path / "library.js"
    asset_path.write_text("library 1.0.0\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "components": {
                    "library": {
                        "version": "1.0.0",
                        "source": "https://example.invalid/library.js",
                        "license": "MIT",
                    }
                },
                "assets": [
                    {
                        "path": "library.js",
                        "component": "library",
                        "version_marker": "library 1.0.0",
                        "sha256": "0" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    errors = audit(manifest_path, check_osv=False)

    assert len(errors) == 1
    assert errors[0].startswith("SHA-256 mismatch for library.js")


def test_vendor_audit_reports_osv_findings(tmp_path: Path) -> None:
    module = _load_script()
    audit = cast(Callable[..., list[str]], module.audit_manifest)
    payload = b"library 1.0.0\n"
    import hashlib

    (tmp_path / "library.js").write_bytes(payload)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "components": {
                    "library": {
                        "version": "1.0.0",
                        "source": "https://example.invalid/library.js",
                        "license": "MIT",
                        "osv": {"ecosystem": "npm", "name": "library"},
                    }
                },
                "assets": [
                    {
                        "path": "library.js",
                        "component": "library",
                        "version_marker": "library 1.0.0",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    errors = audit(
        manifest_path,
        osv_client=lambda _payload: {"results": [{"vulns": [{"id": "GHSA-test"}]}]},
    )

    assert errors == ["OSV findings for library@1.0.0: GHSA-test"]
