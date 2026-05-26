from __future__ import annotations

import re
from typing import Any

from backend.harness.agent_runtime.serialization import to_jsonable
from backend.harness.observability.redaction import redact_trace_payload

_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(?:api[_-]?key|apikey|token|access_token|secret|password|authorization|system_prompt|"
    r"hidden_reasoning|chain_of_thought|raw_model_response|openai_api_key|tavily_api_key|minmax_api_key|"
    r"bearer_token|access_key|private_key|prompt_bundle|system_message|developer_message)\s*[:=]\s*[^,\s]+"
)


def sanitize_memory_text(value: Any, *, limit: int = 1000) -> str:
    if value is None:
        return ""
    redacted = redact_trace_payload({"value": str(value)}).get("value", "")
    text = _SENSITIVE_ASSIGNMENT_PATTERN.sub("[REDACTED]", str(redacted))
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


def sanitize_memory_artifacts(value: Any) -> dict[str, str]:
    jsonable = to_jsonable(value)
    if not isinstance(jsonable, dict):
        return {}
    artifacts: dict[str, str] = {}
    for key, item in jsonable.items():
        safe_key = sanitize_memory_text(key, limit=100)
        artifacts[safe_key] = _sanitize_path_like_text(sanitize_memory_text(item, limit=500))
    return artifacts


def clamp_confidence(value: float) -> float:
    return round(min(max(float(value), 0.0), 1.0), 4)


def _sanitize_path_like_text(text: str) -> str:
    value = str(text or "")
    normalized = value.replace("\\", "/")
    run_match = re.search(r"(?:^|/)runs/([^/\s]+)/([^/\s]+)$", normalized)
    if run_match:
        return f"runs/{run_match.group(1)}/{run_match.group(2)}"
    if re.match(r"^(?:[A-Za-z]:)?/", normalized):
        return normalized.rstrip("/").rsplit("/", 1)[-1] or "[path]"
    windows_match = re.match(r"^[A-Za-z]:/", normalized)
    if windows_match:
        return normalized.rstrip("/").rsplit("/", 1)[-1] or "[path]"
    return value
