from __future__ import annotations

from copy import deepcopy
from typing import Any

from backend.harness.orchestration.models import (
    PatchRiskLevel,
    PlanGraph,
    PlanPatch,
    PlanPatchAction,
    PlanStep,
    PlanStepStatus,
    PlanStepType,
    RetryPolicy,
    stable_orchestration_id,
    utc_now_iso,
)
from backend.harness.orchestration.safety import sanitize_orchestration_mapping


def build_default_ppt_plan(
    *,
    run_id: str,
    include_document_summary: bool = False,
    include_repair: bool = True,
) -> PlanGraph:
    steps: list[PlanStep] = []
    if include_document_summary:
        steps.append(
            _step(
                run_id,
                PlanStepType.DOCUMENT_SUMMARY,
                "Document summary",
                agent_name="document",
                capability="document_summarize",
                success_criteria=["Document context summarized when input document is present"],
            )
        )
    steps.extend(
        [
            _step(
                run_id,
                PlanStepType.OUTLINE_PLANNING,
                "Outline planning",
                agent_name="planner",
                capability="plan_outline",
                success_criteria=["Outline contains valid slide sequence"],
            ),
            _step(
                run_id,
                PlanStepType.RESEARCH_AND_ASSETS,
                "Research and assets",
                agent_name="researcher/asset",
                capability="research_slide/fetch_assets",
                success_criteria=["Research notes and optional assets are available"],
            ),
            _step(
                run_id,
                PlanStepType.SLIDE_GENERATION,
                "Slide generation",
                agent_name="planner",
                capability="generate_slide_code",
                success_criteria=["PPTX generation code produces an artifact"],
            ),
            _step(
                run_id,
                PlanStepType.CONTENT_QA,
                "Content QA",
                agent_name="evaluator",
                capability="evaluate_content",
                success_criteria=["Content issue count is within threshold"],
            ),
            _step(
                run_id,
                PlanStepType.VISUAL_QA,
                "Visual QA",
                agent_name="evaluator",
                capability="evaluate_visual",
                success_criteria=["Visual score meets threshold when preview is available"],
            ),
        ]
    )
    if include_repair:
        steps.append(
            _step(
                run_id,
                PlanStepType.REPAIR_PLANNING,
                "Repair planning",
                agent_name="repair",
                capability="repair_planning",
                success_criteria=["Repair issues are converted into a structured repair plan"],
            )
        )
    steps.append(
        _step(
            run_id,
            PlanStepType.FINALIZE,
            "Finalize",
            agent_name="orchestrator",
            success_criteria=["Final artifacts and reports are written"],
        )
    )
    now = utc_now_iso()
    return PlanGraph(
        run_id=run_id,
        plan_id=stable_orchestration_id("plan", run_id, include_document_summary, include_repair),
        status="created",
        steps=steps,
        created_at=now,
        updated_at=now,
        metadata={"source": "default_ppt_workflow"},
    )


def apply_plan_patch(plan: PlanGraph, patch: PlanPatch, *, allow_high_risk: bool = False) -> PlanGraph:
    if not patch.auto_apply:
        return _with_patch_history(plan, patch, applied=False, reason="auto_apply_false")
    if patch.risk_level == PatchRiskLevel.HIGH and not allow_high_risk:
        return _with_patch_history(plan, patch, applied=False, reason="high_risk_not_allowed")

    updated = plan.model_copy(deep=True)
    try:
        if patch.action == PlanPatchAction.INSERT_STEP:
            updated.steps = _insert_steps(updated.steps, patch)
        elif patch.action == PlanPatchAction.SKIP_STEP:
            updated.steps = [_patch_step_status(step, patch.target_step_id, PlanStepStatus.SKIPPED) for step in updated.steps]
        elif patch.action == PlanPatchAction.REPEAT_STEP:
            updated.steps = _repeat_step(updated.steps, patch)
        elif patch.action == PlanPatchAction.REPLACE_STEP:
            updated.steps = _replace_step(updated.steps, patch)
        elif patch.action == PlanPatchAction.UPDATE_STEP:
            updated.steps = [_update_step(step, patch) for step in updated.steps]
        elif patch.action == PlanPatchAction.ANNOTATE_STEP:
            updated.steps = [_annotate_step(step, patch) for step in updated.steps]
        elif patch.action == PlanPatchAction.STOP:
            updated.status = "failed"
            updated.steps = [_patch_step_status(step, patch.target_step_id, PlanStepStatus.BLOCKED) for step in updated.steps]
        else:
            raise ValueError(f"unsupported patch action: {patch.action}")
    except Exception as exc:
        return _with_patch_history(plan, patch, applied=False, reason=f"patch application failed: {exc}")

    updated.status = "patched"
    updated.updated_at = utc_now_iso()
    updated.metadata = _append_patch_history(updated.metadata, patch, applied=True, reason="applied")
    return updated


