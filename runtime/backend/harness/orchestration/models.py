from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.harness.orchestration.safety import (
    sanitize_orchestration_mapping,
    sanitize_orchestration_text,
)


class PlanStepType(str, Enum):
    DOCUMENT_SUMMARY = "document_summary"
    OUTLINE_PLANNING = "outline_planning"
    RESEARCH_AND_ASSETS = "research_and_assets"
    SLIDE_GENERATION = "slide_generation"
    CONTENT_QA = "content_qa"
    VISUAL_QA = "visual_qa"
    REPAIR_PLANNING = "repair_planning"
    REPAIR_EXECUTION = "repair_execution"
    FINALIZE = "finalize"

    TOOL_RETRY = "tool_retry"
    FALLBACK_ASSET = "fallback_asset"
    DISABLE_IMAGES = "disable_images"
    SKIP_VISUAL_QA = "skip_visual_qa"
    MANUAL_REVIEW = "manual_review"
    DEGRADED_MODE = "degraded_mode"


class PlanStepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class PlanPatchAction(str, Enum):
    INSERT_STEP = "insert_step"
    SKIP_STEP = "skip_step"
    REPEAT_STEP = "repeat_step"
    REPLACE_STEP = "replace_step"
    UPDATE_STEP = "update_step"
    ANNOTATE_STEP = "annotate_step"
    STOP = "stop"


class PatchRiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RetryPolicy(BaseModel):
    max_attempts: int = 1
    retry_on: list[str] = Field(default_factory=list)
    backoff_s: float = 0.0

    @field_validator("retry_on", mode="before")
    @classmethod
    def _safe_retry_on(cls, value: Any) -> list[str]:
        if not isinstance(value, list | tuple | set):
            return []
        return [sanitize_orchestration_text(item, limit=160) for item in value]


class PlanStep(BaseModel):
    step_id: str
    step_type: PlanStepType
    name: str = ""
    status: PlanStepStatus = PlanStepStatus.PENDING

    agent_name: str | None = None
    capability: str | None = None
    tool_name: str | None = None

    input_refs: list[str] = Field(default_factory=list)
    output_refs: list[str] = Field(default_factory=list)

    preconditions: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)

    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("step_id")
    @classmethod
    def _required_step_id(cls, value: str) -> str:
        text = sanitize_orchestration_text(value, limit=160)
        if not text:
            raise ValueError("step_id must not be empty")
        return text

    @field_validator("name", "agent_name", "capability", "tool_name", mode="before")
    @classmethod
    def _safe_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        return sanitize_orchestration_text(value, limit=240)

    @field_validator("input_refs", "output_refs", "preconditions", "success_criteria", mode="before")
    @classmethod
    def _safe_string_list(cls, value: Any) -> list[str]:
        if not isinstance(value, list | tuple | set):
            return []
        return [sanitize_orchestration_text(item, limit=240) for item in value]

    @field_validator("metadata", mode="before")
    @classmethod
    def _safe_metadata(cls, value: Any) -> dict[str, Any]:
        return sanitize_orchestration_mapping(value)


class PlanGraph(BaseModel):
    run_id: str
    plan_id: str
    status: Literal["created", "running", "completed", "failed", "patched"] = "created"
    steps: list[PlanStep] = Field(default_factory=list)
    created_at: str
    updated_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("run_id", "plan_id")
    @classmethod
    def _required_text(cls, value: str) -> str:
        text = sanitize_orchestration_text(value, limit=160)
        if not text:
            raise ValueError("plan identifier fields must not be empty")
        return text

    @field_validator("metadata", mode="before")
    @classmethod
    def _safe_metadata(cls, value: Any) -> dict[str, Any]:
        return sanitize_orchestration_mapping(value)


class PlanPatch(BaseModel):
    patch_id: str
    run_id: str
    action: PlanPatchAction
    reason: str
    risk_level: PatchRiskLevel = PatchRiskLevel.LOW
    auto_apply: bool = False

    target_step_id: str | None = None
    new_steps: list[PlanStep] = Field(default_factory=list)

    evidence: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("patch_id", "run_id")
    @classmethod
    def _required_text(cls, value: str) -> str:
        text = sanitize_orchestration_text(value, limit=160)
        if not text:
            raise ValueError("patch identifier fields must not be empty")
        return text

    @field_validator("reason", "target_step_id", mode="before")
    @classmethod
    def _safe_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        return sanitize_orchestration_text(value, limit=1000)

    @field_validator("evidence", "metadata", mode="before")
    @classmethod
    def _safe_mapping(cls, value: Any) -> dict[str, Any]:
        return sanitize_orchestration_mapping(value)

    @model_validator(mode="after")
    def _high_risk_not_auto_apply(self) -> PlanPatch:
        if self.risk_level == PatchRiskLevel.HIGH and self.auto_apply:
            self.auto_apply = False
        return self


class ReplanDecision(BaseModel):
    decision_id: str
    run_id: str
    plan_id: str
    status: Literal["no_change", "patch_proposed", "blocked", "error"] = "no_change"

    patches: list[PlanPatch] = Field(default_factory=list)
    summary: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)

    created_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("decision_id", "run_id", "plan_id")
    @classmethod
    def _required_text(cls, value: str) -> str:
        text = sanitize_orchestration_text(value, limit=160)
        if not text:
            raise ValueError("decision identifier fields must not be empty")
        return text

    @field_validator("summary", mode="before")
    @classmethod
    def _safe_summary(cls, value: Any) -> str:
        return sanitize_orchestration_text(value, limit=1200)

    @field_validator("evidence", "metadata", mode="before")
    @classmethod
    def _safe_mapping(cls, value: Any) -> dict[str, Any]:
        return sanitize_orchestration_mapping(value)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_orchestration_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha1("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"
