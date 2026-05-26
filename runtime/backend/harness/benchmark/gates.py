from __future__ import annotations

from pydantic import BaseModel, Field

from backend.harness.benchmark.metrics import BenchmarkReport


class BenchmarkGateThresholds(BaseModel):
    min_end_to_end_success_rate: float = 0.8
    min_acceptable_success_rate: float | None = None
    min_pptx_exists_rate: float = 0.9
    min_preview_success_rate: float = 0.7
    min_tool_call_success_rate: float = 0.8
    max_avg_content_issue_count: float | None = None
    min_avg_visual_score: float | None = None


class BenchmarkGateResult(BaseModel):
    passed: bool
    reasons: list[str] = Field(default_factory=list)


def evaluate_benchmark_gate(
    report: BenchmarkReport,
    thresholds: BenchmarkGateThresholds,
) -> BenchmarkGateResult:
    reasons: list[str] = []
    _check_min(
        reasons,
        "end_to_end_success_rate",
        report.end_to_end_success_rate,
        thresholds.min_end_to_end_success_rate,
    )
    if thresholds.min_acceptable_success_rate is not None:
        _check_min(
            reasons,
            "acceptable_success_rate",
            report.acceptable_success_rate,
            thresholds.min_acceptable_success_rate,
        )
    _check_min(reasons, "pptx_exists_rate", report.pptx_exists_rate, thresholds.min_pptx_exists_rate)
    _check_min(reasons, "preview_success_rate", report.preview_success_rate, thresholds.min_preview_success_rate)
    _check_min(
        reasons,
        "tool_call_success_rate",
        report.tool_call_success_rate,
        thresholds.min_tool_call_success_rate,
    )
    if thresholds.max_avg_content_issue_count is not None:
        _check_max(
            reasons,
            "avg_content_issue_count",
            report.avg_content_issue_count,
            thresholds.max_avg_content_issue_count,
        )
    if thresholds.min_avg_visual_score is not None:
        _check_min(reasons, "avg_visual_score", report.avg_visual_score, thresholds.min_avg_visual_score)
    return BenchmarkGateResult(passed=not reasons, reasons=reasons)


def _check_min(reasons: list[str], name: str, value: float | None, threshold: float) -> None:
    if value is None:
        reasons.append(f"{name} unavailable")
    elif value < threshold:
        reasons.append(f"{name} {value:.4f} < {threshold:.4f}")


def _check_max(reasons: list[str], name: str, value: float | None, threshold: float) -> None:
    if value is None:
        reasons.append(f"{name} unavailable")
    elif value > threshold:
        reasons.append(f"{name} {value:.4f} > {threshold:.4f}")
