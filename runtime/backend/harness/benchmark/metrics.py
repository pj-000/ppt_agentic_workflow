from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.harness.benchmark.cases import BenchmarkCase


class BenchmarkCaseResult(BaseModel):
    case_id: str
    run_id: str | None = None
    status: Literal["pass", "fail", "warning", "skipped", "missing_artifacts"] = "missing_artifacts"
    reasons: list[str] = Field(default_factory=list)

    quality_report_exists: bool = False
    trace_summary_exists: bool = False
    pptx_exists: bool | None = None
    preview_success: bool | None = None

    slide_count: int | None = None
    visual_score_avg: float | None = None
    visual_score_min: float | None = None
    content_issue_count: int | None = None
    repaired_slide_count: int | None = None
    repair_attempt_count: int | None = None

    trace_status: str | None = None
    tool_call_count: int = 0
    tool_attempt_count: int = 0
    failed_tool_count: int = 0
    skipped_tool_count: int = 0
    timeout_tool_count: int = 0
    tool_call_success_rate: float | None = None
    error_signatures: list[str] = Field(default_factory=list)

    memory_query_count: int | None = None
    memory_hit_rate: float | None = None

    metrics: dict[str, Any] = Field(default_factory=dict)


class BenchmarkReport(BaseModel):
    benchmark_id: str
    suite_id: str
    status: Literal["pass", "fail", "warning", "empty"] = "empty"
    total_cases: int = 0
    passed_cases: int = 0
    failed_cases: int = 0
    warning_cases: int = 0
    skipped_cases: int = 0
    missing_artifact_cases: int = 0

    end_to_end_success_rate: float | None = None
    acceptable_success_rate: float | None = None
    pptx_exists_rate: float | None = None
    preview_success_rate: float | None = None
    quality_report_exists_rate: float | None = None
    trace_summary_exists_rate: float | None = None

    avg_visual_score: float | None = None
    min_visual_score: float | None = None
    avg_content_issue_count: float | None = None
    avg_repair_attempt_count: float | None = None

    tool_call_success_rate: float | None = None
    failed_tool_count: int = 0
    skipped_tool_count: int = 0
    timeout_tool_count: int = 0
    top_error_signatures: list[tuple[str, int]] = Field(default_factory=list)

    latency_ms_avg: float | None = None
    estimated_cost_total: float | None = None

    cases: list[BenchmarkCaseResult] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


def evaluate_case_from_artifacts(
    case: BenchmarkCase,
    run_dir: str | Path,
) -> BenchmarkCaseResult:
    run_path = Path(run_dir)
    run_id = case.run_id or run_path.name or case.case_id
    result = BenchmarkCaseResult(case_id=case.case_id, run_id=run_id)
    quality_path = run_path / "quality_report.json"
    trace_path = run_path / "trace_summary.json"
    quality_path_exists = quality_path.exists()
    trace_path_exists = trace_path.exists()
    quality = _load_json(quality_path, result.reasons, "quality_report.json")
    trace = _load_json(trace_path, result.reasons, "trace_summary.json")

    result.quality_report_exists = quality is not None
    result.trace_summary_exists = trace is not None

    if quality is not None:
        _apply_quality_report(result, quality)
        _evaluate_content_expectations(case, result, quality)
    elif case.expected.require_quality_report and not quality_path_exists:
        result.reasons.append("missing quality_report.json")

    if trace is not None:
        _apply_trace_summary(result, trace)
    elif case.expected.require_trace_summary and not trace_path_exists:
        result.reasons.append("missing trace_summary.json")

    result.tool_call_success_rate = _tool_success_rate(
        result.tool_attempt_count,
        result.failed_tool_count,
        result.skipped_tool_count,
        result.timeout_tool_count,
    )
    result.status = _classify_case(case, result)
    _ensure_non_pass_reason(result)
    return result


