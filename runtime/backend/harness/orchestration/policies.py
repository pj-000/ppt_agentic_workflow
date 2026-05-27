from __future__ import annotations

from pydantic import BaseModel, Field


class ReplannerPolicy(BaseModel):
    min_visual_score: float = 3.5
    max_content_issue_count: int = 5

    allow_skip_research_on_search_failure: bool = True
    allow_disable_images_on_asset_failure: bool = True
    allow_skip_visual_qa_on_dependency_missing: bool = True
    allow_insert_repair_planning: bool = True
    allow_tool_retry: bool = True
    allow_degraded_mode: bool = True
    allow_manual_review: bool = True

    low_risk_auto_apply: bool = False

    max_patches: int = 8
    max_inserted_steps: int = 5

    search_failure_signatures: list[str] = Field(
        default_factory=lambda: [
            "search.web_text",
            "ProviderUnavailable",
            "ConnectionError",
            "TimeoutError",
        ]
    )

    asset_failure_signatures: list[str] = Field(
        default_factory=lambda: [
            "search.image",
            "asset",
            "image",
            "ProviderUnavailable",
        ]
    )

    preview_dependency_signatures: list[str] = Field(
        default_factory=lambda: [
            "DependencyMissing",
            "soffice_not_found",
            "pdftoppm_not_found",
        ]
    )

    pptx_failure_signatures: list[str] = Field(
        default_factory=lambda: [
            "ppt.run_pptxgenjs",
            "PptxArtifactMissing",
            "PptxArtifactEmpty",
        ]
    )

    preview_failure_signatures: list[str] = Field(
        default_factory=lambda: [
            "ppt.render_preview",
            "PreviewGenerationFailed",
        ]
    )
