from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import cast

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts/capture_query_plan_evidence.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("capture_query_plan_evidence", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_query_plan_capture_covers_every_r4_query_family() -> None:
    module = _load_script()
    specs = cast(dict[str, object], module.QUERY_SPECS)

    assert set(specs) == {
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


def test_plan_node_walk_includes_nested_index_and_sequential_scans() -> None:
    module = _load_script()
    walk = cast(Callable[[object], list[dict[str, object]]], module._plan_nodes)

    nodes = walk(
        {
            "Plan": {
                "Node Type": "Nested Loop",
                "Plans": [
                    {
                        "Node Type": "Index Scan",
                        "Index Name": "ix_expected",
                        "Relation Name": "first_table",
                    },
                    {"Node Type": "Seq Scan", "Relation Name": "small_table"},
                ],
            }
        }
    )

    assert [node["Node Type"] for node in nodes] == ["Nested Loop", "Index Scan", "Seq Scan"]
