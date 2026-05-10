from __future__ import annotations

from backend.harness.pipelines.full_generation import PipelineService


def create_generate_from_outline_service() -> PipelineService:
    return PipelineService()

