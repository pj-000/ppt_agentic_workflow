from __future__ import annotations

import logging
from typing import Any

from backend.harness.observability.event import TraceEvent, TraceEventType, json_safe, new_event_id, utc_now_iso
from backend.harness.observability.redaction import redact_trace_payload
from backend.harness.observability.trace_store import TraceStore

logger = logging.getLogger(__name__)

_DIRECT_STAGE_MAP: dict[str, TraceEventType] = {
    "run.started": "run.started",
    "run.finished": "run.finished",
    "tool.started": "tool.started",
    "tool.finished": "tool.finished",
    "quality.reported": "quality.reported",
    "qa.completed": "qa.completed",
    "repair.started": "repair.started",
    "repair.finished": "repair.finished",
    "memory.queried": "memory.queried",
    "memory.hit": "memory.hit",
    "memory.written": "memory.written",
    "artifact.created": "artifact.created",
    "replan.triggered": "replan.triggered",
    "phase.started": "phase.started",
    "phase.finished": "phase.finished",
    "phase.failed": "phase.failed",
    "phase.skipped": "phase.skipped",
    "agent.started": "agent.started",
    "agent.finished": "agent.finished",
}

_PHASE_STATUS_EVENT_TYPE: dict[str, TraceEventType] = {
    "running": "phase.started",
    "started": "phase.started",
    "completed": "phase.finished",
    "success": "phase.finished",
    "failed": "phase.failed",
    "error": "phase.failed",
    "skipped": "phase.skipped",
}


class ObservabilityTraceAdapter:
    def __init__(
        self,
        run_id: str,
        trace_store: TraceStore,
        legacy_trace: Any | None = None,
    ):
        self.run_id = run_id
        self.trace_store = trace_store
        self.legacy_trace = legacy_trace

    def record(self, stage: str, payload: dict[str, Any]) -> None:
        original_payload = payload or {}
        safe_payload = redact_trace_payload(original_payload)
        self._record_event(stage, safe_payload)
        self._record_legacy(stage, original_payload)

    def _record_event(self, stage: str, payload: dict[str, Any]) -> None:
        try:
            event_type = _map_stage_to_event_type(stage, payload)
            event = TraceEvent(
                run_id=self.run_id,
                event_id=new_event_id(),
                parent_event_id=_optional_str(payload.get("parent_event_id")),
                event_type=event_type,
                timestamp=utc_now_iso(),
                phase=_extract_phase(stage, payload, event_type),
                agent_name=_optional_str(payload.get("agent_name") or payload.get("agent")),
                tool_name=_optional_str(payload.get("tool_name") or payload.get("tool")),
                status=_optional_str(payload.get("status")),
                payload=json_safe(payload),
                metrics=_extract_metrics(payload),
                artifact_refs=_extract_artifact_refs(payload),
                error_signature=_optional_str(payload.get("error_signature")),
            )
            self.trace_store.append(event)
        except Exception as exc:
            logger.warning("[Observability] Failed to record trace event; continuing: %s", exc)

    def _record_legacy(self, stage: str, payload: dict[str, Any]) -> None:
        if not self.legacy_trace:
            return
        record = getattr(self.legacy_trace, "record", None)
        if not callable(record):
            return
        try:
            record(stage=stage, payload=payload)
        except Exception as exc:
            logger.warning("[Observability] Legacy trace failed; continuing: %s", exc)


def _map_stage_to_event_type(stage: str, payload: dict[str, Any]) -> TraceEventType:
    if stage == "phase_state":
        return _PHASE_STATUS_EVENT_TYPE.get(str(payload.get("status") or "").lower(), "phase.finished")
    return _DIRECT_STAGE_MAP.get(stage, "phase.finished")


def _extract_phase(stage: str, payload: dict[str, Any], event_type: TraceEventType) -> str | None:
    phase = payload.get("phase")
    if phase:
        return str(phase)
    if event_type.startswith("phase."):
        return stage if stage not in {"phase_state", *set(_DIRECT_STAGE_MAP)} else None
    return None


def _extract_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    metrics = payload.get("metrics")
    if isinstance(metrics, dict):
        return json_safe(metrics)
    extracted: dict[str, Any] = {}
    for key in ("latency_ms", "attempt", "issue_count"):
        if key in payload:
            extracted[key] = payload[key]
    return json_safe(extracted)


def _extract_artifact_refs(payload: dict[str, Any]) -> dict[str, str]:
    refs = payload.get("artifact_refs")
    if not isinstance(refs, dict):
        return {}
    return {str(key): str(value) for key, value in refs.items()}


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
