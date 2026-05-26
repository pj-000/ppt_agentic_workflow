from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolSideEffect(str, Enum):
    NETWORK = "network"
    FILESYSTEM = "filesystem"
    SUBPROCESS = "subprocess"
    LLM = "llm"
    EXTERNAL_API = "external_api"


class ToolSpec(BaseModel):
    name: str
    version: str = "1.0.0"
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    timeout_s: int = 60
    retry: int = 0
    idempotent: bool = True
    side_effects: list[ToolSideEffect] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class ToolCall(BaseModel):
    run_id: str
    call_id: str
    tool_name: str
    caller: str = ""
    input: dict[str, Any] = Field(default_factory=dict)
    purpose: str = ""


class ToolError(BaseModel):
    error_type: str
    message: str
    error_signature: str
    retryable: bool = False
    raw_excerpt: str | None = None


class ToolResult(BaseModel):
    run_id: str
    call_id: str
    tool_name: str
    status: Literal["success", "failed", "timeout", "skipped"]
    output: dict[str, Any] = Field(default_factory=dict)
    error: ToolError | None = None
    latency_ms: int = 0
    artifacts: dict[str, str] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
