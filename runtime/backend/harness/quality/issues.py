from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from backend.harness.quality.models import QualityIssue
from backend.harness.quality.thresholds import DEFAULT_LOW_VISUAL_SCORE, severity_for_score


def normalize_content_issues(content_issues: Iterable[Any] | None) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    for item_index, item in enumerate(content_issues or []):
        slide_index = _coerce_optional_int(_get(item, "slide_index"))
        raw_messages = _coerce_list(_get(item, "issues") or _get(item, "messages") or _get(item, "message"))
        suggestions = _coerce_list(_get(item, "suggestions") or _get(item, "suggested_fix"))
        if not raw_messages:
            raw_messages = ["Content QA reported an issue."]
        for issue_index, message in enumerate(raw_messages):
            issues.append(
                QualityIssue(
                    issue_id=f"content_qa:{item_index}:{issue_index}",
                    issue_type="content_issue",
                    severity="warning",
                    slide_index=slide_index,
                    message=str(message),
                    evidence=_safe_mapping(item),
                    suggested_fix=_string_at(suggestions, issue_index),
                    source="content_qa",
                )
            )
    return issues


def normalize_visual_eval_results(
    visual_eval_results: Iterable[Any] | None,
    *,
    low_score_threshold: float = DEFAULT_LOW_VISUAL_SCORE,
) -> tuple[list[QualityIssue], dict[int, dict[str, Any]]]:
    issues: list[QualityIssue] = []
    score_by_slide: dict[int, dict[str, Any]] = {}
    for fallback_index, item in enumerate(visual_eval_results or []):
        slide_index = _coerce_optional_int(_get(item, "slide_index"))
        if slide_index is None:
            slide_index = fallback_index
        overall = _coerce_optional_float(_get(item, "overall") or _get(item, "visual_score"))
        layout_score = _coerce_optional_float(_get(item, "layout_score"))
        content_score = _coerce_optional_float(_get(item, "content_score"))
        design_score = _coerce_optional_float(_get(item, "design_score"))
        score_by_slide[slide_index] = {
            "visual_score": overall,
            "layout_score": layout_score,
            "content_score": content_score,
            "design_score": design_score,
        }

        messages = _coerce_list(_get(item, "issues"))
        suggestions = _coerce_list(_get(item, "suggestions"))
        for issue_index, message in enumerate(messages):
            issues.append(
                QualityIssue(
                    issue_id=f"visual_qa:{slide_index}:{issue_index}",
                    issue_type="visual_issue",
                    severity=severity_for_score(overall, low_score_threshold=low_score_threshold),
                    slide_index=slide_index,
                    message=str(message),
                    evidence={
                        "overall": overall,
                        "layout_score": layout_score,
                        "content_score": content_score,
                        "design_score": design_score,
                    },
                    suggested_fix=_string_at(suggestions, issue_index),
                    source="visual_qa",
                )
            )

        if overall is not None and overall < low_score_threshold:
            issues.append(
                QualityIssue(
                    issue_id=f"visual_qa:{slide_index}:low_score",
                    issue_type="low_visual_score",
                    severity=severity_for_score(overall, low_score_threshold=low_score_threshold),
                    slide_index=slide_index,
                    message=f"Visual score {overall:.2f} is below threshold {low_score_threshold:.2f}.",
                    evidence={
                        "overall": overall,
                        "threshold": low_score_threshold,
                        "layout_score": layout_score,
                        "content_score": content_score,
                        "design_score": design_score,
                    },
                    suggested_fix=_string_at(suggestions, 0),
                    source="visual_qa",
                )
            )
    return issues, score_by_slide


def normalize_tool_errors(tool_errors: Iterable[Any] | None) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    for item_index, item in enumerate(tool_errors or []):
        message = _get(item, "message") or _get(item, "error") or str(item)
        issues.append(
            QualityIssue(
                issue_id=f"tool:{item_index}",
                issue_type=str(_get(item, "stage") or _get(item, "tool") or "tool_error"),
                severity="error",
                slide_index=_coerce_optional_int(_get(item, "slide_index")),
                message=str(message),
                evidence=_safe_mapping(item),
                suggested_fix="Inspect the related tool stage and retry with the captured artifact inputs.",
                source="tool",
            )
        )
    return issues


def normalize_repair_failures(repair_events: Iterable[Any] | None) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    for item_index, item in enumerate(repair_events or []):
        event = str(_get(item, "event") or _get(item, "type") or "")
        if event != "slide_revision_failed":
            continue
        issues.append(
            QualityIssue(
                issue_id=f"repair:{item_index}",
                issue_type="repair_failed",
                severity="warning",
                slide_index=_coerce_optional_int(_get(item, "slide_index")),
                message=str(_get(item, "detail") or "Slide repair failed and the previous version was kept."),
                evidence=_safe_mapping(item),
                suggested_fix="Review the repair feedback, generated slide code, and evaluator comments for this slide.",
                source="repair",
            )
        )
    return issues


def _get(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _coerce_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _string_at(values: list[Any], index: int) -> str | None:
    if not values:
        return None
    if index < len(values):
        return str(values[index])
    return str(values[0])


def _safe_mapping(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        data = dict(item)
    elif hasattr(item, "model_dump"):
        data = item.model_dump(mode="json")
    elif hasattr(item, "__dict__"):
        data = {key: value for key, value in vars(item).items() if not key.startswith("_")}
    else:
        data = {"value": str(item)}
    return _to_json_safe(data)


def _to_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _to_json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_to_json_safe(item) for item in value]
    if hasattr(value, "model_dump"):
        return _to_json_safe(value.model_dump(mode="json"))
    return str(value)
