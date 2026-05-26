from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.harness.quality.issues import (
    normalize_content_issues,
    normalize_repair_failures,
    normalize_tool_errors,
    normalize_visual_eval_results,
)
from backend.harness.quality.models import QualityIssue, QualityReport, RunQualityMetrics, SlideQualityMetrics
from backend.harness.quality.thresholds import DEFAULT_LOW_VISUAL_SCORE, quality_status


class QualityCollector:
    def __init__(self, *, low_score_threshold: float = DEFAULT_LOW_VISUAL_SCORE) -> None:
        self.low_score_threshold = low_score_threshold

    def collect(
        self,
        *,
        run_id: str,
        topic: str | None,
        pptx_path: str | None,
        preview_images: list[str] | None,
        extracted_text: str | None,
        visual_eval_results: list[Any] | None,
        content_issues: list[Any] | None,
        repair_events: list[Any] | None,
        tool_errors: list[Any] | None,
        stage_latency_ms: dict[str, int] | None,
    ) -> QualityReport:
        normalized_preview_images = list(preview_images or [])
        normalized_stage_latency = _coerce_latency_map(stage_latency_ms)
        pptx_exists = bool(pptx_path and Path(pptx_path).exists())

        issues: list[QualityIssue] = []
        if not pptx_path:
            issues.append(
                QualityIssue(
                    issue_id="pptx_parse:missing_path",
                    issue_type="missing_pptx_path",
                    severity="critical",
                    slide_index=None,
                    message="PPTX path was not provided.",
                    evidence={},
                    suggested_fix="Check whether PPTX assembly returned an output path.",
                    source="pptx_parse",
                )
            )
        elif not pptx_exists:
            issues.append(
                QualityIssue(
                    issue_id="pptx_parse:missing_file",
                    issue_type="missing_pptx_file",
                    severity="critical",
                    slide_index=None,
                    message="PPTX file does not exist at the reported path.",
                    evidence={"pptx_path": pptx_path},
                    suggested_fix="Check the assembly stage and output directory permissions.",
                    source="pptx_parse",
                )
            )

        if pptx_exists and not normalized_preview_images:
            issues.append(
                QualityIssue(
                    issue_id="preview:no_images",
                    issue_type="preview_missing",
                    severity="warning",
                    slide_index=None,
                    message="No preview images were available for the generated PPTX.",
                    evidence={"pptx_path": pptx_path},
                    suggested_fix="Inspect the preview rendering runtime and slides_preview output directory.",
                    source="preview",
                )
            )

        content_quality_issues = normalize_content_issues(content_issues)
        visual_quality_issues, score_by_slide = normalize_visual_eval_results(
            visual_eval_results,
            low_score_threshold=self.low_score_threshold,
        )
        tool_quality_issues = normalize_tool_errors(tool_errors)
        repair_quality_issues = normalize_repair_failures(repair_events)
        issues.extend(content_quality_issues)
        issues.extend(visual_quality_issues)
        issues.extend(tool_quality_issues)
        issues.extend(repair_quality_issues)

        repair_summary = _summarize_repair_events(repair_events)
        slide_count = _infer_slide_count(
            preview_images=normalized_preview_images,
            score_by_slide=score_by_slide,
            content_issues=content_issues,
            repair_summary=repair_summary,
        )
        issues_by_slide: dict[int, list[QualityIssue]] = defaultdict(list)
        for issue in issues:
            if issue.slide_index is not None:
                issues_by_slide[issue.slide_index].append(issue)

        slides: list[SlideQualityMetrics] = []
        if slide_count is not None:
            slide_indices = list(range(slide_count))
        else:
            slide_indices = sorted(set(issues_by_slide) | set(score_by_slide) | set(repair_summary["attempts_by_slide"]))

        for slide_index in slide_indices:
            scores = score_by_slide.get(slide_index, {})
            slide_issues = issues_by_slide.get(slide_index, [])
            before_score = repair_summary["before_score_by_slide"].get(slide_index)
            visual_score = scores.get("visual_score")
            slides.append(
                SlideQualityMetrics(
                    slide_index=slide_index,
                    title=None,
                    text_length=None,
                    has_image=None,
                    has_chart_or_diagram=None,
                    visual_score=visual_score,
                    layout_score=scores.get("layout_score"),
                    content_score=scores.get("content_score"),
                    design_score=scores.get("design_score"),
                    issue_count=len(slide_issues),
                    issues=slide_issues,
                    repaired=slide_index in repair_summary["repaired_slides"],
                    repair_attempts=repair_summary["attempts_by_slide"].get(slide_index, 0),
                    before_repair_score=before_score,
                    after_repair_score=visual_score if slide_index in repair_summary["repaired_slides"] else None,
                )
            )

        visual_scores = [item["visual_score"] for item in score_by_slide.values() if item.get("visual_score") is not None]
        visual_score_avg = round(sum(visual_scores) / len(visual_scores), 3) if visual_scores else None
        visual_score_min = min(visual_scores) if visual_scores else None
        issue_severities = [issue.severity for issue in issues]
        status = quality_status(
            issue_severities=issue_severities,
            visual_score_min=visual_score_min,
            low_score_threshold=self.low_score_threshold,
        )
        total_latency_ms = sum(normalized_stage_latency.values()) if normalized_stage_latency else None

        run = RunQualityMetrics(
            run_id=run_id,
            topic=topic,
            slide_count=slide_count,
            pptx_exists=pptx_exists,
            pptx_path=str(pptx_path) if pptx_path else None,
            preview_success=bool(normalized_preview_images),
            preview_image_count=len(normalized_preview_images),
            extracted_text_length=len(extracted_text) if extracted_text is not None else None,
            content_issue_count=len(content_quality_issues),
            visual_score_avg=visual_score_avg,
            visual_score_min=visual_score_min,
            repaired_slide_count=len(repair_summary["repaired_slides"]),
            repair_attempt_count=sum(repair_summary["attempts_by_slide"].values()),
            tool_error_count=len(tool_quality_issues),
            total_latency_ms=total_latency_ms,
            stage_latency_ms=normalized_stage_latency,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        low_quality_slide_indices = [
            slide.slide_index
            for slide in slides
            if slide.visual_score is not None and slide.visual_score < self.low_score_threshold
        ]
        summary = {
            "status": status,
            "low_score_threshold": self.low_score_threshold,
            "issue_count": len(issues),
            "critical_issue_count": sum(1 for issue in issues if issue.severity == "critical"),
            "error_issue_count": sum(1 for issue in issues if issue.severity == "error"),
            "warning_issue_count": sum(1 for issue in issues if issue.severity == "warning"),
            "low_quality_slide_indices": low_quality_slide_indices,
            "report_version": "quality_harness_v1",
        }

        return QualityReport(run=run, slides=slides, issues=issues, summary=summary)


def _infer_slide_count(
    *,
    preview_images: list[str],
    score_by_slide: dict[int, dict[str, Any]],
    content_issues: list[Any] | None,
    repair_summary: dict[str, Any],
) -> int | None:
    candidates: list[int] = []
    if preview_images:
        candidates.append(len(preview_images))
    for slide_index in score_by_slide:
        candidates.append(slide_index + 1)
    for item in content_issues or []:
        slide_index = _optional_int(_get(item, "slide_index"))
        if slide_index is not None:
            candidates.append(slide_index + 1)
    for slide_index in repair_summary["attempts_by_slide"]:
        candidates.append(slide_index + 1)
    for slide_index in repair_summary["repaired_slides"]:
        candidates.append(slide_index + 1)
    return max(candidates) if candidates else None


def _summarize_repair_events(repair_events: list[Any] | None) -> dict[str, Any]:
    attempts_by_slide: dict[int, int] = defaultdict(int)
    repaired_slides: set[int] = set()
    before_score_by_slide: dict[int, float] = {}
    for item in repair_events or []:
        event = str(_get(item, "event") or _get(item, "type") or "")
        if event == "slide_revision_start":
            slide_index = _optional_int(_get(item, "slide_index"))
            if slide_index is None:
                continue
            attempts_by_slide[slide_index] += 1
            before_score = _optional_float(_get(item, "overall") or _get(item, "before_score"))
            if before_score is not None and slide_index not in before_score_by_slide:
                before_score_by_slide[slide_index] = before_score
        elif event == "slide_revision_done":
            slide_indices = _coerce_slide_indices(_get(item, "slide_indices") or _get(item, "slide_index"))
            repaired_slides.update(slide_indices)
    return {
        "attempts_by_slide": dict(attempts_by_slide),
        "repaired_slides": repaired_slides,
        "before_score_by_slide": before_score_by_slide,
    }


def _coerce_latency_map(value: dict[str, int] | None) -> dict[str, int]:
    result: dict[str, int] = {}
    for key, raw in (value or {}).items():
        try:
            result[str(key)] = int(raw)
        except (TypeError, ValueError):
            continue
    return result


def _coerce_slide_indices(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, list | tuple | set):
        values = value
    else:
        values = [value]
    result: list[int] = []
    for item in values:
        slide_index = _optional_int(item)
        if slide_index is not None:
            result.append(slide_index)
    return result


def _get(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
