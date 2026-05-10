from __future__ import annotations

from backend.harness.pipelines.full_generation import PipelineService


def create_outline_pipeline_service() -> PipelineService:
    return PipelineService()

