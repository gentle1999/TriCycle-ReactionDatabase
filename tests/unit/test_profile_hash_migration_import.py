"""Import-order regression tests for profile hash migrations."""

import subprocess
import sys
from pathlib import Path


def test_stereo_agnostic_hash_migration_imports_without_service_cycle() -> None:
    """Alembic must be able to discover the migration in a fresh interpreter."""

    repository_root = Path(__file__).resolve().parents[2]
    migration_path = (
        repository_root / "migrations" / "versions" / "0057_topology_stereo_agnostic_hash.py"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys; runpy.run_path(sys.argv[1])",
            str(migration_path),
        ],
        cwd=repository_root,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
