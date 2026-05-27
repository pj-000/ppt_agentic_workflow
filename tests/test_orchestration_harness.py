from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from backend.harness.orchestration import (  # noqa: E402
    DeterministicReplanner,
    PatchRiskLevel,
    PlanPatch,
    PlanPatchAction,
    PlanStep,
    PlanStepStatus,
    PlanStepType,
    ReplanDecision,
    ReplannerPolicy,
    RetryPolicy,
    RunSignals,
    apply_plan_patch,
    build_default_ppt_plan,
    build_replan_decision_from_run_artifacts,
    extract_run_signals_from_artifacts,
    simulate_replan_decision,
    write_plan_graph,
    write_replan_artifacts_for_run,
    write_replan_decision,
)
from backend.harness.orchestration.models import stable_orchestration_id, utc_now_iso  # noqa: E402
from backend.harness.orchestration.plan_graph import make_replan_step  # noqa: E402
from backend.harness.orchestration.report import write_replan_report_markdown  # noqa: E402


def _step(step_type: PlanStepType = PlanStepType.TOOL_RETRY) -> PlanStep:
    return PlanStep(
        step_id=stable_orchestration_id("step", "run_1", step_type.value, "test"),
        step_type=step_type,
        name=step_type.value,
        success_criteria=["complete"],
    )


def _patch(
    action: PlanPatchAction = PlanPatchAction.INSERT_STEP,
    *,
    target_step_id: str | None = None,
    new_steps: list[PlanStep] | None = None,
    risk_level: PatchRiskLevel = PatchRiskLevel.LOW,
    auto_apply: bool = True,
    reason: str = "test patch",
    metadata: dict[str, Any] | None = None,
) -> PlanPatch:
    return PlanPatch(
        patch_id=stable_orchestration_id("patch", action.value, target_step_id, reason),
        run_id="run_1",
        action=action,
        reason=reason,
        risk_level=risk_level,
        auto_apply=auto_apply,
        target_step_id=target_step_id,
        new_steps=new_steps or [],
        evidence={"reason": reason},
        metadata=metadata or {},
    )


def _signals(**updates: Any) -> RunSignals:
    base = {
        "run_id": "run_1",
        "quality_report_exists": True,
        "trace_summary_exists": True,
        "pptx_exists": True,
        "preview_success": True,
        "visual_score_min": 4.0,
        "visual_score_avg": 4.2,
        "content_issue_count": 1,
    }
    base.update(updates)
    return RunSignals(**base)


def _quality_report() -> dict[str, Any]:
    return {
        "run": {
            "pptx_exists": False,
            "preview_success": False,
            "visual_score_min": 2.8,
            "visual_score_avg": 3.0,
            "content_issue_count": 8,
        },
        "missing_reasons": {"stage_latency_ms": "not available"},
    }


def _trace_summary() -> dict[str, Any]:
    return {
        "status": "warning",
        "failed_tool_count": 1,
        "skipped_tool_count": 2,
        "timeout_tool_count": 1,
        "error_signatures": [
            "ppt.run_pptxgenjs:PptxArtifactEmpty:empty_file",
            "ppt.render_preview:DependencyMissing:soffice_not_found",
            "search.web_text:ConnectionError:provider_unavailable",
            "search.image:ProviderUnavailable:image_provider",
        ],
        "artifact_refs": {"quality": "/private/tmp/project/outputs/runs/run_1/quality_report.json"},
    }


def _repair_plan() -> dict[str, Any]:
    return {
        "run_id": "run_1",
        "plan_id": "repair_1",
        "issues": [{"issue_id": "i1"}],
        "actions": [{"action_id": "a1", "action_type": "rerender_preview", "metadata": {"auto_execute": True}}],
    }


def _repair_result(status: str = "partial") -> dict[str, Any]:
    return {"run_id": "run_1", "plan_id": "repair_1", "status": status, "metadata": {"attempt_count": 2, "repair_success_rate": 0.5}}


