from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


IssueSeverity = Literal["info", "warning", "error", "critical"]
IssueSource = Literal["content_qa", "visual_qa", "preview", "pptx_parse", "tool", "repair", "system"]


class QualityIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_id: str
    issue_type: str
    severity: IssueSeverity
    slide_index: int | None
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    suggested_fix: str | None = None
    source: IssueSource


class SlideQualityMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slide_index: int
    title: str | None = None
    text_length: int | None = None
    has_image: bool | None = None
    has_chart_or_diagram: bool | None = None
    visual_score: float | None = None
    layout_score: float | None = None
    content_score: float | None = None
    design_score: float | None = None
    issue_count: int = 0
    issues: list[QualityIssue] = Field(default_factory=list)
    repaired: bool = False
    repair_attempts: int = 0
    before_repair_score: float | None = None
    after_repair_score: float | None = None


class RunQualityMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    topic: str | None = None
    slide_count: int | None = None
    pptx_exists: bool = False
    pptx_path: str | None = None
    preview_success: bool = False
    preview_image_count: int = 0
    extracted_text_length: int | None = None
    content_issue_count: int = 0
    visual_score_avg: float | None = None
    visual_score_min: float | None = None
    repaired_slide_count: int = 0
    repair_attempt_count: int = 0
    tool_error_count: int = 0
    total_latency_ms: int | None = None
    stage_latency_ms: dict[str, int] = Field(default_factory=dict)
    created_at: str


class QualityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run: RunQualityMetrics
    slides: list[SlideQualityMetrics] = Field(default_factory=list)
    issues: list[QualityIssue] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    missing_reasons: dict[str, str] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
