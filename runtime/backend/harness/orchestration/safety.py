from __future__ import annotations

from typing import Any

from backend.harness.memory.safety import sanitize_memory_artifacts, sanitize_memory_mapping, sanitize_memory_text


def sanitize_orchestration_text(value: Any, *, limit: int = 1000) -> str:
    return sanitize_memory_text(value, limit=limit)


def sanitize_orchestration_mapping(value: Any) -> dict[str, Any]:
    return sanitize_memory_mapping(value)


def sanitize_orchestration_artifacts(value: Any) -> dict[str, str]:
    return sanitize_memory_artifacts(value)


def sanitize_orchestration_path(value: Any) -> str:
    return sanitize_orchestration_artifacts({"path": str(value)}).get("path", "")
