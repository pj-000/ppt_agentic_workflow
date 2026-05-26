from __future__ import annotations

import logging
from typing import Any

from backend.harness.repair.safety import sanitize_repair_text

logger = logging.getLogger(__name__)


class LegacyRepairOrchestratorAdapter:
    def __init__(self, repair_orchestrator: Any):
        self.repair_orchestrator = repair_orchestrator

    def classify_error(
        self,
        *,
        error: str,
        stage: str,
        image_path: str | None = None,
    ) -> str:
        try:
            classify = getattr(self.repair_orchestrator, "classify_error")
            return sanitize_repair_text(classify(error, stage=stage, image_path=image_path), limit=200) or "generic_retry"
        except Exception as exc:
            logger.warning("[Repair] Legacy classify_error failed; continuing: %s", exc)
            return "generic_retry"

    def build_repair_instruction(
        self,
        *,
        error_signature: str,
        error: str,
        layout_scope: str = "*",
        visual_mode_scope: str = "*",
    ) -> str:
        try:
            method = getattr(self.repair_orchestrator, "build_repair_instruction")
            return sanitize_repair_text(
                method(
                    error_signature=error_signature,
                    error=error,
                    layout_scope=layout_scope,
                    visual_mode_scope=visual_mode_scope,
                ),
                limit=1000,
            )
        except Exception as exc:
            logger.warning("[Repair] Legacy build_repair_instruction failed; continuing: %s", exc)
            return ""

    def prevention_section(
        self,
        *,
        trigger_stage: str,
        layout_scope: str = "*",
        visual_mode_scope: str = "*",
        max_items: int | None = None,
    ) -> str:
        try:
            method = getattr(self.repair_orchestrator, "prevention_section")
            return sanitize_repair_text(
                method(
                    trigger_stage=trigger_stage,
                    layout_scope=layout_scope,
                    visual_mode_scope=visual_mode_scope,
                    max_items=max_items,
                ),
                limit=1200,
            )
        except Exception as exc:
            logger.warning("[Repair] Legacy prevention_section failed; continuing: %s", exc)
            return ""

    def repair_section(
        self,
        *,
        trigger_stage: str,
        error_signature: str,
        layout_scope: str = "*",
        visual_mode_scope: str = "*",
        max_items: int | None = None,
    ) -> str:
        try:
            method = getattr(self.repair_orchestrator, "repair_section")
            return sanitize_repair_text(
                method(
                    trigger_stage=trigger_stage,
                    error_signature=error_signature,
                    layout_scope=layout_scope,
                    visual_mode_scope=visual_mode_scope,
                    max_items=max_items,
                ),
                limit=1200,
            )
        except Exception as exc:
            logger.warning("[Repair] Legacy repair_section failed; continuing: %s", exc)
            return ""

    def remember_success(self, **kwargs: Any) -> None:
        try:
            method = getattr(self.repair_orchestrator, "remember_success")
            method(**kwargs)
        except Exception as exc:
            logger.warning("[Repair] Legacy remember_success failed; continuing: %s", exc)

    def mark_memory_failure(self, memory_id: str) -> None:
        try:
            method = getattr(self.repair_orchestrator, "mark_memory_failure")
            method(memory_id)
        except Exception as exc:
            logger.warning("[Repair] Legacy mark_memory_failure failed; continuing: %s", exc)