def _step(
    run_id: str,
    step_type: PlanStepType,
    name: str,
    *,
    agent_name: str | None = None,
    capability: str | None = None,
    tool_name: str | None = None,
    success_criteria: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> PlanStep:
    return PlanStep(
        step_id=stable_orchestration_id("step", run_id, step_type.value),
        step_type=step_type,
        name=name,
        agent_name=agent_name,
        capability=capability,
        tool_name=tool_name,
        success_criteria=success_criteria or [],
        retry_policy=RetryPolicy(max_attempts=1),
        metadata=metadata or {},
    )


def make_replan_step(run_id: str, step_type: PlanStepType, reason: str, *, suffix: str = "") -> PlanStep:
    return _step(
        run_id,
        step_type,
        step_type.value.replace("_", " ").title(),
        success_criteria=[reason],
        metadata={"replanned": True, "reason": reason, "suffix": suffix},
    )


def _insert_steps(steps: list[PlanStep], patch: PlanPatch) -> list[PlanStep]:
    if not patch.new_steps:
        raise ValueError("insert_step patch requires new_steps")
    if not patch.target_step_id:
        return [*steps, *patch.new_steps]
    result: list[PlanStep] = []
    inserted = False
    for step in steps:
        if step.step_id == patch.target_step_id and not inserted:
            result.extend(patch.new_steps)
            inserted = True
        result.append(step)
    if not inserted:
        result.extend(patch.new_steps)
    return result


def _repeat_step(steps: list[PlanStep], patch: PlanPatch) -> list[PlanStep]:
    result: list[PlanStep] = []
    repeated = False
    for step in steps:
        result.append(step)
        if step.step_id == patch.target_step_id:
            clone = step.model_copy(
                update={
                    "step_id": stable_orchestration_id("step", patch.run_id, step.step_id, patch.patch_id, "repeat"),
                    "status": PlanStepStatus.PENDING,
                    "metadata": {**step.metadata, "repeated_from": step.step_id},
                }
            )
            result.append(clone)
            repeated = True
    if not repeated:
        raise ValueError(f"target step not found: {patch.target_step_id}")
    return result


def _replace_step(steps: list[PlanStep], patch: PlanPatch) -> list[PlanStep]:
    if not patch.new_steps:
        raise ValueError("replace_step patch requires new_steps")
    result: list[PlanStep] = []
    replaced = False
    for step in steps:
        if step.step_id == patch.target_step_id:
            result.extend(patch.new_steps)
            replaced = True
        else:
            result.append(step)
    if not replaced:
        raise ValueError(f"target step not found: {patch.target_step_id}")
    return result


def _patch_step_status(step: PlanStep, target_step_id: str | None, status: PlanStepStatus) -> PlanStep:
    if target_step_id and step.step_id == target_step_id:
        return step.model_copy(update={"status": status, "metadata": {**step.metadata, "patched": True}})
    return step


def _update_step(step: PlanStep, patch: PlanPatch) -> PlanStep:
    if patch.target_step_id and step.step_id == patch.target_step_id:
        return step.model_copy(update={"metadata": {**step.metadata, **patch.metadata, "patched": True}})
    return step


def _annotate_step(step: PlanStep, patch: PlanPatch) -> PlanStep:
    if patch.target_step_id and step.step_id == patch.target_step_id:
        notes = list(step.metadata.get("notes", []))
        notes.append(patch.reason)
        return step.model_copy(update={"metadata": {**step.metadata, "notes": notes, **patch.metadata}})
    return step


def _with_patch_history(plan: PlanGraph, patch: PlanPatch, *, applied: bool, reason: str) -> PlanGraph:
    updated = plan.model_copy(deep=True)
    updated.metadata = _append_patch_history(updated.metadata, patch, applied=applied, reason=reason)
    updated.updated_at = utc_now_iso()
    return updated


def _append_patch_history(metadata: dict[str, Any], patch: PlanPatch, *, applied: bool, reason: str) -> dict[str, Any]:
    updated = deepcopy(metadata)
    history = list(updated.get("patch_history", []))
    history.append(
        {
            "patch_id": patch.patch_id,
            "action": patch.action.value,
            "risk_level": patch.risk_level.value,
            "auto_apply": patch.auto_apply,
            "applied": applied,
            "reason": reason,
        }
    )
    updated["patch_history"] = history
    return sanitize_orchestration_mapping(updated)
