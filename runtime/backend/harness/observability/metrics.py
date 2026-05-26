from __future__ import annotations

from typing import Any

from backend.harness.observability.event import TraceEvent


def summarize_trace_events(events: list[TraceEvent]) -> dict[str, Any]:
    tool_finished = [event for event in events if event.event_type == "tool.finished"]
    error_signatures = sorted({event.error_signature for event in events if event.error_signature})
    artifact_refs: dict[str, str] = {}
    quality_report_paths: list[str] = []

    for event in events:
        artifact_refs.update(event.artifact_refs)
        if event.event_type == "quality.reported":
            for key in ("json_path", "markdown_path"):
                value = event.payload.get(key)
                if value:
                    quality_report_paths.append(str(value))
            for key, value in event.artifact_refs.items():
                if key.startswith("quality_report") and value:
                    quality_report_paths.append(str(value))

    status = _summarize_status(events, tool_finished)
    phases = {event.phase for event in events if event.phase}

    return {
        "run_id": events[0].run_id if events else "",
        "total_events": len(events),
        "status": status,
        "phase_count": len(phases),
        "tool_call_count": len(tool_finished),
        "failed_tool_count": sum(1 for event in tool_finished if event.status == "failed"),
        "skipped_tool_count": sum(1 for event in tool_finished if event.status == "skipped"),
        "timeout_tool_count": sum(1 for event in tool_finished if event.status == "timeout"),
        "error_signatures": error_signatures,
        "artifact_refs": artifact_refs,
        "quality_report_paths": sorted(set(quality_report_paths)),
    }


def _summarize_status(events: list[TraceEvent], tool_finished: list[TraceEvent]) -> str:
    has_finished = any(event.event_type == "run.finished" for event in events)
    has_success_finish = any(
        event.event_type == "run.finished" and event.status == "success" for event in events
    )
    has_failed_phase = any(event.event_type == "phase.failed" for event in events)
    has_failed_tool = any(event.status == "failed" for event in tool_finished)

    if has_failed_phase:
        return "failed"
    if has_failed_tool:
        return "warning"
    if has_success_finish:
        return "success"
    if not has_finished:
        return "incomplete"
    return "unknown"
