from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from backend.harness.repair.safety import sanitize_repair_artifacts, sanitize_repair_mapping, sanitize_repair_text


class RepairSource(str, Enum):
    QUALITY = "quality"
    TRACE = "trace"
    TOOL = "tool"
    VISUAL_QA = "visual_qa"
    CONTENT_QA = "content_qa"
    MEMORY = "memory"
    MANUAL = "manual"


class RepairSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class RepairScope(str, Enum):
    DECK = "deck"
    SLIDE = "slide"
    TOOL = "tool"
    CONTENT = "content"
    VISUAL = "visual"
    ASSET = "asset"
    UNKNOWN = "unknown"


class RepairActionType(str, Enum):
    NO_OP = "no_op"
    RETRY_TOOL = "retry_tool"
    REGENERATE_SLIDE = "regenerate_slide"
    REVISE_SLIDE_CODE = "revise_slide_code"
    ADJUST_LAYOUT = "adjust_layout"
    DISABLE_IMAGES = "disable_images"
    FALLBACK_NO_IMAGE = "fallback_no_image"
    RERENDER_PREVIEW = "rerender_preview"
    RESEARCH_RETRY = "research_retry"
    CONTENT_REWRITE = "content_rewrite"
    MANUAL_REVIEW = "manual_review"


class RepairIssue(BaseModel):
    issue_id: str
    run_id: str
    source: RepairSource
    scope: RepairScope = RepairScope.UNKNOWN
    severity: RepairSeverity = RepairSeverity.WARNING

    trigger_stage: str = ""
    issue_type: str = ""
    slide_index: int | None = None
    tool_name: str | None = None
    error_signature: str | None = None

    message: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: dict[str, str] = Field(default_factory=dict)
    retryable: bool = False

    @field_validator("issue_id", "run_id")
    @classmethod
    def _required_text(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("repair identifier fields must not be empty")
        return text

    @field_validator("message", "trigger_stage", "issue_type", "tool_name", "error_signature", mode="before")
    @classmethod
    def _safe_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        return sanitize_repair_text(value, limit=500)

    @field_validator("evidence", "metrics", mode="before")
    @classmethod
    def _safe_mapping(cls, value: Any) -> dict[str, Any]:
        return sanitize_repair_mapping(value)

    @field_validator("artifact_refs", mode="before")
    @classmethod
    def _safe_artifacts(cls, value: Any) -> dict[str, str]:
        return sanitize_repair_artifacts(value)


class RepairAction(BaseModel):
    action_id: str
    issue_id: str
    action_type: RepairActionType
    scope: RepairScope

    target_slide_index: int | None = None
    target_tool: str | None = None

    instruction: str = ""
    memory_refs: list[str] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high"] = "low"
    expected_effect: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("action_id", "issue_id")
    @classmethod
    def _required_text(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("repair action identifier fields must not be empty")
        return text

    @field_validator("instruction", "expected_effect", "target_tool", mode="before")
    @classmethod
    def _safe_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        return sanitize_repair_text(value, limit=1000)

    @field_validator("memory_refs", mode="before")
    @classmethod
    def _safe_memory_refs(cls, value: Any) -> list[str]:
        if not isinstance(value, list | tuple | set):
            return []
        return [sanitize_repair_text(item, limit=160) for item in value]

    @field_validator("metadata", mode="before")
    @classmethod
    def _safe_metadata(cls, value: Any) -> dict[str, Any]:
        return sanitize_repair_mapping(value)


class RepairPlan(BaseModel):
    plan_id: str
    run_id: str
    status: Literal["created", "empty", "planned", "skipped"] = "created"
    issues: list[RepairIssue] = Field(default_factory=list)
    actions: list[RepairAction] = Field(default_factory=list)

    memory_hits: list[dict[str, Any]] = Field(default_factory=list)
    prevention_summary: str = ""
    repair_summary: str = ""

    created_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("plan_id", "run_id")
    @classmethod
    def _required_text(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("repair plan identifier fields must not be empty")
        return text

    @field_validator("prevention_summary", "repair_summary", mode="before")
    @classmethod
    def _safe_text(cls, value: Any) -> str:
        return sanitize_repair_text(value, limit=1200)

    @field_validator("memory_hits", mode="before")
    @classmethod
    def _safe_memory_hits(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [sanitize_repair_mapping(item) for item in value if isinstance(item, dict)]

    @field_validator("metadata", mode="before")
    @classmethod
    def _safe_metadata(cls, value: Any) -> dict[str, Any]:
        return sanitize_repair_mapping(value)


class RepairAttempt(BaseModel):
    attempt_id: str
    plan_id: str
    action_id: str
    issue_id: str
    run_id: str

    status: Literal["success", "failed", "skipped", "not_executed"] = "not_executed"
    started_at: str | None = None
    finished_at: str | None = None

    input_artifacts: dict[str, str] = Field(default_factory=dict)
    output_artifacts: dict[str, str] = Field(default_factory=dict)

    error_signature: str | None = None
    message: str = ""
    metrics: dict[str, Any] = Field(default_factory=dict)

    @field_validator("attempt_id", "plan_id", "action_id", "issue_id", "run_id")
    @classmethod
    def _required_text(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("repair attempt identifier fields must not be empty")
        return text

    @field_validator("message", "error_signature", mode="before")
    @classmethod
    def _safe_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        return sanitize_repair_text(value, limit=500)

    @field_validator("input_artifacts", "output_artifacts", mode="before")
    @classmethod
    def _safe_artifacts(cls, value: Any) -> dict[str, str]:
        return sanitize_repair_artifacts(value)

    @field_validator("metrics", mode="before")
    @classmethod
    def _safe_metrics(cls, value: Any) -> dict[str, Any]:
        return sanitize_repair_mapping(value)


class RepairResult(BaseModel):
    run_id: str
    plan_id: str
    status: Literal["success", "failed", "partial", "skipped", "not_executed"] = "not_executed"

    attempts: list[RepairAttempt] = Field(default_factory=list)
    resolved_issue_ids: list[str] = Field(default_factory=list)
    unresolved_issue_ids: list[str] = Field(default_factory=list)

    quality_before: dict[str, Any] = Field(default_factory=dict)
    quality_after: dict[str, Any] = Field(default_factory=dict)
    quality_delta: dict[str, Any] = Field(default_factory=dict)

    memory_writes: list[str] = Field(default_factory=list)
    artifact_refs: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("run_id", "plan_id")
    @classmethod
    def _required_text(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("repair result identifier fields must not be empty")
        return text

    @field_validator("quality_before", "quality_after", "quality_delta", "metadata", mode="before")
    @classmethod
    def _safe_mapping(cls, value: Any) -> dict[str, Any]:
        return sanitize_repair_mapping(value)

    @field_validator("artifact_refs", mode="before")
    @classmethod
    def _safe_artifacts(cls, value: Any) -> dict[str, str]:
        return sanitize_repair_artifacts(value)

    @field_validator("memory_writes", "resolved_issue_ids", "unresolved_issue_ids", mode="before")
    @classmethod
    def _safe_strings(cls, value: Any) -> list[str]:
        if not isinstance(value, list | tuple | set):
            return []
        return [sanitize_repair_text(item, limit=160) for item in value]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_repair_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha1("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"
