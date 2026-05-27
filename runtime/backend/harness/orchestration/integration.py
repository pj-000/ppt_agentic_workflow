from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.harness.orchestration.plan_graph import build_default_ppt_plan
from backend.harness.orchestration.policies import ReplannerPolicy
from backend.harness.orchestration.replanner import DeterministicReplanner
from backend.harness.orchestration.report import (
    write_plan_graph,
    write_replan_decision,
    write_replan_report_markdown,
)
from backend.harness.orchestration.safety import sanitize_orchestration_artifacts
from backend.harness.orchestration.signals import RunSignals, extract_run_signals_from_artifacts
from backend.harness.orchestration.models import PlanGraph, ReplanDecision


def build_replan_decision_from_run_artifacts(
    *,
    run_id: str,
    run_dir: str | Path,
    policy: ReplannerPolicy | None = None,
    trace: Any | None = None,
) -> tuple[PlanGraph, RunSignals, ReplanDecision]:
    plan = build_default_ppt_plan(run_id=run_id)
    signals = extract_run_signals_from_artifacts(run_id=run_id, run_dir=run_dir)
    decision = DeterministicReplanner(policy=policy, trace=trace).propose(plan=plan, signals=signals)
    return plan, signals, decision


def write_replan_artifacts_for_run(
    *,
    run_id: str,
    run_dir: str | Path,
    plan: PlanGraph,
    decision: ReplanDecision,
) -> dict[str, str]:
    del run_id
    output_dir = Path(run_dir)
    refs = {}
    refs.update(write_plan_graph(plan=plan, output_dir=output_dir))
    refs.update(write_replan_decision(decision=decision, output_dir=output_dir))
    markdown_path = output_dir / "replan_report.md"
    write_replan_report_markdown(plan=plan, decision=decision, output_path=markdown_path)
    refs["replan_report_md"] = str(markdown_path)
    return sanitize_orchestration_artifacts(refs)
