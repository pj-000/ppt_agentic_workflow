from __future__ import annotations

from pathlib import Path

from backend.harness.benchmark.cases import load_benchmark_suite
from backend.harness.benchmark.metrics import BenchmarkReport
from backend.harness.benchmark.runner import BenchmarkRunner


def run_offline_benchmark_from_suite_path(
    *,
    suite_path: str | Path,
    output_root: str | Path,
    runs_root: str | Path | None = None,
    benchmark_id: str | None = None,
) -> BenchmarkReport:
    suite = load_benchmark_suite(suite_path)
    runner = BenchmarkRunner(output_root=output_root, runs_root=runs_root)
    return runner.run_offline(suite=suite, benchmark_id=benchmark_id)
