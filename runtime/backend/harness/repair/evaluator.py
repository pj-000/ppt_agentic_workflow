from __future__ import annotations

from typing import Any


def compute_quality_delta(
    *,
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    before_metrics = _metrics(before)
    after_metrics = _metrics(after)
    delta: dict[str, Any] = {}
    for key in ("visual_score_avg", "visual_score_min", "content_issue_count", "repair_attempt_count"):
        before_value = _number(before_metrics.get(key))
        after_value = _number(after_metrics.get(key))
        if before_value is None or after_value is None:
            continue
        delta[f"{key}_delta"] = round(after_value - before_value, 4)
    for key in ("preview_success", "pptx_exists"):
        if key in before_metrics and key in after_metrics:
            delta[f"{key}_changed"] = before_metrics.get(key) != after_metrics.get(key)
    return delta


def _metrics(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    run = value.get("run")
    if isinstance(run, dict):
        return run
    return value


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
