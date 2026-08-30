from pathlib import Path
from uuid import UUID

import pytest

from tricycle_reaction_db.dev.reconcile_reaction_geometries import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_STATEMENT_TIMEOUT_MS,
    _arguments,
)

REPOSITORY_ROOT = Path(__file__).parents[2]


def test_reaction_geometry_maintenance_arguments_support_bounded_resume() -> None:
    reaction_id = UUID("00000000-0000-7000-8000-000000000123")
    arguments = _arguments(
        [
            "--batch-size",
            "25",
            "--limit",
            "75",
            "--mapped-reaction-id",
            str(reaction_id),
            "--dry-run",
        ]
    )

    assert arguments.batch_size == 25
    assert arguments.limit == 75
    assert arguments.mapped_reaction_id == reaction_id
    assert arguments.dry_run is True


def test_reaction_geometry_maintenance_arguments_validate_ranges() -> None:
    defaults = _arguments([])
    assert defaults.batch_size == DEFAULT_BATCH_SIZE
    assert defaults.statement_timeout_ms == DEFAULT_STATEMENT_TIMEOUT_MS

    with pytest.raises(SystemExit):
        _arguments(["--batch-size", "0"])
    with pytest.raises(SystemExit):
        _arguments(["--limit", "0"])
    with pytest.raises(SystemExit):
        _arguments(["--reaction-timeout-seconds", "0"])
    with pytest.raises(SystemExit):
        _arguments(
            [
                "--mapped-reaction-id",
                "00000000-0000-7000-8000-000000000123",
                "--start-after",
                "00000000-0000-7000-8000-000000000122",
            ]
        )


def test_reaction_geometry_maintenance_is_registered_as_project_command() -> None:
    project = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    makefile = (REPOSITORY_ROOT / "Makefile").read_text(encoding="utf-8")

    assert "tricycle-reconcile-reaction-geometries" in project
    assert "reconcile-reaction-geometries:" in makefile
