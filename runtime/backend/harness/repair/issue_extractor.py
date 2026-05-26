from __future__ import annotations

from typing import Any

from backend.harness.repair.models import (
    RepairIssue,
    RepairScope,
    RepairSeverity,
    RepairSource,
    stable_repair_id,
)
from backend.harness.repair.policies import RepairPolicy
from backend.harness.repair.safety import sanitize_repair_artifacts, sanitize_repair_mapping, sanitize_repair_text


def extract_repair_issues_from_quality_report(
    *,
    run_id: str,
    quality_report: dict[str, Any],
    policy: RepairPolicy,
) -> list[RepairIssue]:
    quality = quality_report if isinstance(quality_report, dict) else {}
    run = _dict_value(quality, "run")
    issues: list[RepairIssue] = []

    visual_score_min = _number(run.get("visual_score_min"))
    if visual_score_min is not None and visual_score_min < policy.min_visual_score:
        issues.append(
            _issue(
                run_id=run_id,
                source=RepairSource.VISUAL_QA,
                scope=RepairScope.VISUAL,
                severity=RepairSeverity.ERROR if visual_score_min < policy.min_visual_score - 1 else RepairSeverity.WARNING,
                trigger_stage="visual_qa",
                issue_type="visual_score_below_threshold",
                slide_index=_lowest_visual_slide_index(quality, policy),
                error_signature="visual.low_score",
                message=f"Minimum visual score {visual_score_min} is below threshold {policy.min_visual_score}.",
                metrics={"visual_score_min": visual_score_min, "threshold": policy.min_visual_score},
                evidence={"run": run},
            )
        )

    content_issue_count = _int(run.get("content_issue_count"))
    if content_issue_count is not None and content_issue_count > policy.max_content_issue_count:
        issues.append(
            _issue(
                run_id=run_id,
                source=RepairSource.CONTENT_QA,
                scope=RepairScope.CONTENT,
                severity=RepairSeverity.WARNING,
                trigger_stage="content_qa",
                issue_type="content_issue_count_exceeded",
                error_signature="content.issue_count_exceeded",
                message=f"Content issue count {content_issue_count} exceeds {policy.max_content_issue_count}.",
                metrics={"content_issue_count": content_issue_count, "threshold": policy.max_content_issue_count},
                evidence={"run": run},
            )
        )

    if run.get("preview_success") is False:
        issues.append(
            _issue(
                run_id=run_id,
                source=RepairSource.QUALITY,
                scope=RepairScope.TOOL,
                severity=RepairSeverity.ERROR,
                trigger_stage="visual_qa",
                issue_type="preview_failed",
                tool_name="ppt.render_preview",
                error_signature="ppt.render_preview:PreviewGenerationFailed",
                message="PPT preview rendering failed.",
                evidence={"run": run},
                retryable=True,
            )
        )

    if run.get("pptx_exists") is False:
        issues.append(
            _issue(
                run_id=run_id,
                source=RepairSource.QUALITY,
                scope=RepairScope.TOOL,
                severity=RepairSeverity.CRITICAL,
                trigger_stage="slide_generation",
                issue_type="pptx_missing",
                tool_name="ppt.run_pptxgenjs",
                error_signature="ppt.run_pptxgenjs:PptxArtifactMissing",
                message="Generated PPTX artifact is missing.",
                evidence={"run": run},
                retryable=True,
            )
        )

    raw_issues = quality.get("issues", [])
    if isinstance(raw_issues, list):
        for index, item in enumerate(raw_issues):
            if not isinstance(item, dict):
                continue
            issue_type = str(item.get("issue_type") or item.get("type") or item.get("code") or "quality_issue")
            scope = _scope_from_text(item.get("scope") or issue_type)
            source = RepairSource.CONTENT_QA if scope == RepairScope.CONTENT else RepairSource.QUALITY
            issues.append(
                _issue(
                    run_id=run_id,
                    source=source,
                    scope=scope,
                    severity=_severity_from_text(item.get("severity")),
                    trigger_stage=str(item.get("trigger_stage") or item.get("stage") or ""),
                    issue_type=issue_type,
                    slide_index=_int(item.get("slide_index")),
                    tool_name=_optional_text(item.get("tool") or item.get("tool_name")),
                    error_signature=_optional_text(item.get("error_signature")),
                    message=str(item.get("message") or item.get("description") or issue_type),
                    evidence=item,
                    metrics=_dict_value(item, "metrics"),
                    artifact_refs=_dict_value(item, "artifact_refs"),
                    retryable=bool(item.get("retryable", False)),
                    salt=f"quality_issue_{index}",
                )
            )

    missing_reasons = _dict_value(quality, "missing_reasons")
    for key, reason in missing_reasons.items():
        issues.append(
            _issue(
                run_id=run_id,
                source=RepairSource.QUALITY,
                scope=RepairScope.UNKNOWN,
                severity=RepairSeverity.INFO,
                trigger_stage="quality_report",
                issue_type="missing_metric",
                error_signature=f"quality.missing_metric:{key}",
                message=f"Quality metric missing: {key}",
                evidence={"metric": key, "reason": reason},
            )
        )

    return _dedupe_issues(issues)


