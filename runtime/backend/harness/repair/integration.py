from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.harness.repair.issue_extractor import (
    extract_repair_issues_from_quality_report,
    extract_repair_issues_from_trace_summary,
)
from backend.harness.repair.models import RepairPlan, RepairResult, stable_repair_id, utc_now_iso
from backend.harness.repair.planner import RepairPlanner
from backend.harness.repair.policies import RepairPolicy
from backend.harness.repair.report import write_repair_plan, write_repair_report_markdown, write_repair_result
from backend.harness.repair.safety import sanitize_repair_mapping


def build_repair_plan_from_run_artifacts(
    *,
    run_id: str,
    run_dir: str | Path,
    policy: RepairPolicy | None = None,
    memory: Any | None = None,
    legacy_repair: Any | None = None,
    trace: Any | None = None,
) -> RepairPlan:
    active_policy = policy or RepairPolicy()
    run_path = Path(run_dir)
    quality, quality_error = _load_json_object(run_path / "quality_report.json")
    trace_summary, trace_error = _load_json_object(run_path / "trace_summary.json")
    issues = []
    if quality is not None:
        issues.extend(extract_repair_issues_from_quality_report(run_id=run_id, quality_report=quality, policy=active_policy))
    if trace_summary is not None:
        issues.extend(extract_repair_issues_from_trace_summary(run_id=run_id, trace_summary=trace_summary, policy=active_policy))
    missing = [item for item in (quality_error, trace_error) if item]
    if not issues:
        return RepairPlan(
            plan_id=stable_repair_id("plan", run_id, "empty", ",".join(missing)),
            run_id=run_id,
            status="empty" if quality is None and trace_summary is None else "skipped",
            issues=[],
            actions=[],
            created_at=utc_now_iso(),
            metadata=sanitize_repair_mapping({"missing_artifacts": missing}),
        )
    planner = RepairPlanner(policy=active_policy, memory=memory, legacy_repair=legacy_repair, trace=trace)
    return planner.plan(run_id=run_id, issues=issues, context={"run_dir": str(run_path), "missing_artifacts": missing})


def write_repair_artifacts_for_run(
    *,
    run_id: str,
    run_dir: str | Path,
    plan: RepairPlan,
    result: RepairResult | None = None,
) -> dict[str, str]:
    del run_id
    output_dir = Path(run_dir)
    artifact_refs = {}
    artifact_refs.update(write_repair_plan(plan, output_dir))
    if result is not None:
        artifact_refs.update(write_repair_result(result, output_dir))
    markdown_path = output_dir / "repair_report.md"
    write_repair_report_markdown(plan=plan, result=result, output_path=markdown_path)
    artifact_refs["repair_report_md"] = str(markdown_path)
    return artifact_refs


def _load_json_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, f"missing {path.name}"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, f"invalid {path.name}"
    if not isinstance(loaded, dict):
        return None, f"invalid {path.name}: expected object"
    return loaded, None