def aggregate_case_results(
    *,
    benchmark_id: str,
    suite_id: str,
    results: list[BenchmarkCaseResult],
) -> BenchmarkReport:
    total = len(results)
    if total == 0:
        return BenchmarkReport(benchmark_id=benchmark_id, suite_id=suite_id, status="empty", total_cases=0)

    error_counter: Counter[str] = Counter()
    for result in results:
        error_counter.update(result.error_signatures)

    attempts = sum(result.tool_attempt_count for result in results)
    degraded = sum(
        result.failed_tool_count + result.skipped_tool_count + result.timeout_tool_count
        for result in results
    )
    report = BenchmarkReport(
        benchmark_id=benchmark_id,
        suite_id=suite_id,
        total_cases=total,
        passed_cases=sum(1 for result in results if result.status == "pass"),
        failed_cases=sum(1 for result in results if result.status == "fail"),
        warning_cases=sum(1 for result in results if result.status == "warning"),
        skipped_cases=sum(1 for result in results if result.status == "skipped"),
        missing_artifact_cases=sum(1 for result in results if result.status == "missing_artifacts"),
        end_to_end_success_rate=_rate(sum(1 for result in results if result.status == "pass"), total),
        acceptable_success_rate=_rate(
            sum(1 for result in results if result.status in {"pass", "warning"}),
            total,
        ),
        pptx_exists_rate=_bool_rate(result.pptx_exists for result in results),
        preview_success_rate=_bool_rate(result.preview_success for result in results),
        quality_report_exists_rate=_bool_rate(result.quality_report_exists for result in results),
        trace_summary_exists_rate=_bool_rate(result.trace_summary_exists for result in results),
        avg_visual_score=_average(result.visual_score_avg for result in results),
        min_visual_score=_minimum(result.visual_score_min for result in results),
        avg_content_issue_count=_average(result.content_issue_count for result in results),
        avg_repair_attempt_count=_average(result.repair_attempt_count for result in results),
        tool_call_success_rate=None if attempts <= 0 else _round_rate((attempts - degraded) / attempts),
        failed_tool_count=sum(result.failed_tool_count for result in results),
        skipped_tool_count=sum(result.skipped_tool_count for result in results),
        timeout_tool_count=sum(result.timeout_tool_count for result in results),
        top_error_signatures=error_counter.most_common(10),
        cases=list(results),
    )
    report.status = _report_status(report)
    return report


def _apply_quality_report(result: BenchmarkCaseResult, quality: dict[str, Any]) -> None:
    run = _mapping(quality.get("run"))
    summary = _mapping(quality.get("summary"))
    artifacts = _mapping(quality.get("artifacts"))
    missing_reasons = _mapping(quality.get("missing_reasons"))

    result.pptx_exists = _optional_bool(run.get("pptx_exists"))
    result.preview_success = _optional_bool(run.get("preview_success"))
    result.slide_count = _optional_int(run.get("slide_count"))
    result.visual_score_avg = _optional_float(run.get("visual_score_avg"))
    result.visual_score_min = _optional_float(run.get("visual_score_min"))
    result.content_issue_count = _optional_int(run.get("content_issue_count"))
    result.repaired_slide_count = _optional_int(run.get("repaired_slide_count"))
    result.repair_attempt_count = _optional_int(run.get("repair_attempt_count"))
    result.metrics.update(
        {
            "quality_status": summary.get("status"),
            "issue_count": _optional_int(summary.get("issue_count")),
            "critical_issue_count": _optional_int(summary.get("critical_issue_count")),
            "artifacts": artifacts,
            "missing_reasons": missing_reasons,
        }
    )
    if result.slide_count is None:
        result.reasons.append("missing run.slide_count")
    if result.pptx_exists is None:
        result.reasons.append("missing run.pptx_exists")
    if result.preview_success is None:
        result.reasons.append("missing run.preview_success")
    if missing_reasons:
        result.reasons.append("quality report has missing_reasons")


def _apply_trace_summary(result: BenchmarkCaseResult, trace: dict[str, Any]) -> None:
    result.trace_status = _optional_str(trace.get("status"))
    result.tool_call_count = _optional_int(trace.get("tool_call_count")) or 0
    result.tool_attempt_count = _optional_int(trace.get("tool_attempt_count")) or result.tool_call_count
    result.failed_tool_count = _optional_int(trace.get("failed_tool_count")) or 0
    result.skipped_tool_count = _optional_int(trace.get("skipped_tool_count")) or 0
    result.timeout_tool_count = _optional_int(trace.get("timeout_tool_count")) or 0
    result.error_signatures = [str(item) for item in trace.get("error_signatures") or []]
    result.metrics.update(
        {
            "trace_artifact_refs": _mapping(trace.get("artifact_refs")),
            "quality_report_paths": list(trace.get("quality_report_paths") or []),
        }
    )
    if result.trace_status is None:
        result.reasons.append("missing trace status")


