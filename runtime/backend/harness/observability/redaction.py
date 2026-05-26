from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any


SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "token",
    "access_token",
    "secret",
    "password",
    "authorization",
    "system_prompt",
    "hidden_reasoning",
    "chain_of_thought",
    "raw_model_response",
}

_SECRET_VALUE_PATTERN = re.compile(r"\b(?:sk|gho|ghp|xoxb|AKIA|AIza)[A-Za-z0-9_\-]{8,}\b")
_PATH_PATTERN = re.compile(r"(?<![\w.-])(?:[A-Za-z]:)?(?:/[^\s:]+)+")
_MAX_STRING_LENGTH = 500


def redact_trace_payload(payload: dict[str, Any]) -> dict[str, Any]:
    redacted = _redact_value(payload)
    return redacted if isinstance(redacted, dict) else {}


def _redact_value(value: Any, *, key: str | None = None) -> Any:
    if key and _normalize_key(key) in SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _redact_value(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple | set):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    if value is None or isinstance(value, int | float | bool):
        return value
    return _redact_string(str(value))


def _redact_string(value: str) -> str:
    cleaned = _SECRET_VALUE_PATTERN.sub("[REDACTED]", value)
    cleaned = _PATH_PATTERN.sub(lambda match: _safe_path(match.group(0)), cleaned)
    if len(cleaned) > _MAX_STRING_LENGTH:
        return cleaned[:_MAX_STRING_LENGTH].rstrip() + "... [TRUNCATED]"
    return cleaned


def _normalize_key(key: str) -> str:
    return key.lower().replace("-", "_").replace(" ", "_")


def _safe_path(value: str) -> str:
    stripped = value.rstrip("/")
    if not stripped:
        return "[path]"
    parts = PurePosixPath(stripped).parts
    if "runs" in parts:
        run_index = parts.index("runs")
        return "/".join(parts[run_index:])
    return PurePosixPath(stripped).name or "[path]"
