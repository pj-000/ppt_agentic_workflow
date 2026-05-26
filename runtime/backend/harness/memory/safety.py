from __future__ import annotations

from typing import Any

from backend.harness.agent_runtime.serialization import to_jsonable
from backend.harness.observability.redaction import redact_trace_payload


def sanitize_memory_text(value: Any, *, limit: int = 1000) -> str:
    if value is None:
        return ""
    redacted = redact_trace_payload({"value": str(value)}).get("value", "")
    text = str(redacted)
    if len(text) > limit:
        return text[:limit].rstrip() + "... [TRUNCATED]"
    return text


def sanitize_memory_mapping(value: Any) -> dict[str, Any]:
    jsonable = to_jsonable(value)
    if not isinstance(jsonable, dict):
        return {}
    return redact_trace_payload(jsonable)


def sanitize_memory_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    return [sanitize_memory_text(item, limit=200) for item in value]


def clamp_confidence(value: float) -> float:
    return round(min(max(float(value), 0.0), 1.0), 4)