def test_orchestration_models_serialize_and_sanitize() -> None:
    step = _step()
    graph = build_default_ppt_plan(run_id="run_1")
    patch = _patch(risk_level=PatchRiskLevel.HIGH, auto_apply=True, reason="api_key=sk-secret123456789")
    decision = ReplanDecision(
        decision_id="decision_1",
        run_id="run_1",
        plan_id=graph.plan_id,
        status="patch_proposed",
        patches=[patch],
        summary="system_prompt=private",
        evidence={"authorization": "Bearer sk-secret123456789"},
        created_at=utc_now_iso(),
    )
    serialized = step.model_dump_json() + graph.model_dump_json() + patch.model_dump_json() + decision.model_dump_json()

    assert PlanStep.model_validate_json(step.model_dump_json()).step_type == PlanStepType.TOOL_RETRY
    assert RetryPolicy.model_validate_json(RetryPolicy().model_dump_json()).max_attempts == 1
    assert patch.auto_apply is False
    assert "sk-secret123456789" not in serialized
    assert "system_prompt=private" not in serialized


def test_build_default_plan_and_apply_patches() -> None:
    plan = build_default_ppt_plan(run_id="run_1")
    doc_plan = build_default_ppt_plan(run_id="run_1", include_document_summary=True)
    types = [step.step_type for step in plan.steps]

    assert PlanStepType.OUTLINE_PLANNING in types
    assert PlanStepType.RESEARCH_AND_ASSETS in types
    assert PlanStepType.REPAIR_PLANNING in types
    assert doc_plan.steps[0].step_type == PlanStepType.DOCUMENT_SUMMARY

    finalize_id = next(step.step_id for step in plan.steps if step.step_type == PlanStepType.FINALIZE)
    insert = _patch(target_step_id=finalize_id, new_steps=[_step(PlanStepType.TOOL_RETRY)])
    inserted = apply_plan_patch(plan, insert)
    assert len(inserted.steps) == len(plan.steps) + 1
    assert inserted.metadata["patch_history"][0]["applied"] is True

    skip = _patch(PlanPatchAction.SKIP_STEP, target_step_id=finalize_id)
    skipped = apply_plan_patch(plan, skip)
    assert next(step for step in skipped.steps if step.step_id == finalize_id).status == PlanStepStatus.SKIPPED

    update = _patch(PlanPatchAction.UPDATE_STEP, target_step_id=finalize_id, metadata={"mode": "degraded"})
    updated = apply_plan_patch(plan, update)
    assert next(step for step in updated.steps if step.step_id == finalize_id).metadata["mode"] == "degraded"

    high = _patch(target_step_id=finalize_id, new_steps=[_step()], risk_level=PatchRiskLevel.HIGH, auto_apply=True)
    not_applied = apply_plan_patch(plan, high)
    assert len(not_applied.steps) == len(plan.steps)
    assert not_applied.metadata["patch_history"][0]["reason"] == "auto_apply_false"


def test_make_replan_step_generates_unique_ids_for_same_step_type() -> None:
    step_a = make_replan_step("run1", PlanStepType.REPAIR_PLANNING, "visual issue", suffix="visual")
    step_b = make_replan_step("run1", PlanStepType.REPAIR_PLANNING, "content issue", suffix="content")

    assert step_a.step_id != step_b.step_id


def test_apply_skip_patch_with_missing_target_is_not_applied() -> None:
    plan = build_default_ppt_plan(run_id="run_1")
    patch = _patch(PlanPatchAction.SKIP_STEP, target_step_id=None)

    updated = apply_plan_patch(plan, patch)

    assert updated.metadata["patch_history"][0]["applied"] is False
    assert updated.metadata["patch_history"][0]["reason"] == "target_step_id_missing"


def test_apply_update_patch_with_unknown_target_is_not_applied() -> None:
    plan = build_default_ppt_plan(run_id="run_1")
    patch = _patch(PlanPatchAction.UPDATE_STEP, target_step_id="missing_step")

    updated = apply_plan_patch(plan, patch)

    assert updated.metadata["patch_history"][0]["applied"] is False
    assert updated.metadata["patch_history"][0]["reason"] == "target_step_not_found"