def _classify_case(case: BenchmarkCase, result: BenchmarkCaseResult) -> str:
    expected = case.expected
    if expected.require_quality_report and not result.quality_report_exists:
        return "missing_artifacts"
    if expected.require_trace_summary and not result.trace_summary_exists:
        return "missing_artifacts"

    missing_metric_reasons = _missing_required_metric_reasons(case, result)
    if missing_metric_reasons:
        result.reasons.extend(missing_metric_reasons)
        return "missing_artifacts"

    fail_reasons = _fail_reasons(case, result)
    if fail_reasons:
        result.reasons.extend(fail_reasons)
        return "fail"

    warning_reasons = _warning_reasons(case, result)
    if warning_reasons:
        result.reasons.extend(warning_reasons)
        return "warning"
    return "pass"


def _missing_required_metric_reasons(case: BenchmarkCase, result: BenchmarkCaseResult) -> list[str]:
    expected = case.expected
    reasons: list[str] = []
    if expected.require_pptx and result.pptx_exists is None:
        reasons.append("missing required metric run.pptx_exists")
    if expected.require_preview and result.preview_success is None:
        reasons.append("missing required metric run.preview_success")
    if (expected.min_slides is not None or expected.max_slides is not None) and result.slide_count is None:
        reasons.append("missing required metric run.slide_count")
    if expected.min_visual_score is not None and result.visual_score_min is None:
        reasons.append("missing required metric run.visual_score_min")
    if expected.max_content_issue_count is not None and result.content_issue_count is None:
        reasons.append("missing required metric run.content_issue_count")
    return reasons


def _fail_reasons(case: BenchmarkCase, result: BenchmarkCaseResult) -> list[str]:
    expected = case.expected
    reasons: list[str] = []
    if expected.require_pptx and result.pptx_exists is False:
        reasons.append("required pptx missing")
    if expected.require_preview and result.preview_success is False:
        reasons.append("required preview missing")
    if result.slide_count is not None and expected.min_slides is not None and result.slide_count < expected.min_slides:
        reasons.append(f"slide_count {result.slide_count} < min_slides {expected.min_slides}")
    if result.slide_count is not None and expected.max_slides is not None and result.slide_count > expected.max_slides:
        reasons.append(f"slide_count {result.slide_count} > max_slides {expected.max_slides}")
    if (
        result.visual_score_min is not None
        and expected.min_visual_score is not None
        and result.visual_score_min < expected.min_visual_score
    ):
        reasons.append(f"visual_score_min {result.visual_score_min} < min_visual_score {expected.min_visual_score}")
    if (
        result.content_issue_count is not None
        and expected.max_content_issue_count is not None
        and result.content_issue_count > expected.max_content_issue_count
    ):
        reasons.append(
            f"content_issue_count {result.content_issue_count} > max_content_issue_count "
            f"{expected.max_content_issue_count}"
        )
    if result.trace_status == "failed":
        reasons.append("trace_status failed")
    missing_sections = result.metrics.get("missing_required_sections")
    if isinstance(missing_sections, list):
        reasons.extend(f"missing required_section: {section}" for section in missing_sections)
    missing_keywords = result.metrics.get("missing_expected_keywords")
    if isinstance(missing_keywords, list):
        reasons.extend(f"missing expected_keyword: {keyword}" for keyword in missing_keywords)
    return reasons


def _warning_reasons(case: BenchmarkCase, result: BenchmarkCaseResult) -> list[str]:
    reasons: list[str] = []
    if result.preview_success is False and not case.expected.require_preview:
        reasons.append("preview_success false")
    if result.trace_status == "warning":
        reasons.append("trace_status warning")
    if result.failed_tool_count > 0:
        reasons.append(f"failed_tool_count {result.failed_tool_count} > 0")
    if result.timeout_tool_count > 0:
        reasons.append(f"timeout_tool_count {result.timeout_tool_count} > 0")
    if result.skipped_tool_count > 0:
        reasons.append(f"skipped_tool_count {result.skipped_tool_count} > 0")
    if result.visual_score_avg is not None and case.expected.min_visual_score is not None:
        if result.visual_score_avg < case.expected.min_visual_score + 0.25:
            reasons.append("visual_score_avg near minimum threshold")
    missing_reasons = result.metrics.get("missing_reasons")
    if isinstance(missing_reasons, dict) and missing_reasons:
        reasons.append("missing_reasons present")
    if case.expected.required_sections and result.metrics.get("content_expectations_evaluated") is False:
        reasons.append("required_sections not evaluated: no searchable text in quality_report")
    if case.expected.expected_keywords and result.metrics.get("content_expectations_evaluated") is False:
        reasons.append("expected_keywords not evaluated: no searchable text in quality_report")
    return reasons


