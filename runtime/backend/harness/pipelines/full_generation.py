from __future__ import annotations

from typing import Callable

from backend.harness.agents.orchestrator import OrchestratorAgent
from backend.harness.runtime import HarnessTrace


class PipelineService:
    """Thin pipeline facade around the harness orchestrator."""

    def create_orchestrator(
        self,
        *,
        debug_layout: bool = False,
        no_research: bool = False,
        no_images: bool = False,
        image_source: str = "auto",
        model_provider: str = "minmax",
        thinking_callback: Callable[[str], None] | None = None,
        search_callback: Callable[[dict], None] | None = None,
        harness_trace: HarnessTrace | None = None,
    ) -> OrchestratorAgent:
        return OrchestratorAgent(
            debug_layout=debug_layout,
            no_research=no_research,
            no_images=no_images,
            image_source=image_source,
            model_provider=model_provider,
            thinking_callback=thinking_callback,
            search_callback=search_callback,
            harness_trace=harness_trace,
        )

    def generate(self, **kwargs):
        orchestrator = self.create_orchestrator(
            debug_layout=kwargs.pop("debug_layout", False),
            no_research=kwargs.pop("no_research", False),
            no_images=kwargs.pop("no_images", False),
            image_source=kwargs.pop("image_source", "auto"),
            model_provider=kwargs.pop("model_provider", "minmax"),
            thinking_callback=kwargs.pop("thinking_callback", None),
            search_callback=kwargs.pop("search_callback", None),
            harness_trace=kwargs.pop("harness_trace", None),
        )
        return orchestrator.generate(**kwargs)

    def generate_bundle(self, **kwargs):
        orchestrator = self.create_orchestrator(
            debug_layout=kwargs.pop("debug_layout", False),
            no_research=kwargs.pop("no_research", False),
            no_images=kwargs.pop("no_images", False),
            image_source=kwargs.pop("image_source", "auto"),
            model_provider=kwargs.pop("model_provider", "minmax"),
            thinking_callback=kwargs.pop("thinking_callback", None),
            search_callback=kwargs.pop("search_callback", None),
            harness_trace=kwargs.pop("harness_trace", None),
        )
        return orchestrator.generate_bundle(**kwargs)
