from __future__ import annotations

from pydantic import BaseModel, Field


class RepairPolicy(BaseModel):
    max_total_actions: int = 8
    max_actions_per_issue: int = 2
    max_attempts_per_action: int = 1

    min_visual_score: float = 3.5
    max_content_issue_count: int = 5

    allow_tool_retry: bool = True
    allow_slide_regeneration: bool = True
    allow_content_rewrite: bool = True
    allow_disable_images: bool = True
    allow_manual_review: bool = True

    low_risk_only: bool = True

    tool_retryable_signatures: list[str] = Field(
        default_factory=lambda: [
            "TimeoutError",
            "ProviderUnavailable",
            "PreviewGenerationFailed",
        ]
    )

    high_risk_action_types: list[str] = Field(
        default_factory=lambda: [
            "regenerate_slide",
            "content_rewrite",
        ]
    )
