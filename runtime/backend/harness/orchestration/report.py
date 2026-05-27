from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.harness.orchestration.models import PatchRiskLevel, PlanGraph, ReplanDecision
from backend.harness.orchestration.signals import RunSignals
from backend.harness.orchestration.safety import (
    sanitize_orchestration_artifacts,
    sanitize_orchestration_text,
)


def write_replan_decision(
    *,
    decision: ReplanDecision,
    output_dir: str | Path,
) -> dict[str, str]:
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    output_path = path / "replan_decision.json"
    output_path.write_text(
        json.dumps(decision.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return sanitize_orchestration_artifacts({"replan_decision_json": str(output_path)})


def write_plan_graph(
    *,
    plan: PlanGraph,
    output_dir: str | Path,
) -> dict[str, str]:
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    output_path = path / "plan_graph.json"
    output_path.write_text(json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return sanitize_orchestration_artifacts({"plan_graph_json": str(output_path)})


def write_replan_report_markdown(
    *,
    plan: PlanGraph,
    decision: ReplanDecision,
    output_path: str | Path,
    signals: RunSignals | None = None,
) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    low = sum(1 for patch in decision.patches if patch.risk_level == PatchRiskLevel.LOW)
    medium = sum(1 for patch in decision.patches if patch.risk_level == PatchRiskLevel.MEDIUM)
    high = sum(1 for patch in decision.patches if patch.risk_level == PatchRiskLevel.HIGH)
    insert_step_count = int(decision.metadata.get("insert_step_count") or 0)
    skip_step_count = int(decision.metadata.get("skip_step_count") or 0)
    manual_review_patch_count = int(decision.metadata.get("manual_review_patch_count") or 0)
    deduped_patch_count = int(decision.metadata.get("deduped_patch_count") or 0)
    lines = [
        "# Replan Report",
        "",
        f"- Run ID: `{_cell(decision.run_id)}`",
        f"- Plan ID: `{_cell(decision.plan_id)}`",
        f"- Decision Status: `{_cell(decision.status)}`",
        f"- Patch Count: {len(decision.patches)}",
        f"- Low Risk Patch Count: {low}",
        f"- Medium Risk Patch Count: {medium}",
        f"- High Risk Patch Count: {high}",
        f"- Insert Step Count: {insert_step_count}",
        f"- Skip Step Count: {skip_step_count}",
        f"- Manual Review Patch Count: {manual_review_patch_count}",
        f"- Deduped Patch Count: {deduped_patch_count}",
        "",
        "## Current Plan",
        "",
        "| step_id | type | status | agent | capability |",
        "| --- | --- | --- | --- | --- |",
    ]
    for step in plan.steps:
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(step.step_id),
                    _cell(step.step_type.value),
                    _cell(step.status.value),
                    _cell(step.agent_name or ""),
                    _cell(step.capability or ""),
                ]
            )
            + " |"
        )

    lines.extend(["", "## Proposed Patches", ""])
    if decision.patches:
        lines.extend(["| patch_id | action | risk | auto_apply | target | reason |", "| --- | --- | --- | --- | --- | --- |"])
        for patch in decision.patches:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _cell(patch.patch_id),
                        _cell(patch.action.value),
                        _cell(patch.risk_level.value),
                        _cell(patch.auto_apply),
                        _cell(patch.target_step_id or ""),
                        _cell(patch.reason),
                    ]
                )
                + " |"
            )
    else:
        lines.append("(none)")

    if signals is not None:
        lines.extend(
            [
                "",
                "## Run Signals",
                "",
                f"- pptx_exists: `{_cell(signals.pptx_exists)}`",
                f"- preview_success: `{_cell(signals.preview_success)}`",
                f"- visual_score_min: `{_cell(signals.visual_score_min)}`",
                f"- content_issue_count: `{_cell(signals.content_issue_count)}`",
                f"- failed_tool_count: `{signals.failed_tool_count}`",
                f"- skipped_tool_count: `{signals.skipped_tool_count}`",
                f"- timeout_tool_count: `{signals.timeout_tool_count}`",
                f"- repair_action_count: `{signals.repair_action_count}`",
                f"- repair_auto_executable_action_count: `{signals.repair_auto_executable_action_count}`",
            ]
        )

    lines.extend(["", "## Evidence", ""])
    if decision.evidence:
        lines.extend([f"- {sanitize_orchestration_text(key, limit=120)}: `{sanitize_orchestration_text(value, limit=240)}`" for key, value in decision.evidence.items()])
    else:
        lines.append("(none)")

    lines.extend(["", "## Notes", "", sanitize_orchestration_text(decision.summary, limit=1200)])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _cell(value: Any) -> str:
    return sanitize_orchestration_text(value, limit=220).replace("|", "\\|").replace("\n", " ")
