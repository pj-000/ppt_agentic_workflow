from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.harness.benchmark.metrics import BenchmarkCaseResult, BenchmarkReport


def write_benchmark_report(
    report: BenchmarkReport,
    output_dir: str | Path,
) -> dict[str, str]:
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    json_path = target_dir / "benchmark_report.json"
    markdown_path = target_dir / "benchmark_report.md"
    case_results_path = target_dir / "case_results.jsonl"

    json_path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_benchmark_markdown(report), encoding="utf-8")
    case_results_path.write_text(
        "".join(case.model_dump_json() + "\n" for case in report.cases),
        encoding="utf-8",
    )
    return {
        "json_path": str(json_path),
        "markdown_path": str(markdown_path),
        "case_results_path": str(case_results_path),
    }


def render_benchmark_markdown(report: BenchmarkReport) -> str:
    lines = [
        "# Benchmark Report",
        "",
        f"- Benchmark ID: {report.benchmark_id}",
        f"- Suite ID: {report.suite_id}",
        f"- Status: {report.status}",
        f"- Total Cases: {report.total_cases}",
        f"- Passed Cases: {report.passed_cases}",
        f"- Failed Cases: {report.failed_cases}",
        f"- Warning Cases: {report.warning_cases}",
        f"- Missing Artifact Cases: {report.missing_artifact_cases}",
        "",
        "## Core Rates",
        "",
        f"- End-to-end Success Rate: {_display_rate(report.end_to_end_success_rate)}",
        f"- PPTX Exists Rate: {_display_rate(report.pptx_exists_rate)}",
        f"- Preview Success Rate: {_display_rate(report.preview_success_rate)}",
        f"- Quality Report Exists Rate: {_display_rate(report.quality_report_exists_rate)}",
        f"- Trace Summary Exists Rate: {_display_rate(report.trace_summary_exists_rate)}",
        "",
        "## Quality Metrics",
        "",
        f"- Average Visual Score: {_display(report.avg_visual_score)}",
        f"- Minimum Visual Score: {_display(report.min_visual_score)}",
        f"- Average Content Issue Count: {_display(report.avg_content_issue_count)}",
        f"- Average Repair Attempt Count: {_display(report.avg_repair_attempt_count)}",
        "",
        "## Tool Metrics",
        "",
        f"- Tool Call Success Rate: {_display_rate(report.tool_call_success_rate)}",
        f"- Failed Tool Count: {report.failed_tool_count}",
        f"- Skipped Tool Count: {report.skipped_tool_count}",
        f"- Timeout Tool Count: {report.timeout_tool_count}",
        "- Top Error Signatures:",
    ]
    if report.top_error_signatures:
        lines.extend(f"  - {signature}: {count}" for signature, count in report.top_error_signatures)
    else:
        lines.append("  - None")

    lines.extend(
        [
            "",
            "## Case Results",
            "",
            "| case_id | status | run_id | slide_count | visual_score_avg | visual_score_min | "
            "content_issue_count | tool_call_success_rate | reasons |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    if report.cases:
        lines.extend(_case_row(case) for case in report.cases)
    else:
        lines.append("| n/a | empty | n/a | n/a | n/a | n/a | n/a | n/a | no cases |")
    lines.append("")
    return "\n".join(lines)


def _case_row(case: BenchmarkCaseResult) -> str:
    reasons = "; ".join(case.reasons) if case.reasons else ""
    return (
        f"| {case.case_id} | {case.status} | {_display(case.run_id)} | {_display(case.slide_count)} | "
        f"{_display(case.visual_score_avg)} | {_display(case.visual_score_min)} | "
        f"{_display(case.content_issue_count)} | {_display_rate(case.tool_call_success_rate)} | "
        f"{reasons} |"
    )


def _display(value: Any) -> str:
    return "n/a" if value is None else str(value)


def _display_rate(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.4f}"
