from __future__ import annotations

import re


_SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|gho|ghp|xoxb|AKIA|AIza)[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*[^,\s]+"),
]
_PATH_PATTERN = re.compile(r"(?:[A-Za-z]:)?(?:/[^\s:]+)+")
_NON_WORD_PATTERN = re.compile(r"[^a-z0-9]+")


def build_error_signature(
    *,
    tool_name: str,
    error_type: str,
    message: str,
    stage: str | None = None,
) -> str:
    safe_tool = _normalize_token(stage or tool_name, fallback="tool")
    safe_error_type = _normalize_token(error_type, fallback="Error")
    reason = _classify_reason(tool_name=tool_name, error_type=error_type, message=message)
    return f"{safe_tool}:{safe_error_type}:{reason}"[:120]


def sanitize_error_message(message: str, *, limit: int = 300) -> str:
    cleaned = str(message or "")
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("[redacted]", cleaned)
    cleaned = _PATH_PATTERN.sub(lambda match: _path_basename(match.group(0)), cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > limit:
        return cleaned[: limit - 3].rstrip() + "..."
    return cleaned


def _classify_reason(*, tool_name: str, error_type: str, message: str) -> str:
    text = sanitize_error_message(message, limit=500).lower()
    error_text = str(error_type or "").lower()
    tool_text = str(tool_name or "").lower()

    if "timeout" in error_text or "timed out" in text or "timeout" in text:
        if "render" in tool_text or "libreoffice" in text or "soffice" in text:
            return "libreoffice_timeout"
        return "timeout"
    if "syntax" in error_text or "syntaxerror" in text:
        if "missing" in text and ";" in text:
            return "missing_semicolon"
        if "unexpected token" in text:
            return "unexpected_token"
        return "syntax_error"
    if "connection" in error_text or "connection" in text or "provider" in text:
        return "provider_unavailable"
    if "file not found" in text or "no such file" in text or "not exist" in text or "does not exist" in text:
        return "file_not_found"
    if "unsupported" in text:
        return "unsupported_format"
    if "node" in text and ("not found" in text or "enoent" in text):
        return "node_not_available"
    if "libreoffice" in text or "soffice" in text:
        return "libreoffice_unavailable"
    if "skipped" in text or "not implemented" in text:
        return "not_implemented"

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
