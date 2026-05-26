from __future__ import annotations

from typing import Literal


DEFAULT_LOW_VISUAL_SCORE = 3.5


def severity_for_score(score: float | None, *, low_score_threshold: float = DEFAULT_LOW_VISUAL_SCORE) -> Literal["info", "warning", "error"]:
    if score is None:
        return "info"
    if score < low_score_threshold - 0.75:
        return "error"
    if score < low_score_threshold:
        return "warning"
    return "info"


def quality_status(*, issue_severities: list[str], visual_score_min: float | None, low_score_threshold: float = DEFAULT_LOW_VISUAL_SCORE) -> str:
    if "critical" in issue_severities:
        return "critical"
    if "error" in issue_severities:
        return "error"
    if visual_score_min is not None and visual_score_min < low_score_threshold:
        return "warning"
    if "warning" in issue_severities:
        return "warning"
    return "pass"
