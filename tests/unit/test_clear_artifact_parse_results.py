from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path
from uuid import UUID

import pytest

_MODULE_SPEC = importlib.util.spec_from_file_location(
    "clear_artifact_parse_results",
    Path(__file__).parents[2] / "scripts/clear_artifact_parse_results.py",
)
assert _MODULE_SPEC is not None and _MODULE_SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_MODULE_SPEC)
_MODULE_SPEC.loader.exec_module(_MODULE)
_requested_ids = _MODULE._requested_ids


def test_cleanup_command_collects_and_deduplicates_id_file(tmp_path: Path) -> None:
    first = UUID("01a086db-147e-7cdf-9abe-9c70c2c70ec8")
    second = UUID("01a09ae5-346f-77e2-b515-abe77a6e77a8")
    ids_file = tmp_path / "artifact-ids.txt"
    ids_file.write_text(
        f"{first}, {second}\n# duplicate is harmless\n{first}\n",
        encoding="utf-8",
    )

    arguments = Namespace(
        artifact_ids=[second],
        artifact_id_files=[ids_file],
    )

    assert _requested_ids(arguments) == (second, first)


def test_cleanup_command_requires_an_id() -> None:
    arguments = Namespace(artifact_ids=None, artifact_id_files=None)

    with pytest.raises(ValueError, match="at least one"):
        _requested_ids(arguments)
