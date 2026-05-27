from __future__ import annotations

from backend.harness.orchestration.models import PatchRiskLevel, PlanGraph, ReplanDecision
from backend.harness.orchestration.plan_graph import apply_plan_patch
from backend.harness.orchestration.safety import sanitize_orchestration_mapping


def simulate_replan_decision(
    *,
    plan: PlanGraph,
    decision: ReplanDecision,
    apply_auto_patches: bool = True,
    force_apply_all_low_risk: bool = False,
    allow_high_risk: bool = False,
) -> PlanGraph:
    simulated = plan.model_copy(deep=True)
    simulated_records = []
    for patch in decision.patches:
        should_apply = False
        if apply_auto_patches and patch.auto_apply:
            should_apply = True
        if force_apply_all_low_risk and patch.risk_level == PatchRiskLevel.LOW:
            should_apply = True
        if patch.risk_level == PatchRiskLevel.HIGH:
            should_apply = bool(allow_high_risk and force_apply_all_low_risk)
        if patch.risk_level == PatchRiskLevel.HIGH and not allow_high_risk:
            reason = "high_risk_not_allowed"
        elif not should_apply:
            reason = "auto_apply_false"
        else:
            reason = "simulated_apply"
        if should_apply:
            patched = patch.model_copy(update={"auto_apply": True})
            simulated = apply_plan_patch(simulated, patched, allow_high_risk=allow_high_risk)
        simulated_records.append(
            {
                "patch_id": patch.patch_id,
                "applied": should_apply,
                "reason": reason,
                "risk_level": patch.risk_level.value,
            }
        )
    simulated.metadata = sanitize_orchestration_mapping(
        {
            **simulated.metadata,
            "simulation": {
                "decision_id": decision.decision_id,
                "patches": simulated_records,
            },
        }
    )
    return simulated
