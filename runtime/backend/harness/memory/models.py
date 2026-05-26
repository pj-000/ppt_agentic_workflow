from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from backend.harness.memory.namespace import validate_namespace


class MemoryType(str, Enum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class MemoryLifecycleState(str, Enum):
    ACTIVE = "active"
    STALE = "stale"
    RETIRED = "retired"


class MemoryPromotionState(str, Enum):
    NONE = "none"
    CANDIDATE = "candidate"
    PENDING_BENCHMARK = "pending_benchmark"
    PROMOTED = "promoted"
    REJECTED = "rejected"


class MemoryRecord(BaseModel):
    memory_id: str
    namespace: str
    memory_type: MemoryType
    key: str
    content: str

    context: dict[str, Any] = Field(default_factory=dict)
    outcome: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)

    confidence: float = 0.5
    success_count: int = 0
    failure_count: int = 0

    source_run_id: str = ""
    source_trace_id: str | None = None
    source_artifacts: dict[str, str] = Field(default_factory=dict)

    created_at: str
    updated_at: str
    expires_at: str | None = None

    lifecycle_state: MemoryLifecycleState = MemoryLifecycleState.ACTIVE
    promotion_state: MemoryPromotionState = MemoryPromotionState.NONE

    @field_validator("namespace")
    @classmethod
    def _namespace_required(cls, value: str) -> str:
        return validate_namespace(value)

    @field_validator("memory_id")
    @classmethod
    def _memory_id_required(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("memory_id must not be empty")
        return text

    @field_validator("key")
    @classmethod
    def _key_required(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("key must not be empty")
        return text

    @field_validator("confidence")
    @classmethod
    def _confidence_range(cls, value: float) -> float:
        if value < 0 or value > 1:
            raise ValueError("confidence must be between 0 and 1")
        return value


class MemoryHit(BaseModel):
    record: MemoryRecord
    score: float
    reason: str = ""


class MemoryQuery(BaseModel):
    namespace: str
    query: str = ""
    memory_type: MemoryType | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    top_k: int = 5
    min_score: float = 0.0

    @field_validator("namespace")
    @classmethod
    def _query_namespace_required(cls, value: str) -> str:
        return validate_namespace(value)


class MemoryWriteResult(BaseModel):
    memory_id: str
    created: bool
    updated: bool = False
    skipped: bool = False
    reason: str = ""
