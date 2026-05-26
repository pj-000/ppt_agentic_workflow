from __future__ import annotations

import json
from pathlib import Path

from backend.harness.quality.models import QualityIssue, QualityReport


def write_quality_report(report: QualityReport, output_root: str | Path) -> dict[str, str]:
    run_dir = Path(output_root).resolve() / "runs" / _safe_run_id(report.run.run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    json_path = run_dir / "quality_report.json"
    markdown_path = run_dir / "quality_report.md"
    json_path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown_report(report), encoding="utf-8")
    return {
        "json_path": str(json_path),
        "markdown_path": str(markdown_path),
    }


def render_markdown_report(report: QualityReport) -> str:
    lines: list[str] = [
        f"# Quality Report: {report.run.run_id}",
        "",
        "## Run Summary",
        "",
        f"- Topic: {_display(report.run.topic)}",
        f"- PPTX exists: {report.run.pptx_exists}",
        f"- PPTX path: {_display(report.run.pptx_path)}",
        f"- Slide count: {_display(report.run.slide_count)}",
        f"- Preview images: {report.run.preview_image_count}",
        f"- Extracted text length: {_display(report.run.extracted_text_length)}",
        f"- Content issues: {report.run.content_issue_count}",
        f"- Tool errors: {report.run.tool_error_count}",
        f"- Repair attempts: {report.run.repair_attempt_count}",
        f"- Repaired slides: {report.run.repaired_slide_count}",
        f"- Created at: {report.run.created_at}",
        "",
        "## Overall Quality Status",
        "",
        f"- Status: {report.summary.get('status', 'unknown')}",
        f"- Visual score avg: {_display(report.run.visual_score_avg)}",
        f"- Visual score min: {_display(report.run.visual_score_min)}",
        f"- Low-quality slides: {_display(report.summary.get('low_quality_slide_indices', []))}",
        "",
        "## Slide-Level Table",
        "",
        "| Slide | Visual | Layout | Content | Design | Issues | Repaired | Attempts |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: |",
    ]

    if report.slides:
        for slide in report.slides:
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(slide.slide_index),
                        _display(slide.visual_score),
                        _display(slide.layout_score),
                        _display(slide.content_score),
                        _display(slide.design_score),
                        str(slide.issue_count),
                        "yes" if slide.repaired else "no",
                        str(slide.repair_attempts),
                    ]
                )
                + " |"
            )
    else:
        lines.append("| n/a | n/a | n/a | n/a | n/a | 0 | no | 0 |")

    lines.extend(
        [
            "",
            "## Top Quality Issues",
            "",
        ]
    )
    top_issues = _rank_issues(report.issues)[:10]
    if top_issues:
        for issue in top_issues:
            location = f"slide {issue.slide_index}" if issue.slide_index is not None else "run"
            suggestion = f" Suggested fix: {issue.suggested_fix}" if issue.suggested_fix else ""
            lines.append(f"- [{issue.severity}] {issue.source}/{issue.issue_type} at {location}: {issue.message}{suggestion}")
    else:
        lines.append("- No quality issues captured.")

    lines.extend(
        [
            "",
            "## Repair Summary",
            "",
            f"- Repaired slide count: {report.run.repaired_slide_count}",
            f"- Repair attempt count: {report.run.repair_attempt_count}",
        ]
    )
    repaired_slides = [slide for slide in report.slides if slide.repaired or slide.repair_attempts]
    if repaired_slides:
        for slide in repaired_slides:
            lines.append(
                f"- Slide {slide.slide_index}: attempts={slide.repair_attempts}, "
                f"before={_display(slide.before_repair_score)}, after={_display(slide.after_repair_score)}"
            )
    else:
        lines.append("- No repair events captured.")

    lines.extend(
        [
            "",
            "## Tool Errors",
            "",
        ]
    )
    tool_errors = [issue for issue in report.issues if issue.source == "tool"]
    if tool_errors:
        for issue in tool_errors:
            lines.append(f"- [{issue.severity}] {issue.issue_type}: {issue.message}")
    else:
        lines.append("- No tool errors captured.")

    lines.extend(
        [
            "",
            "## Suggested Next Debugging Steps",
            "",
        ]
    )
    lines.extend(_suggest_debugging_steps(report))
    lines.append("")
    return "\n".join(lines)


def _rank_issues(issues: list[QualityIssue]) -> list[QualityIssue]:
    severity_rank = {"critical": 0, "error": 1, "warning": 2, "info": 3}
    return sorted(issues, key=lambda issue: (severity_rank.get(issue.severity, 9), issue.slide_index is None, issue.slide_index or -1, issue.issue_id))


def _suggest_debugging_steps(report: QualityReport) -> list[str]:
    steps: list[str] = []
    if not report.run.pptx_exists:
        steps.append("- Inspect PPTX assembly output and confirm the reported output path exists.")
    if not report.run.preview_success:
        steps.append("- Inspect preview rendering diagnostics and slides_preview artifacts.")
    if report.run.tool_error_count:
        steps.append("- Review tool error evidence and the corresponding harness trace entries.")
    if report.summary.get("low_quality_slide_indices"):
        steps.append("- Re-run visual QA for low-quality slides and compare before/after repair scores.")
    if report.run.content_issue_count:
        steps.append("- Review content QA issues against the outline and generated PPTX text.")
    if not steps:
        steps.append("- No immediate action required from captured quality signals.")
    return steps


def _display(value: object) -> str:
    if value is None:
        return "n/a"
    return str(value)


def _safe_run_id(run_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in run_id)
    return safe or "run"