def test_apply_insert_patch_without_target_appends() -> None:
    plan = build_default_ppt_plan(run_id="run_1")
    patch = _patch(target_step_id=None, new_steps=[_step(PlanStepType.TOOL_RETRY)])

    updated = apply_plan_patch(plan, patch)

    assert len(updated.steps) == len(plan.steps) + 1
    assert updated.steps[-1].step_type == PlanStepType.TOOL_RETRY
    assert updated.metadata["patch_history"][0]["applied"] is True


def test_extract_run_signals_from_artifacts(tmp_path: Path) -> None:
    run_dir = tmp_path / "outputs" / "runs" / "run_1"
    run_dir.mkdir(parents=True)
    (run_dir / "quality_report.json").write_text(json.dumps(_quality_report(), ensure_ascii=False), encoding="utf-8")
    (run_dir / "trace_summary.json").write_text(json.dumps(_trace_summary(), ensure_ascii=False), encoding="utf-8")
    (run_dir / "repair_plan.json").write_text(json.dumps(_repair_plan(), ensure_ascii=False), encoding="utf-8")
    (run_dir / "repair_result.json").write_text(json.dumps(_repair_result(), ensure_ascii=False), encoding="utf-8")

    signals = extract_run_signals_from_artifacts(run_id="run_1", run_dir=run_dir)
    serialized_refs = json.dumps(signals.artifact_refs, ensure_ascii=False)

    assert signals.quality_report_exists is True
    assert signals.trace_summary_exists is True
    assert signals.repair_plan_exists is True
    assert signals.repair_result_exists is True
    assert signals.pptx_exists is False
    assert signals.preview_success is False
    assert signals.visual_score_min == 2.8
    assert signals.content_issue_count == 8
    assert signals.failed_tool_count == 1
    assert signals.skipped_tool_count == 2
    assert signals.timeout_tool_count == 1
    assert signals.repair_issue_count == 1
    assert signals.repair_action_count == 1
    assert signals.repair_auto_executable_action_count == 1
    assert signals.repair_non_auto_action_count == 0
    assert signals.repair_attempt_count == 2
    assert signals.repair_success_rate == 0.5
    assert "/private/tmp" not in serialized_refs
    assert "runs/run_1/quality_report.json" in serialized_refs


def test_extract_run_signals_does_not_treat_missing_repair_artifacts_as_core_missing(tmp_path: Path) -> None:
    run_dir = tmp_path / "outputs" / "runs" / "run_1"
    run_dir.mkdir(parents=True)
    (run_dir / "quality_report.json").write_text(json.dumps(_quality_report(), ensure_ascii=False), encoding="utf-8")
    (run_dir / "trace_summary.json").write_text(json.dumps(_trace_summary(), ensure_ascii=False), encoding="utf-8")

    signals = extract_run_signals_from_artifacts(run_id="run_1", run_dir=run_dir)

    assert "repair_plan.json" not in signals.missing_artifacts
    assert "repair_result.json" not in signals.missing_artifacts
    assert "repair_plan.json" in signals.metadata["optional_missing_artifacts"]
    assert "repair_result.json" in signals.metadata["optional_missing_artifacts"]


def test_extract_run_signals_sanitizes_run_dir_metadata(tmp_path: Path) -> None:
    run_dir = tmp_path / "private" / "tmp" / "project" / "outputs" / "runs" / "run_1"
    run_dir.mkdir(parents=True)
    (run_dir / "quality_report.json").write_text(json.dumps(_quality_report(), ensure_ascii=False), encoding="utf-8")
    (run_dir / "trace_summary.json").write_text(json.dumps(_trace_summary(), ensure_ascii=False), encoding="utf-8")

    signals = extract_run_signals_from_artifacts(run_id="run_1", run_dir=run_dir)
    serialized = json.dumps(signals.metadata, ensure_ascii=False)

    assert "/private/tmp" not in serialized
    assert "/home/" not in serialized
    assert "/Users/" not in serialized
    assert "run_1" in serialized


