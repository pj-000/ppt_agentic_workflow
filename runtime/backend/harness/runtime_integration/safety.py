from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.harness.orchestration.safety import (
    sanitize_orchestration_artifacts,
    sanitize_orchestration_mapping,
    sanitize_orchestration_text,
)


def sanitize_runtime_text(value: Any, *, limit: int = 1000) -> str:
    return sanitize_orchestration_text(value, limit=limit)


def sanitize_runtime_mapping(value: Any) -> dict[str, Any]:
    return sanitize_orchestration_mapping(value)


def sanitize_runtime_artifacts(value: Any) -> dict[str, str]:
    return sanitize_orchestration_artifacts(value)


def sanitize_runtime_path(value: str | Path) -> str:
    return sanitize_runtime_artifacts({"path": str(value)}).get("path", Path(value).name)
