from __future__ import annotations

import re


class AgentRuntimeError(Exception):
    pass


class AgentNotFoundError(AgentRuntimeError):
    pass


class AgentCapabilityError(AgentRuntimeError):
    pass


class AgentExecutionError(AgentRuntimeError):
    pass


_SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|gho|ghp|xoxb|AKIA|AIza)[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*[^,\s]+"),
    re.compile(r"(?i)\b(system[_ -]?prompt|hidden[_ -]?reasoning|chain[_ -]?of[_ -]?thought)\s*[:=].*"),
]
_PATH_PATTERN = re.compile(r"(?:[A-Za-z]:)?(?:/[^\s:]+)+")
_NON_WORD_PATTERN = re.compile(r"[^a-z0-9]+")


def build_agent_error_signature(
    *,
    agent_name: str,
    capability: str,
    error_type: str,
    message: str,
) -> str:
    safe_agent = _normalize_token(agent_name, fallback="agent")
    safe_capability = _normalize_token(capability, fallback="unknown")
    safe_error_type = _normalize_token(error_type, fallback="Error")
    reason = _classify_reason(error_type=error_type, message=message)
    return f"{safe_agent}:{safe_capability}:{safe_error_type}:{reason}"[:140]


def sanitize_agent_error_message(message: str, *, limit: int = 300) -> str:
    cleaned = str(message or "")
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("[redacted]", cleaned)
    cleaned = _PATH_PATTERN.sub(lambda match: _path_basename(match.group(0)), cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > limit:
        return cleaned[: limit - 3].rstrip() + "..."
    return cleaned


def _classify_reason(*, error_type: str, message: str) -> str:
    text = sanitize_agent_error_message(message, limit=500).lower()
    error_text = str(error_type or "").lower()

    if "timeout" in error_text or "timeout" in text or "timed out" in text:
        return "timeout"
    if "connection" in error_text or "connection" in text or "provider" in text:
        return "provider_unavailable"
    if "json" in text and ("invalid" in text or "decode" in text):
        return "invalid_json"
    if "outline" in text and ("invalid" in text or "bad" in text):
        return "invalid_outline_json"
    if "disabled" in text:
        return "disabled"
    if "unsupported" in text:
        return "unsupported_capability"
    normalized = _NON_WORD_PATTERN.sub("_", text).strip("_")
    if not normalized:
        return "unknown"
    return "_".join(normalized.split("_")[:6])[:64]


def _normalize_token(value: str, *, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("_")
    return cleaned or fallback


def _path_basename(value: str) -> str:
    stripped = value.rstrip("/")
    if not stripped:
        return "[path]"
    return stripped.rsplit("/", 1)[-1] or "[path]"
