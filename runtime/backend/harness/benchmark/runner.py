from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from backend.harness.benchmark.cases import BenchmarkSuite
from backend.harness.benchmark.metrics import BenchmarkReport, aggregate_case_results, evaluate_case_from_artifacts
from backend.harness.benchmark.report import write_benchmark_report


class BenchmarkRunner:
    def __init__(
        self,
        *,
        output_root: str | Path,
        runs_root: str | Path | None = None,
    ):
        self.output_root = Path(output_root)
        self.runs_root = Path(runs_root) if runs_root is not None else self.output_root / "runs"

    def run_offline(
        self,
        *,
        suite: BenchmarkSuite,
        benchmark_id: str | None = None,
    ) -> BenchmarkReport:
        resolved_benchmark_id = benchmark_id or _make_benchmark_id(suite.suite_id)
        results = []
        for case in suite.cases:
            run_id = case.run_id or case.case_id
            run_dir = self.runs_root / run_id
            results.append(evaluate_case_from_artifacts(case, run_dir))

        report = aggregate_case_results(
            benchmark_id=resolved_benchmark_id,
            suite_id=suite.suite_id,
            results=results,
        )
        output_dir = self.output_root / "benchmarks" / resolved_benchmark_id
        paths = write_benchmark_report(report, output_dir)
        report = report.model_copy(
            update={
                "metadata": {
                    **report.metadata,
                    "suite_name": suite.name,
                    "case_count": len(suite.cases),
                    "output_paths": paths,
                }
            }
        )
        write_benchmark_report(report, output_dir)
        return report


def _make_benchmark_id(suite_id: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_suite_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in suite_id)
    return f"bench_{safe_suite_id or 'suite'}_{timestamp}"
