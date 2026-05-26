from __future__ import annotations

from pydantic import BaseModel, Field

from backend.harness.benchmark.metrics import BenchmarkReport


class BenchmarkComparison(BaseModel):
    current_benchmark_id: str
    baseline_benchmark_id: str
    regressions: list[str] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)
    deltas: dict[str, float] = Field(default_factory=dict)


def compare_benchmark_reports(
    current: BenchmarkReport,
    baseline: BenchmarkReport,
) -> BenchmarkComparison:
    comparison = BenchmarkComparison(
        current_benchmark_id=current.benchmark_id,
        baseline_benchmark_id=baseline.benchmark_id,
    )
    higher_is_better = {
        "end_to_end_success_rate",
        "pptx_exists_rate",
        "preview_success_rate",
        "tool_call_success_rate",
        "avg_visual_score",
    }
    lower_is_better = {"avg_content_issue_count", "failed_tool_count", "timeout_tool_count"}
    for field_name in sorted(higher_is_better | lower_is_better):
        current_value = getattr(current, field_name)
        baseline_value = getattr(baseline, field_name)
        if current_value is None or baseline_value is None:
            continue
        delta = round(float(current_value) - float(baseline_value), 4)
        comparison.deltas[field_name] = delta
        if field_name in higher_is_better:
            _classify_delta(comparison, field_name, delta, positive_is_improvement=True)
        else:
            _classify_delta(comparison, field_name, delta, positive_is_improvement=False)
    return comparison


def _classify_delta(
    comparison: BenchmarkComparison,
    field_name: str,
    delta: float,
    *,
    positive_is_improvement: bool,
) -> None:
    if delta == 0:
        return
    is_improvement = delta > 0 if positive_is_improvement else delta < 0
    if is_improvement:
        comparison.improvements.append(f"{field_name} improved by {delta:.4f}")
    else:
        comparison.regressions.append(f"{field_name} regressed by {delta:.4f}")
