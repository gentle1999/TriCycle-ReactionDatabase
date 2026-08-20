import gzip
import json
import shutil
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from tricycle_reaction_db.application.services.authentication import (
    AuthenticatedPrincipal,
    reset_current_principal,
    reset_request_context_active,
    set_current_principal,
    set_request_context_active,
)
from tricycle_reaction_db.domain.identity import (
    DEVELOPMENT_IDENTITY_ISSUER,
    DEVELOPMENT_IDENTITY_SUBJECT,
    DEVELOPMENT_USER_ID,
)

DA_BENCH_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "da_bench_minimal"


@pytest.fixture
def development_query_principal() -> Iterator[AuthenticatedPrincipal]:
    """Run direct query-service calls with the same principal as development HTTP requests."""

    principal = AuthenticatedPrincipal(
        user_id=DEVELOPMENT_USER_ID,
        display_name="Development User",
        primary_email="developer@localhost",
        is_service_account=False,
        issuer=DEVELOPMENT_IDENTITY_ISSUER,
        subject=DEVELOPMENT_IDENTITY_SUBJECT,
    )
    request_token = set_request_context_active()
    principal_token = set_current_principal(principal)
    try:
        yield principal
    finally:
        reset_current_principal(principal_token)
        reset_request_context_active(request_token)


@pytest.fixture(scope="session")
def da_bench_manifest() -> dict[str, Any]:
    return json.loads((DA_BENCH_FIXTURE_ROOT / "manifest.json").read_text())


@pytest.fixture(scope="session")
def da_bench_log_paths(
    tmp_path_factory: pytest.TempPathFactory,
    da_bench_manifest: dict[str, Any],
) -> dict[str, Path]:
    output_root = tmp_path_factory.mktemp("da-bench-minimal")
    paths: dict[str, Path] = {}
    for entry in da_bench_manifest["logs"]:
        compressed_path = DA_BENCH_FIXTURE_ROOT / entry["relative_path"]
        assert sha256(compressed_path.read_bytes()).hexdigest() == entry["gzip_sha256"]

        output_path = output_root / Path(entry["relative_path"]).with_suffix("")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(compressed_path, "rb") as source, output_path.open("wb") as target:
            shutil.copyfileobj(source, target)

        payload = output_path.read_bytes()
        assert len(payload) == entry["source_size_bytes"]
        assert sha256(payload).hexdigest() == entry["source_sha256"]
        paths[entry["role"]] = output_path
    return paths