def test_extract_run_signals_handles_missing_and_invalid_json(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run_bad"
    run_dir.mkdir(parents=True)
    (run_dir / "quality_report.json").write_text("not json", encoding="utf-8")

    signals = extract_run_signals_from_artifacts(run_id="run_bad", run_dir=run_dir)

    assert signals.quality_report_exists is False
    assert "quality_report.json" in signals.missing_artifacts
    assert "invalid quality_report.json" in signals.missing_reasons["quality_report.json"]
    assert "trace_summary.json" in signals.missing_artifacts


def test_replanner_proposes_patches_for_core_failure_signals() -> None:
    plan = build_default_ppt_plan(run_id="run_1")
    signals = _signals(
        pptx_exists=False,
        preview_success=False,
        visual_score_min=2.5,
        content_issue_count=9,
        failed_tool_count=1,
        timeout_tool_count=1,
        skipped_tool_count=2,
        repair_plan_exists=True,
        repair_result_exists=False,
        repair_action_count=2,
        repair_auto_executable_action_count=2,
        error_signatures=[
            "ppt.run_pptxgenjs:PptxArtifactEmpty:empty_file",
            "ppt.render_preview:PreviewGenerationFailed:no_images",
            "search.web_text:ConnectionError:provider_unavailable",
            "search.image:ProviderUnavailable:image_provider",
        ],
    )

    decision = DeterministicReplanner(policy=ReplannerPolicy(max_patches=20, max_inserted_steps=20)).propose(plan=plan, signals=signals)
    reasons = " ".join(patch.reason for patch in decision.patches)
    inserted_types = [step.step_type for patch in decision.patches for step in patch.new_steps]

    assert decision.status == "patch_proposed"
    assert "PPTX artifact missing" in reasons
    assert "Preview generation failed" in reasons
    assert "Visual score below threshold" in reasons
    assert "Content issue count exceeded" in reasons
    assert "Search provider failed" in reasons
    assert "Asset acquisition failed" in reasons
    assert "Repair plan exists" in reasons
    assert "Multiple tool failures detected" in reasons
    assert PlanStepType.REPAIR_PLANNING in inserted_types
    assert PlanStepType.DISABLE_IMAGES in inserted_types
    assert PlanStepType.DEGRADED_MODE in inserted_types
    assert any(patch.risk_level == PatchRiskLevel.HIGH and patch.auto_apply is False for patch in decision.patches)


def test_replanner_dependency_missing_skips_visual_qa() -> None:
    plan = build_default_ppt_plan(run_id="run_1")
    signals = _signals(error_signatures=["ppt.render_preview:DependencyMissing:soffice_not_found"])

    decision = DeterministicReplanner().propose(plan=plan, signals=signals)
    patch = next(patch for patch in decision.patches if patch.action == PlanPatchAction.SKIP_STEP)
    target = next(step for step in plan.steps if step.step_type == PlanStepType.VISUAL_QA)

    assert patch.target_step_id == target.step_id
    assert patch.risk_level == PatchRiskLevel.LOW
    assert patch.auto_apply is False


def test_replanner_marks_patch_when_target_step_missing() -> None:
    plan = build_default_ppt_plan(run_id="run_1")
    plan = plan.model_copy(update={"steps": [step for step in plan.steps if step.step_type != PlanStepType.VISUAL_QA]})
    signals = _signals(error_signatures=["ppt.render_preview:DependencyMissing:soffice_not_found"])

    decision = DeterministicReplanner().propose(plan=plan, signals=signals)
    patch = next(patch for patch in decision.patches if patch.action == PlanPatchAction.SKIP_STEP)

    assert patch.target_step_id is None
    assert patch.metadata["target_step_missing"] is True


def test_replanner_repair_plan_with_auto_executable_actions_schedules_repair_execution() -> None:
    plan = build_default_ppt_plan(run_id="run_1")
    signals = _signals(
        repair_plan_exists=True,
        repair_result_exists=False,
        repair_action_count=2,
        repair_auto_executable_action_count=1,
        repair_non_auto_action_count=1,
    )

    decision = DeterministicReplanner().propose(plan=plan, signals=signals)
    inserted_types = [step.step_type for patch in decision.patches for step in patch.new_steps]

    assert PlanStepType.REPAIR_EXECUTION in inserted_types
    assert PlanStepType.MANUAL_REVIEW not in inserted_types


def test_replanner_repair_plan_with_only_non_auto_actions_schedules_manual_review() -> None:
    plan = build_default_ppt_plan(run_id="run_1")
    signals = _signals(
        repair_plan_exists=True,
        repair_result_exists=False,
        repair_action_count=2,
        repair_auto_executable_action_count=0,
        repair_non_auto_action_count=2,
    )

    decision = DeterministicReplanner().propose(plan=plan, signals=signals)
    inserted_types = [step.step_type for patch in decision.patches for step in patch.new_steps]
    reasons = " ".join(patch.reason for patch in decision.patches)

    assert PlanStepType.MANUAL_REVIEW in inserted_types
    assert PlanStepType.REPAIR_EXECUTION not in inserted_types
    assert "non-auto-executable" in reasons


def test_replanner_dedupes_equivalent_patches() -> None:
    plan = build_default_ppt_plan(run_id="run_1")
    signals = _signals(
        pptx_exists=False,
        error_signatures=["ppt.run_pptxgenjs:PptxArtifactMissing:pptx_file_missing"],
    )

    decision = DeterministicReplanner().propose(plan=plan, signals=signals)
    repair_patches = [
        patch
        for patch in decision.patches
        if any(step.step_type == PlanStepType.REPAIR_PLANNING for step in patch.new_steps)
    ]

    assert len(repair_patches) == 1
    assert decision.metadata["deduped_patch_count"] == 0


def test_replan_decision_metadata_contains_patch_summary() -> None:
    plan = build_default_ppt_plan(run_id="run_1")
    signals = _signals(pptx_exists=False, error_signatures=["ppt.render_preview:DependencyMissing:soffice_not_found"])

    decision = DeterministicReplanner().propose(plan=plan, signals=signals)

    assert decision.metadata["patch_count"] == len(decision.patches)
    assert decision.metadata["insert_step_count"] >= 1
    assert decision.metadata["skip_step_count"] >= 1
    assert "manual_review_patch_count" in decision.metadata
    assert "deduped_patch_count" in decision.metadata


def test_replanner_trace_failed_without_signature_schedules_manual_review() -> None:
    plan = build_default_ppt_plan(run_id="run_1")
    signals = _signals(trace_status="failed", error_signatures=[])

    decision = DeterministicReplanner().propose(plan=plan, signals=signals)
    inserted_types = [step.step_type for patch in decision.patches for step in patch.new_steps]

    assert PlanStepType.MANUAL_REVIEW in inserted_types
    assert "Trace summary indicates failed run" in decision.patches[0].reason


def test_replanner_no_change_limits_and_trace() -> None:
    class FakeTrace:
        def __init__(self) -> None:
            self.records: list[tuple[str, dict[str, Any]]] = []

        def record(self, stage: str, payload: dict[str, Any]) -> None:
            self.records.append((stage, payload))

    plan = build_default_ppt_plan(run_id="run_1")
    trace = FakeTrace()
    no_change = DeterministicReplanner(trace=trace).propose(plan=plan, signals=_signals())

    assert no_change.status == "no_change"
    assert no_change.patches == []
    assert trace.records[-1][0] == "replan.triggered"
    assert trace.records[-1][1]["patch_count"] == 0

    noisy = _signals(
        pptx_exists=False,
        preview_success=False,
        visual_score_min=1.0,
        content_issue_count=99,
        failed_tool_count=5,
        timeout_tool_count=5,
        skipped_tool_count=5,
        repair_plan_exists=True,
        repair_action_count=3,
        repair_auto_executable_action_count=3,
        error_signatures=["ppt.run_pptxgenjs:PptxArtifactEmpty", "search.image:ProviderUnavailable", "search.web_text:TimeoutError"],
    )
    limited = DeterministicReplanner(policy=ReplannerPolicy(max_patches=2, max_inserted_steps=1)).propose(plan=plan, signals=noisy)
    inserted_count = sum(len(patch.new_steps) for patch in limited.patches if patch.action == PlanPatchAction.INSERT_STEP)
    assert len(limited.patches) <= 2
    assert inserted_count <= 1


def test_replanner_trace_failure_is_best_effort() -> None:
    class BrokenTrace:
        def record(self, stage: str, payload: dict[str, Any]) -> None:
            raise RuntimeError("trace down")

    plan = build_default_ppt_plan(run_id="run_1")
    decision = DeterministicReplanner(trace=BrokenTrace()).propose(plan=plan, signals=_signals(pptx_exists=False))

    assert decision.patches


def test_simulator_applies_only_auto_apply_patches_by_default() -> None:
    plan = build_default_ppt_plan(run_id="run_1")
    finalize_id = next(step.step_id for step in plan.steps if step.step_type == PlanStepType.FINALIZE)
    auto_patch = _patch(target_step_id=finalize_id, new_steps=[_step(PlanStepType.TOOL_RETRY)], auto_apply=True)
    non_auto_patch = _patch(
        target_step_id=finalize_id,
        new_steps=[_step(PlanStepType.FALLBACK_ASSET)],
        auto_apply=False,
        reason="non auto",
    )
    decision = ReplanDecision(
        decision_id="decision_1",
        run_id="run_1",
        plan_id=plan.plan_id,
        status="patch_proposed",
        patches=[auto_patch, non_auto_patch],
        created_at=utc_now_iso(),
    )

    simulated = simulate_replan_decision(plan=plan, decision=decision)

    assert len(simulated.steps) == len(plan.steps) + 1
    assert simulated.metadata["simulation"]["patches"][0]["applied"] is True
    assert simulated.metadata["simulation"]["patches"][1]["applied"] is False


def test_simulator_force_apply_low_risk_does_not_apply_high_risk() -> None:
    plan = build_default_ppt_plan(run_id="run_1")
    finalize_id = next(step.step_id for step in plan.steps if step.step_type == PlanStepType.FINALIZE)
    low_patch = _patch(target_step_id=finalize_id, new_steps=[_step(PlanStepType.TOOL_RETRY)], auto_apply=False)
    high_patch = _patch(
        target_step_id=finalize_id,
        new_steps=[_step(PlanStepType.MANUAL_REVIEW)],
        risk_level=PatchRiskLevel.HIGH,
        auto_apply=True,
        reason="high risk",
    )
    decision = ReplanDecision(
        decision_id="decision_1",
        run_id="run_1",
        plan_id=plan.plan_id,
        status="patch_proposed",
        patches=[low_patch, high_patch],
        created_at=utc_now_iso(),
    )

    low_applied = simulate_replan_decision(plan=plan, decision=decision, force_apply_all_low_risk=True)

    assert len(low_applied.steps) == len(plan.steps) + 1
    assert low_applied.metadata["simulation"]["patches"][1]["applied"] is False


def test_simulator_allow_high_risk_requires_explicit_high_risk_flag() -> None:
    plan = build_default_ppt_plan(run_id="run_1")
    finalize_id = next(step.step_id for step in plan.steps if step.step_type == PlanStepType.FINALIZE)
    high_patch = _patch(
        target_step_id=finalize_id,
        new_steps=[_step(PlanStepType.MANUAL_REVIEW)],
        risk_level=PatchRiskLevel.HIGH,
        auto_apply=True,
        reason="high risk",
    )
    decision = ReplanDecision(
        decision_id="decision_1",
        run_id="run_1",
        plan_id=plan.plan_id,
        status="patch_proposed",
        patches=[high_patch],
        created_at=utc_now_iso(),
    )

    not_applied = simulate_replan_decision(plan=plan, decision=decision, force_apply_all_low_risk=True)
    high_applied = simulate_replan_decision(
        plan=plan,
        decision=decision,
        force_apply_all_low_risk=True,
        allow_high_risk=True,
    )

    assert len(not_applied.steps) == len(plan.steps)
    assert len(high_applied.steps) == len(plan.steps) + 1
    assert "simulation" in high_applied.metadata


def test_replan_report_writers_and_artifact_refs_are_safe(tmp_path: Path) -> None:
    output_dir = tmp_path / "outputs" / "runs" / "run_1"
    plan = build_default_ppt_plan(run_id="run_1")
    decision = DeterministicReplanner().propose(plan=plan, signals=_signals(pptx_exists=False))

    plan_refs = write_plan_graph(plan=plan, output_dir=output_dir)
    decision_refs = write_replan_decision(decision=decision, output_dir=output_dir)
    write_replan_report_markdown(plan=plan, decision=decision, output_path=output_dir / "replan_report.md")
    markdown = (output_dir / "replan_report.md").read_text(encoding="utf-8")
    serialized_refs = json.dumps({**plan_refs, **decision_refs}, ensure_ascii=False)

    assert (output_dir / "plan_graph.json").exists()
    assert (output_dir / "replan_decision.json").exists()
    assert "# Replan Report" in markdown
    assert "Insert Step Count" in markdown
    assert "Deduped Patch Count" in markdown
    assert "sk-secret123456789" not in markdown
    assert str(tmp_path) not in serialized_refs
    assert "plan_graph.json" in serialized_refs
    assert "replan_decision.json" in serialized_refs


def test_replan_report_markdown_can_include_signal_summary(tmp_path: Path) -> None:
    output_dir = tmp_path / "outputs" / "runs" / "run_1"
    plan = build_default_ppt_plan(run_id="run_1")
    signals = _signals(pptx_exists=False, repair_action_count=2, repair_auto_executable_action_count=1)
    decision = DeterministicReplanner().propose(plan=plan, signals=signals)

    write_replan_report_markdown(
        plan=plan,
        decision=decision,
        signals=signals,
        output_path=output_dir / "replan_report.md",
    )
    markdown = (output_dir / "replan_report.md").read_text(encoding="utf-8")

    assert "## Run Signals" in markdown
    assert "pptx_exists" in markdown
    assert "repair_auto_executable_action_count" in markdown


def test_replan_integration_from_artifacts_and_missing_artifacts(tmp_path: Path) -> None:
    run_dir = tmp_path / "outputs" / "runs" / "run_1"
    run_dir.mkdir(parents=True)
    (run_dir / "quality_report.json").write_text(json.dumps(_quality_report(), ensure_ascii=False), encoding="utf-8")
    (run_dir / "trace_summary.json").write_text(json.dumps(_trace_summary(), ensure_ascii=False), encoding="utf-8")
    (run_dir / "repair_plan.json").write_text(json.dumps(_repair_plan(), ensure_ascii=False), encoding="utf-8")

    plan, signals, decision = build_replan_decision_from_run_artifacts(run_id="run_1", run_dir=run_dir)
    refs = write_replan_artifacts_for_run(run_id="run_1", run_dir=run_dir, plan=plan, decision=decision)
    serialized_refs = json.dumps(refs, ensure_ascii=False)

    assert signals.quality_report_exists is True
    assert decision.status == "patch_proposed"
    assert (run_dir / "plan_graph.json").exists()
    assert "replan_report.md" in serialized_refs
    assert str(tmp_path) not in serialized_refs

    missing_plan, missing_signals, missing_decision = build_replan_decision_from_run_artifacts(run_id="missing", run_dir=tmp_path / "missing")
    assert missing_plan.steps
    assert missing_signals.missing_artifacts
    assert missing_decision.status == "no_change"
