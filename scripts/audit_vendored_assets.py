"""Verify vendored browser assets against their manifest and OSV."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast
from urllib.request import Request, urlopen

JsonObject = dict[str, object]
OsvClient = Callable[[JsonObject], JsonObject]

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPOSITORY_ROOT / "frontend/public/vendor/chemdoodle/manifest.json"
OSV_QUERY_BATCH_URL = "https://api.osv.dev/v1/querybatch"


class ManifestError(ValueError):
    """Raised when the vendored asset manifest does not satisfy its contract."""


def _mapping(value: object, label: str) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ManifestError(f"{label} must be an object with string keys")
    return cast(JsonObject, value)


def _string(mapping: JsonObject, key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{label}.{key} must be a non-empty string")
    return value


def load_manifest(path: Path) -> JsonObject:
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc

    manifest = _mapping(value, "manifest")
    if manifest.get("schema_version") != 1:
        raise ManifestError("manifest.schema_version must be 1")

    components = _mapping(manifest.get("components"), "manifest.components")
    for name, raw_component in components.items():
        component = _mapping(raw_component, f"component {name}")
        _string(component, "version", f"component {name}")
        source = _string(component, "source", f"component {name}")
        if not source.startswith("https://"):
            raise ManifestError(f"component {name}.source must use HTTPS")
        _string(component, "license", f"component {name}")
        if "osv" in component:
            osv = _mapping(component["osv"], f"component {name}.osv")
            _string(osv, "ecosystem", f"component {name}.osv")
            _string(osv, "name", f"component {name}.osv")

    assets = manifest.get("assets")
    if not isinstance(assets, list) or not assets:
        raise ManifestError("manifest.assets must be a non-empty array")
    seen_paths: set[str] = set()
    for index, raw_asset in enumerate(assets):
        asset = _mapping(raw_asset, f"asset {index}")
        relative_path = _string(asset, "path", f"asset {index}")
        component_name = _string(asset, "component", f"asset {index}")
        digest = _string(asset, "sha256", f"asset {index}")
        if component_name not in components:
            raise ManifestError(
                f"asset {relative_path} references unknown component {component_name}"
            )
        if relative_path in seen_paths:
            raise ManifestError(f"duplicate asset path: {relative_path}")
        seen_paths.add(relative_path)
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ManifestError(f"asset {relative_path} has an invalid SHA-256")
        if "version_marker" in asset:
            _string(asset, "version_marker", f"asset {index}")
    return manifest


def _post_osv(payload: JsonObject) -> JsonObject:
    request = Request(
        OSV_QUERY_BATCH_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "tricycle-vendor-audit/1"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed HTTPS OSV endpoint
        value: object = json.loads(response.read())
    return _mapping(value, "OSV response")


def audit_manifest(
    manifest_path: Path,
    *,
    check_osv: bool = True,
    osv_client: OsvClient = _post_osv,
) -> list[str]:
    manifest = load_manifest(manifest_path)
    components = _mapping(manifest["components"], "manifest.components")
    assets = cast(list[object], manifest["assets"])
    asset_root = manifest_path.resolve().parent
    errors: list[str] = []

    for index, raw_asset in enumerate(assets):
        asset = _mapping(raw_asset, f"asset {index}")
        relative_path = _string(asset, "path", f"asset {index}")
        path_fragment = Path(relative_path)
        if path_fragment.is_absolute() or ".." in path_fragment.parts:
            errors.append(f"asset path escapes manifest directory: {relative_path}")
            continue
        asset_path = (asset_root / path_fragment).resolve()
        try:
            asset_path.relative_to(asset_root)
            payload = asset_path.read_bytes()
        except (OSError, ValueError) as exc:
            errors.append(f"cannot read asset {relative_path}: {exc}")
            continue

        actual_digest = hashlib.sha256(payload).hexdigest()
        expected_digest = _string(asset, "sha256", f"asset {index}")
        if actual_digest != expected_digest:
            errors.append(
                f"SHA-256 mismatch for {relative_path}: expected {expected_digest}, "
                f"got {actual_digest}"
            )
        marker = asset.get("version_marker")
        if isinstance(marker, str) and marker.encode("utf-8") not in payload:
            errors.append(f"version marker {marker!r} is missing from {relative_path}")

    if not check_osv:
        return errors

    query_components: list[str] = []
    queries: list[object] = []
    for name, raw_component in components.items():
        component = _mapping(raw_component, f"component {name}")
        raw_osv = component.get("osv")
        if raw_osv is None:
            continue
        osv = _mapping(raw_osv, f"component {name}.osv")
        query_components.append(name)
        queries.append(
            {
                "package": {
                    "ecosystem": _string(osv, "ecosystem", f"component {name}.osv"),
                    "name": _string(osv, "name", f"component {name}.osv"),
                },
                "version": _string(component, "version", f"component {name}"),
            }
        )

    if not queries:
        return errors

    response = osv_client({"queries": queries})
    results = response.get("results")
    if not isinstance(results, list) or len(results) != len(query_components):
        raise ManifestError("OSV response.results does not match the submitted query count")
    for component_name, raw_result in zip(query_components, results, strict=True):
        result = _mapping(raw_result, f"OSV result for {component_name}")
        raw_vulnerabilities = result.get("vulns", [])
        if not isinstance(raw_vulnerabilities, list):
            raise ManifestError(f"OSV vulnerabilities for {component_name} must be an array")
        vulnerability_ids: list[str] = []
        for raw_vulnerability in raw_vulnerabilities:
            vulnerability = _mapping(raw_vulnerability, f"OSV vulnerability for {component_name}")
            vulnerability_ids.append(_string(vulnerability, "id", "OSV vulnerability"))
        if vulnerability_ids:
            version = _string(
                _mapping(components[component_name], f"component {component_name}"),
                "version",
                f"component {component_name}",
            )
            errors.append(
                f"OSV findings for {component_name}@{version}: {', '.join(vulnerability_ids)}"
            )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="verify manifest metadata, version markers, and hashes without querying OSV",
    )
    arguments = parser.parse_args()
    try:
        errors = audit_manifest(arguments.manifest, check_osv=not arguments.offline)
        manifest = load_manifest(arguments.manifest)
    except (ManifestError, OSError, ValueError) as exc:
        print(f"vendored asset audit failed: {exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"vendored asset audit failed: {error}", file=sys.stderr)
        return 1
    asset_count = len(cast(list[object], manifest["assets"]))
    mode = "hash/version" if arguments.offline else "hash/version/OSV"
    print(f"vendored asset audit passed ({mode}): {asset_count} assets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