def extract_repair_issues_from_trace_summary(
    *,
    run_id: str,
    trace_summary: dict[str, Any],
    policy: RepairPolicy,
) -> list[RepairIssue]:
    del policy
    trace = trace_summary if isinstance(trace_summary, dict) else {}
    issues: list[RepairIssue] = []
    if _int(trace.get("failed_tool_count")):
        issues.append(_trace_count_issue(run_id, trace, "tool_failed", "failed_tool_count", RepairSeverity.ERROR))
    if _int(trace.get("timeout_tool_count")):
        issues.append(_trace_count_issue(run_id, trace, "tool_timeout", "timeout_tool_count", RepairSeverity.ERROR))
    if _int(trace.get("skipped_tool_count")):
        issues.append(_trace_count_issue(run_id, trace, "tool_skipped", "skipped_tool_count", RepairSeverity.WARNING))

    error_signatures = trace.get("error_signatures", [])
    if isinstance(error_signatures, list):
        for signature in error_signatures:
            error_signature = sanitize_repair_text(signature, limit=240)
            issues.append(
                _issue(
                    run_id=run_id,
                    source=RepairSource.TRACE,
                    scope=_scope_from_error_signature(error_signature),
                    severity=RepairSeverity.WARNING,
                    trigger_stage="trace",
                    issue_type="trace_error_signature",
                    tool_name=_tool_from_signature(error_signature),
                    error_signature=error_signature,
                    message=f"Trace reported error signature: {error_signature}",
                    evidence={"trace_status": trace.get("status"), "error_signature": error_signature},
                    artifact_refs=_dict_value(trace, "artifact_refs"),
                    retryable=_looks_retryable(error_signature),
                )
            )
    return _dedupe_issues(issues)


def extract_repair_issues_from_tool_error(
    *,
    run_id: str,
    tool_error: dict[str, Any],
) -> RepairIssue:
    error = tool_error if isinstance(tool_error, dict) else {}
    status = str(error.get("status") or "failed")
    tool_name = _optional_text(error.get("tool") or error.get("tool_name"))
    error_type = str(error.get("error_type") or status)
    error_signature = _optional_text(error.get("error_signature")) or f"{tool_name or 'tool'}:{error_type}"
    return _issue(
        run_id=run_id,
        source=RepairSource.TOOL,
        scope=RepairScope.TOOL,
        severity=RepairSeverity.ERROR if status in {"failed", "timeout"} else RepairSeverity.WARNING,
        trigger_stage=str(error.get("stage") or "tool_runtime"),
        issue_type=f"tool_{status}",
        tool_name=tool_name,
        error_signature=error_signature,
        message=str(error.get("message") or error_type),
        evidence=error,
        metrics={"latency_ms": error.get("latency_ms")},
        retryable=status in {"failed", "timeout"},
    )


def _trace_count_issue(
    run_id: str,
    trace: dict[str, Any],
    issue_type: str,
    count_key: str,
    severity: RepairSeverity,
) -> RepairIssue:
    return _issue(
        run_id=run_id,
        source=RepairSource.TRACE,
        scope=RepairScope.TOOL,
        severity=severity,
        trigger_stage="trace",
        issue_type=issue_type,
        error_signature=f"trace.{issue_type}",
        message=f"Trace summary reported {trace.get(count_key, 0)} {issue_type.replace('_', ' ')} events.",
        evidence={"status": trace.get("status"), count_key: trace.get(count_key, 0)},
        artifact_refs=_dict_value(trace, "artifact_refs"),
        retryable=issue_type in {"tool_failed", "tool_timeout"},
    )


