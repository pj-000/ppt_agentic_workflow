from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentRole(str, Enum):
    ORCHESTRATOR = "orchestrator"
    PLANNER = "planner"
    RESEARCHER = "researcher"
    ASSET = "asset"
    EVALUATOR = "evaluator"
    REPAIR = "repair"
    DOCUMENT = "document"
    UNKNOWN = "unknown"


class AgentCapability(str, Enum):
    PLAN_OUTLINE = "plan_outline"
    DECIDE_VISUAL_THEME = "decide_visual_theme"
    GENERATE_SLIDE_CODE = "generate_slide_code"

    RESEARCH_TOPIC = "research_topic"
    RESEARCH_SLIDE = "research_slide"

    FETCH_ASSETS = "fetch_assets"
    FETCH_SLIDE_ASSET = "fetch_slide_asset"

    EVALUATE_VISUAL = "evaluate_visual"
    EVALUATE_CONTENT = "evaluate_content"

    REPAIR_SLIDE = "repair_slide"
    UNKNOWN = "unknown"


class AgentSpec(BaseModel):
    name: str
    role: AgentRole
    capabilities: list[AgentCapability] = Field(default_factory=list)
    version: str = "1.0.0"
    description: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentContext(BaseModel):
    run_id: str
    trace_id: str | None = None
    language: str = "zh-CN"
    model_provider: str | None = None
    budget: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentRequest(BaseModel):
    run_id: str
    task_id: str
    capability: AgentCapability
    payload: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    input_artifacts: dict[str, str] = Field(default_factory=dict)


class AgentError(BaseModel):
    error_type: str
    message: str
    error_signature: str | None = None
    retryable: bool = False
    raw_excerpt: str | None = None


class AgentResult(BaseModel):
    run_id: str
    task_id: str
    agent_name: str
    capability: AgentCapability
    status: Literal["success", "failed", "skipped", "partial"]
    payload: dict[str, Any] = Field(default_factory=dict)
    output_artifacts: dict[str, str] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    errors: list[AgentError] = Field(default_factory=list)
    memory_writes: list[str] = Field(default_factory=list)
