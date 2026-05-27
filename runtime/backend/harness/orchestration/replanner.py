from __future__ import annotations

import logging
from typing import Any

from backend.harness.orchestration.models import (
    PatchRiskLevel,
    PlanGraph,
    PlanPatch,
    PlanPatchAction,
    PlanStepType,
    ReplanDecision,
    stable_orchestration_id,
    utc_now_iso,
)
from backend.harness.orchestration.plan_graph import make_replan_step
from backend.harness.orchestration.policies import ReplannerPolicy
from backend.harness.orchestration.safety import sanitize_orchestration_mapping
from backend.harness.orchestration.signals import RunSignals

logger = logging.getLogger(__name__)


class DeterministicReplanner:
    def __init__(
        self,
        *,
        policy: ReplannerPolicy | None = None,
        trace: Any | None = None,
    ):
        self.policy = policy or ReplannerPolicy()
        self.trace = trace

    def propose(
        self,
        *,
        plan: PlanGraph,
        signals: RunSignals,
    ) -> ReplanDecision:
        patches: list[PlanPatch] = []
        inserted_count = 0

        def add_patch(patch: PlanPatch) -> None:
            nonlocal inserted_count
            if len(patches) >= self.policy.max_patches:
                return
            if patch.action == PlanPatchAction.INSERT_STEP:
                if inserted_count + len(patch.new_steps) > self.policy.max_inserted_steps:
                    return
                inserted_count += len(patch.new_steps)
            patches.append(patch)

        signatures = " ".join(signals.error_signatures)
        if signals.pptx_exists is False or _matches(signals.error_signatures, self.policy.pptx_failure_signatures):
            add_patch(
                self._insert_patch(
                    plan,
                    signals,
                    step_type=PlanStepType.REPAIR_PLANNING,
                    reason="PPTX artifact missing or empty; insert repair planning before finalize.",
                    risk=PatchRiskLevel.MEDIUM,
                    target_step_type=PlanStepType.FINALIZE,
                    evidence={"pptx_exists": signals.pptx_exists, "error_signatures": signals.error_signatures},
                    suffix="pptx",
                )
            )

        if _matches(signals.error_signatures, self.policy.preview_dependency_signatures):
            if self.policy.allow_skip_visual_qa_on_dependency_missing:
                add_patch(
                    self._patch(
                        plan,
                        signals,
                        action=PlanPatchAction.SKIP_STEP,
                        reason="Preview runtime dependency missing; skip or degrade visual QA for this environment.",
                        risk=PatchRiskLevel.LOW,
                        target_step_type=PlanStepType.VISUAL_QA,
                        evidence={"error_signatures": signals.error_signatures},
                        metadata={"mode": "skip_visual_qa", "trigger": "preview_dependency_missing"},
                    )
                )

        preview_failure = (
            signals.preview_success is False
            or _matches(signals.error_signatures, self.policy.preview_failure_signatures)
        )
        dependency_missing = _matches(signals.error_signatures, self.policy.preview_dependency_signatures)
        if preview_failure and not dependency_missing:
            add_patch(
                self._insert_patch(
                    plan,
                    signals,
                    step_type=PlanStepType.TOOL_RETRY,
                    reason="Preview generation failed; rerender preview before visual QA.",
                    risk=PatchRiskLevel.LOW,
                    target_step_type=PlanStepType.VISUAL_QA,
                    evidence={"preview_success": signals.preview_success, "error_signatures": signals.error_signatures},
                    suffix="preview",
                    tool_name="ppt.render_preview",
                )
            )

        if signals.visual_score_min is not None and signals.visual_score_min < self.policy.min_visual_score:
            add_patch(
                self._insert_patch(
                    plan,
                    signals,
                    step_type=PlanStepType.REPAIR_PLANNING,
                    reason="Visual score below threshold; insert visual repair planning.",
                    risk=PatchRiskLevel.LOW,
                    target_step_type=PlanStepType.FINALIZE,
                    evidence={"visual_score_min": signals.visual_score_min, "threshold": self.policy.min_visual_score},
                    suffix="visual",
                )
            )

        if signals.content_issue_count is not None and signals.content_issue_count > self.policy.max_content_issue_count:
            add_patch(
                self._insert_patch(
                    plan,
                    signals,
                    step_type=PlanStepType.REPAIR_PLANNING,
                    reason="Content issue count exceeded threshold; insert content repair planning.",
                    risk=PatchRiskLevel.HIGH,
                    target_step_type=PlanStepType.FINALIZE,
                    evidence={"content_issue_count": signals.content_issue_count, "threshold": self.policy.max_content_issue_count},
                    suffix="content",
                )
            )

        if self.policy.allow_skip_research_on_search_failure and _matches(signals.error_signatures, self.policy.search_failure_signatures):
            add_patch(
                self._patch(
                    plan,
                    signals,
                    action=PlanPatchAction.UPDATE_STEP,
                    reason="Search provider failed; run degraded research mode or skip research.",
                    risk=PatchRiskLevel.LOW,
                    target_step_type=PlanStepType.RESEARCH_AND_ASSETS,
                    evidence={"error_signatures": signals.error_signatures},
                    metadata={"mode": "degraded_research"},
                )
            )

        if self.policy.allow_disable_images_on_asset_failure and _matches(signals.error_signatures, self.policy.asset_failure_signatures):
            add_patch(
                self._insert_patch(
                    plan,
                    signals,
                    step_type=PlanStepType.DISABLE_IMAGES,
                    reason="Asset acquisition failed; fallback to no-image/native-shape mode.",
                    risk=PatchRiskLevel.LOW,
                    target_step_type=PlanStepType.SLIDE_GENERATION,
                    evidence={"error_signatures": signals.error_signatures},
                    suffix="asset",
                )
            )

        if signals.repair_plan_exists and signals.repair_action_count > 0 and not signals.repair_result_exists:
            add_patch(
                self._insert_patch(
                    plan,
                    signals,
                    step_type=PlanStepType.REPAIR_EXECUTION,
                    reason="Repair plan exists; schedule low-risk repair execution review.",
                    risk=PatchRiskLevel.MEDIUM,
                    target_step_type=PlanStepType.FINALIZE,
                    evidence={"repair_action_count": signals.repair_action_count},
                    suffix="repair_execution",
                )
            )

        if self.policy.allow_degraded_mode and (
            signals.failed_tool_count + signals.timeout_tool_count >= 2 or signals.skipped_tool_count >= 2
        ):
            add_patch(
                self._insert_patch(
                    plan,
                    signals,
                    step_type=PlanStepType.DEGRADED_MODE,
                    reason="Multiple tool failures detected; switch to safe degraded mode.",
                    risk=PatchRiskLevel.LOW,
                    target_step_type=PlanStepType.FINALIZE,
                    evidence={
                        "failed_tool_count": signals.failed_tool_count,
                        "timeout_tool_count": signals.timeout_tool_count,
                        "skipped_tool_count": signals.skipped_tool_count,
                    },
                    suffix="degraded",
                )
            )

        repair_result_status = str(signals.metadata.get("repair_result_status") or "")
        if signals.repair_result_exists and repair_result_status in {"failed", "partial"} and self.policy.allow_manual_review:
            add_patch(
                self._insert_patch(
                    plan,
                    signals,
                    step_type=PlanStepType.MANUAL_REVIEW,
                    reason="Repair result is failed or partial; schedule manual review.",
                    risk=PatchRiskLevel.MEDIUM,
                    target_step_type=PlanStepType.FINALIZE,
                    evidence={"repair_result_status": repair_result_status},
                    suffix="repair_result",
                )
            )

        status = "patch_proposed" if patches else "no_change"
        summary = (
            f"Proposed {len(patches)} deterministic patch(es)."
            if patches
            else "No deterministic replanning needed."
        )
        decision = ReplanDecision(
            decision_id=stable_orchestration_id("decision", plan.plan_id, signals.run_id, len(patches), signatures),
            run_id=signals.run_id,
            plan_id=plan.plan_id,
            status=status,
            patches=patches,
            summary=summary,
            evidence={
                "quality_report_exists": signals.quality_report_exists,
                "trace_summary_exists": signals.trace_summary_exists,
                "repair_plan_exists": signals.repair_plan_exists,
                "repair_result_exists": signals.repair_result_exists,
                "error_signature_count": len(signals.error_signatures),
            },
            created_at=utc_now_iso(),
            metadata=_risk_breakdown(patches),
        )
        self._record_trace(decision)
        return decision

    def _insert_patch(
        self,
        plan: PlanGraph,
        signals: RunSignals,
        *,
        step_type: PlanStepType,
        reason: str,
        risk: PatchRiskLevel,
        target_step_type: PlanStepType,
        evidence: dict[str, Any],
        suffix: str,
        tool_name: str | None = None,
    ) -> PlanPatch:
        step = make_replan_step(signals.run_id, step_type, reason, suffix=suffix)
        if tool_name:
            step = step.model_copy(update={"tool_name": tool_name})
        return self._patch(
            plan,
            signals,
            action=PlanPatchAction.INSERT_STEP,
            reason=reason,
            risk=risk,
            target_step_type=target_step_type,
            evidence=evidence,
            new_steps=[step],
            metadata={"inserted_step_type": step_type.value},
        )

    def _patch(
        self,
        plan: PlanGraph,
        signals: RunSignals,
        *,
        action: PlanPatchAction,
        reason: str,
        risk: PatchRiskLevel,
        target_step_type: PlanStepType,
        evidence: dict[str, Any],
        new_steps: list[Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PlanPatch:
        auto_apply = self.policy.low_risk_auto_apply and risk == PatchRiskLevel.LOW
        if risk == PatchRiskLevel.HIGH:
            auto_apply = False
        target_step_id = _find_step_id(plan, target_step_type)
        return PlanPatch(
            patch_id=stable_orchestration_id("patch", plan.plan_id, action.value, reason, target_step_id, len(plan.steps)),
            run_id=signals.run_id,
            action=action,
            reason=reason,
            risk_level=risk,
            auto_apply=auto_apply,
            target_step_id=target_step_id,
            new_steps=new_steps or [],
            evidence=evidence,
            metadata=metadata or {},
        )

    def _record_trace(self, decision: ReplanDecision) -> None:
        if not self.trace:
            return
        record = getattr(self.trace, "record", None)
        if not callable(record):
            return
        payload = {
            "run_id": decision.run_id,
            "plan_id": decision.plan_id,
            "status": decision.status,
            "patch_count": len(decision.patches),
            "summary": decision.summary,
            **_risk_breakdown(decision.patches),
        }
        try:
            record(stage="replan.triggered", payload=sanitize_orchestration_mapping(payload))
        except Exception as exc:
            logger.warning("[Orchestration] Failed to record replan trace; continuing: %s", exc)


def _matches(error_signatures: list[str], patterns: list[str]) -> bool:
    text = "\n".join(error_signatures).lower()
    return any(pattern.lower() in text for pattern in patterns)


def _find_step_id(plan: PlanGraph, step_type: PlanStepType) -> str | None:
    for step in plan.steps:
        if step.step_type == step_type:
            return step.step_id
    return None


def _risk_breakdown(patches: list[PlanPatch]) -> dict[str, Any]:
    return {
        "low_risk_patch_count": sum(1 for patch in patches if patch.risk_level == PatchRiskLevel.LOW),
        "medium_risk_patch_count": sum(1 for patch in patches if patch.risk_level == PatchRiskLevel.MEDIUM),
        "high_risk_patch_count": sum(1 for patch in patches if patch.risk_level == PatchRiskLevel.HIGH),
        "auto_apply_patch_count": sum(1 for patch in patches if patch.auto_apply),
    }
