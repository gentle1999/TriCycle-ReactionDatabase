import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_BENCHMARK_PATH = Path(__file__).resolve().parents[2] / "scripts/benchmark_real_world_ingestion.py"
_BENCHMARK_SPEC = importlib.util.spec_from_file_location(
    "benchmark_real_world_ingestion", _BENCHMARK_PATH
)
assert _BENCHMARK_SPEC is not None and _BENCHMARK_SPEC.loader is not None
benchmark = importlib.util.module_from_spec(_BENCHMARK_SPEC)
sys.modules[_BENCHMARK_SPEC.name] = benchmark
_BENCHMARK_SPEC.loader.exec_module(benchmark)


@pytest.mark.asyncio
async def test_benchmark_queues_files_before_starting_parse_timing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = (tmp_path / "first.log", tmp_path / "second.log")
    for path in paths:
        path.write_text("fixture", encoding="utf-8")

    active_parses = 0
    peak_active_parses = 0

    async def fake_parse(*_: object, **__: object) -> object:
        nonlocal active_parses, peak_active_parses
        active_parses += 1
        peak_active_parses = max(peak_active_parses, active_parses)
        try:
            await asyncio.sleep(0.05)
            raise RuntimeError("synthetic parser failure")
        finally:
            active_parses -= 1

    monkeypatch.setattr(benchmark.artifact_uploads, "_run_molop_file_pipeline", fake_parse)

    results, _wall_seconds, _verification_seconds = await benchmark._benchmark(
        paths,
        {path: path for path in paths},
        workers=1,
        parallel_files=2,
    )

    assert peak_active_parses == 1
    assert all(result["status"] == "failed" for result in results)
    assert max(result["queue_wait_seconds"] for result in results) >= 0.02
