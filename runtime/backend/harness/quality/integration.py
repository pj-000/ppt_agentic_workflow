from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from backend.harness.quality.collector import QualityCollector
from backend.harness.quality.report import write_quality_report

logger = logging.getLogger(__name__)


def write_quality_report_safely(
    *,
    output_root: str | Path,
    run_id: str,
    topic: str | None,
    pptx_path: str | None,
    preview_images: list[str] | None,
    extracted_text: str | None,
    visual_eval_results: list[Any] | None,
    content_issues: list[Any] | None,
    repair_events: list[Any] | None,
    tool_errors: list[Any] | None = None,
    stage_latency_ms: dict[str, int] | None = None,
    artifacts: dict[str, str] | None = None,
    missing_reasons: dict[str, str] | None = None,
    harness_trace: Any | None = None,
    trace: Any | None = None,
) -> dict[str, str]:
    resolved_missing_reasons = dict(missing_reasons or {})
    if tool_errors is None:
        resolved_missing_reasons.setdefault("tool_errors", "ToolRuntime not implemented yet")
    if stage_latency_ms is None:
        resolved_missing_reasons.setdefault("stage_latency_ms", "not available from current HarnessRunState")

    try:
        report = QualityCollector().collect(
            run_id=run_id,
            topic=topic,
            pptx_path=pptx_path,
            preview_images=preview_images,
            extracted_text=extracted_text,
            visual_eval_results=visual_eval_results,
            content_issues=content_issues,
            repair_events=repair_events,
            tool_errors=tool_errors,
            stage_latency_ms=stage_latency_ms,
            artifacts=artifacts,
            missing_reasons=resolved_missing_reasons,
        )
        paths = write_quality_report(report, output_root)
        trace_payload = {
            "status": report.summary.get("status"),
            "json_path": paths.get("json_path", ""),
            "markdown_path": paths.get("markdown_path", ""),
            "issue_count": report.summary.get("issue_count", 0),
            "missing_metric_keys": report.summary.get("missing_metric_keys", []),
            "artifact_refs": {
                "quality_report_json": paths.get("json_path", ""),
                "quality_report_md": paths.get("markdown_path", ""),
            },
        }
        _record_quality_trace(
            harness_trace,
            {
                "status": trace_payload["status"],
                "json_path": trace_payload["json_path"],
                "markdown_path": trace_payload["markdown_path"],
                "issue_count": trace_payload["issue_count"],
                "missing_metric_keys": trace_payload["missing_metric_keys"],
            },
        )
        _record_trace(trace, "quality.reported", trace_payload)
        return paths
    except Exception as exc:
        logger.warning("[QualityHarness] Failed to write quality report; continuing generation: %s", exc)
        _record_quality_trace(harness_trace, {"status": "failed", "error": str(exc)[:300]})
        _record_trace(trace, "quality.reported", {"status": "failed", "error": str(exc)[:300]})
        return {}


def _record_quality_trace(harness_trace: Any | None, payload: dict[str, Any]) -> None:
    if not harness_trace:
        return
    record = getattr(harness_trace, "record", None)
    if not callable(record):
        return
    try:
        record(stage="quality_report", payload=payload)
    except Exception as exc:
        logger.warning("[QualityHarness] Failed to record legacy quality trace; continuing: %s", exc)


def _record_trace(trace: Any | None, stage: str, payload: dict[str, Any]) -> None:
    if not trace:
        return
    record = getattr(trace, "record", None)
    if not callable(record):
        return
    try:
        record(stage=stage, payload=payload)
    except Exception as exc:
        logger.warning("[QualityHarness] Failed to record observability event; continuing: %s", exc)
