from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


TraceEventType = Literal[
    "run.started",
    "run.finished",
    "phase.started",
    "phase.finished",
    "phase.failed",
    "phase.skipped",
    "agent.started",
    "agent.finished",
    "tool.started",
    "tool.finished",
    "qa.completed",
    "repair.started",
    "repair.finished",
    "memory.queried",
    "memory.hit",
    "memory.written",
    "artifact.created",
    "quality.reported",
    "replan.triggered",
]


class TraceEvent(BaseModel):
    run_id: str
    event_id: str
    parent_event_id: str | None = None
    event_type: TraceEventType
    timestamp: str
    phase: str | None = None
    agent_name: str | None = None
    tool_name: str | None = None
    status: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: dict[str, str] = Field(default_factory=dict)
    error_signature: str | None = None

    @field_validator("payload", "metrics", mode="before")
    @classmethod
    def _json_safe_mapping(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        safe = json_safe(value)
        return safe if isinstance(safe, dict) else {}

    @field_validator("artifact_refs", mode="before")
    @classmethod
    def _json_safe_artifact_refs(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {str(key): str(item) for key, item in value.items()}


def new_event_id() -> str:
    return uuid4().hex


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [json_safe(item) for item in value]
    return str(value)