def _issue(
    *,
    run_id: str,
    source: RepairSource,
    scope: RepairScope,
    severity: RepairSeverity,
    trigger_stage: str,
    issue_type: str,
    message: str,
    error_signature: str | None = None,
    slide_index: int | None = None,
    tool_name: str | None = None,
    evidence: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    artifact_refs: dict[str, str] | None = None,
    retryable: bool = False,
    salt: str = "",
) -> RepairIssue:
    issue_id = stable_repair_id(
        "issue",
        run_id,
        source.value,
        scope.value,
        trigger_stage,
        issue_type,
        slide_index,
        tool_name,
        error_signature,
        salt,
    )
    return RepairIssue(
        issue_id=issue_id,
        run_id=run_id,
        source=source,
        scope=scope,
        severity=severity,
        trigger_stage=trigger_stage,
        issue_type=issue_type,
        slide_index=slide_index,
        tool_name=tool_name,
        error_signature=error_signature,
        message=message,
        evidence=sanitize_repair_mapping(evidence or {}),
        metrics=sanitize_repair_mapping(metrics or {}),
        artifact_refs=sanitize_repair_artifacts(artifact_refs or {}),
        retryable=retryable,
    )


def _dedupe_issues(issues: list[RepairIssue]) -> list[RepairIssue]:
    seen: set[tuple[Any, ...]] = set()
    deduped: list[RepairIssue] = []
    for issue in issues:
        key = (
            issue.source,
            issue.scope,
            issue.issue_type,
            issue.slide_index,
            issue.tool_name,
            issue.error_signature,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue)
    return deduped


def _lowest_visual_slide_index(quality: dict[str, Any], policy: RepairPolicy) -> int | None:
    slides = quality.get("slides", [])
    if not isinstance(slides, list):
        return None
    best_index: int | None = None
    best_score: float | None = None
    for slide in slides:
        if not isinstance(slide, dict):
            continue
        score = _number(slide.get("visual_score") or slide.get("score"))
        if score is None or score >= policy.min_visual_score:
            continue
        if best_score is None or score < best_score:
            best_score = score
            best_index = _int(slide.get("slide_index"))
    return best_index


def _scope_from_text(value: Any) -> RepairScope:
    text = str(value or "").lower()
    if "visual" in text or "layout" in text:
        return RepairScope.VISUAL
    if "content" in text or "text" in text or "coherence" in text:
        return RepairScope.CONTENT
    if "asset" in text or "image" in text:
        return RepairScope.ASSET
    if "tool" in text or "ppt" in text or "preview" in text:
        return RepairScope.TOOL
    if "slide" in text:
        return RepairScope.SLIDE
    if "deck" in text:
        return RepairScope.DECK
    return RepairScope.UNKNOWN


def _scope_from_error_signature(error_signature: str) -> RepairScope:
    text = error_signature.lower()
    if "render_preview" in text or "pptxgenjs" in text or "tool" in text:
        return RepairScope.TOOL
    if "image" in text or "asset" in text:
        return RepairScope.ASSET
    if "content" in text:
        return RepairScope.CONTENT
    if "visual" in text or "layout" in text:
        return RepairScope.VISUAL
    return RepairScope.UNKNOWN


def _severity_from_text(value: Any) -> RepairSeverity:
    text = str(value or "").lower()
    if text in {item.value for item in RepairSeverity}:
        return RepairSeverity(text)
    if "critical" in text:
        return RepairSeverity.CRITICAL
    if "error" in text or "fail" in text:
        return RepairSeverity.ERROR
    if "info" in text:
        return RepairSeverity.INFO
    return RepairSeverity.WARNING


def _tool_from_signature(error_signature: str) -> str | None:
    if ":" not in error_signature:
        return None
    first = error_signature.split(":", 1)[0]
    return first if "." in first else None


def _looks_retryable(error_signature: str) -> bool:
    lowered = error_signature.lower()
    return any(marker in lowered for marker in ("timeout", "provider", "unavailable", "generationfailed", "failed"))


def _dict_value(value: dict[str, Any], key: str) -> dict[str, Any]:
    item = value.get(key, {}) if isinstance(value, dict) else {}
    return item if isinstance(item, dict) else {}


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = sanitize_repair_text(value, limit=240)
    return text or None
