from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from backend.harness.runtime_integration.safety import (
    sanitize_runtime_artifacts,
    sanitize_runtime_mapping,
    sanitize_runtime_path,
    sanitize_runtime_text,
)


class HarnessArtifactKind(str, Enum):
    QUALITY = "quality"
    TRACE = "trace"
    MEMORY = "memory"
    REPAIR = "repair"
    REPLAN = "replan"
    BENCHMARK = "benchmark"
    PPTX = "pptx"
    PREVIEW = "preview"
    OTHER = "other"


class HarnessArtifactRef(BaseModel):
    name: str
    kind: HarnessArtifactKind = HarnessArtifactKind.OTHER
    path: str
    exists: bool = False
    required: bool = False
    description: str = ""

    @field_validator("name", "description", mode="before")
    @classmethod
    def _safe_text(cls, value: Any) -> str:
        return sanitize_runtime_text(value, limit=300)

    @field_validator("path", mode="before")
    @classmethod
    def _safe_path(cls, value: Any) -> str:
        return sanitize_runtime_path(str(value))


class HarnessIntegrationConfig(BaseModel):
    enable_episode_memory: bool = True
    enable_repair_planning: bool = True
    enable_replan_decision: bool = True
    enable_one_run_benchmark: bool = False

    execute_repair: bool = False
    apply_replan_patches: bool = False

    fail_soft: bool = True
    include_optional_artifacts: bool = True

    benchmark_suite_id: str = "single_run_smoke"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("benchmark_suite_id", mode="before")
    @classmethod
    def _safe_suite_id(cls, value: Any) -> str:
        text = sanitize_runtime_text(value, limit=160)
        return text or "single_run_smoke"

    @field_validator("metadata", mode="before")
    @classmethod
    def _safe_metadata(cls, value: Any) -> dict[str, Any]:
        return sanitize_runtime_mapping(value)


class HarnessManifest(BaseModel):
    run_id: str
    status: Literal["created", "partial", "success", "warning", "failed"] = "created"

    artifacts: list[HarnessArtifactRef] = Field(default_factory=list)
    missing_required_artifacts: list[str] = Field(default_factory=list)
    missing_optional_artifacts: list[str] = Field(default_factory=list)

    generated_artifacts: dict[str, str] = Field(default_factory=dict)

    quality_status: str | None = None
    trace_status: str | None = None
    repair_plan_status: str | None = None
    replan_status: str | None = None
    benchmark_status: str | None = None

    memory_writes: list[str] = Field(default_factory=list)

    summary: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("run_id", "quality_status", "trace_status", "repair_plan_status", "replan_status", "benchmark_status", "summary", mode="before")
    @classmethod
    def _safe_optional_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        return sanitize_runtime_text(value, limit=500)

    @field_validator("missing_required_artifacts", "missing_optional_artifacts", "memory_writes", mode="before")
    @classmethod
    def _safe_list(cls, value: Any) -> list[str]:
        if not isinstance(value, list | tuple | set):
            return []
        return [sanitize_runtime_text(item, limit=240) for item in value]

    @field_validator("generated_artifacts", mode="before")
    @classmethod
    def _safe_artifacts(cls, value: Any) -> dict[str, str]:
        return sanitize_runtime_artifacts(value)

    @field_validator("metadata", mode="before")
    @classmethod
    def _safe_metadata(cls, value: Any) -> dict[str, Any]:
        return sanitize_runtime_mapping(value)


class HarnessBundleResult(BaseModel):
    run_id: str
    status: Literal["success", "warning", "partial", "failed"] = "partial"

    manifest: HarnessManifest
    artifact_refs: dict[str, str] = Field(default_factory=dict)

    memory_write_ids: list[str] = Field(default_factory=list)
    repair_plan_id: str | None = None
    replan_decision_id: str | None = None
    benchmark_id: str | None = None

    errors: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("run_id", "repair_plan_id", "replan_decision_id", "benchmark_id", mode="before")
    @classmethod
    def _safe_optional_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        return sanitize_runtime_text(value, limit=240)

    @field_validator("artifact_refs", mode="before")
    @classmethod
    def _safe_artifacts(cls, value: Any) -> dict[str, str]:
        return sanitize_runtime_artifacts(value)

    @field_validator("memory_write_ids", "errors", mode="before")
    @classmethod
    def _safe_list(cls, value: Any) -> list[str]:
        if not isinstance(value, list | tuple | set):
            return []
        return [sanitize_runtime_text(item, limit=500) for item in value]

    @field_validator("metadata", mode="before")
    @classmethod
    def _safe_metadata(cls, value: Any) -> dict[str, Any]:
        return sanitize_runtime_mapping(value)