def _evaluate_content_expectations(
    case: BenchmarkCase,
    result: BenchmarkCaseResult,
    quality: dict[str, Any] | None,
) -> None:
    required_sections = list(case.expected.required_sections)
    expected_keywords = list(case.expected.expected_keywords)
    if not required_sections and not expected_keywords:
        return

    searchable_text = _extract_searchable_quality_text(quality or {})
    if not searchable_text:
        result.metrics.update(
            {
                "required_section_coverage": None if required_sections else 1.0,
                "expected_keyword_coverage": None if expected_keywords else 1.0,
                "missing_required_sections": [],
                "missing_expected_keywords": [],
                "content_expectations_evaluated": False,
            }
        )
        return

    normalized_text = searchable_text.lower()
    missing_sections = [section for section in required_sections if section.lower() not in normalized_text]
    missing_keywords = [keyword for keyword in expected_keywords if keyword.lower() not in normalized_text]
    result.metrics.update(
        {
            "required_section_coverage": _coverage(required_sections, missing_sections),
            "expected_keyword_coverage": _coverage(expected_keywords, missing_keywords),
            "missing_required_sections": missing_sections,
            "missing_expected_keywords": missing_keywords,
            "content_expectations_evaluated": True,
        }
    )


def _extract_searchable_quality_text(quality: dict[str, Any]) -> str:
    chunks: list[str] = []
    run = _mapping(quality.get("run"))
    topic = run.get("topic")
    if isinstance(topic, str):
        chunks.append(topic)

    _collect_text(quality.get("summary"), chunks)
    for slide in quality.get("slides") or []:
        if not isinstance(slide, dict):
            continue
        for key in ("title", "text", "content"):
            value = slide.get(key)
            if isinstance(value, str):
                chunks.append(value)
    for issue in quality.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        message = issue.get("message")
        if isinstance(message, str):
            chunks.append(message)
        _collect_text(issue.get("evidence"), chunks)

    return "\n".join(chunk for chunk in chunks if chunk.strip())


def _collect_text(value: Any, chunks: list[str]) -> None:
    if isinstance(value, str):
        chunks.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_text(item, chunks)
    elif isinstance(value, list | tuple | set):
        for item in value:
            _collect_text(item, chunks)


def _load_json(path: Path, reasons: list[str], label: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        reasons.append(f"invalid {label}")
        return None
    if not isinstance(payload, dict):
        reasons.append(f"invalid {label}: expected object")
        return None
    return payload


def _tool_success_rate(attempts: int, failed: int, skipped: int, timeout: int) -> float | None:
    if attempts <= 0:
        return None
    successful = attempts - failed - skipped - timeout
    return _round_rate(successful / attempts)


def _report_status(report: BenchmarkReport) -> str:
    if report.total_cases == 0:
        return "empty"
    if report.failed_cases or report.missing_artifact_cases:
        return "fail"
    if report.warning_cases or report.skipped_cases:
        return "warning"
    return "pass"


def _bool_rate(values: Any) -> float | None:
    items = [item for item in values if item is not None]
    if not items:
        return None
    return _rate(sum(1 for item in items if item is True), len(items))


def _rate(count: int, total: int) -> float | None:
    if total <= 0:
        return None
    return _round_rate(count / total)


def _round_rate(value: float) -> float:
    return round(float(value), 4)


def _coverage(expected: list[str], missing: list[str]) -> float | None:
    if not expected:
        return None
    return _round_rate((len(expected) - len(missing)) / len(expected))


def _ensure_non_pass_reason(result: BenchmarkCaseResult) -> None:
    if result.status != "pass" and not result.reasons:
        result.reasons.append(f"status {result.status} without explicit reason")


def _average(values: Any) -> float | None:
    items = [float(item) for item in values if item is not None]
    if not items:
        return None
    return round(sum(items) / len(items), 4)


def _minimum(values: Any) -> float | None:
    items = [float(item) for item in values if item is not None]
    return min(items) if items else None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
