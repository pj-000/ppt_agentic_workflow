from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.harness.repair.models import RepairPlan, RepairResult
from backend.harness.repair.safety import sanitize_repair_text


def write_repair_plan(
    plan: RepairPlan,
    output_dir: str | Path,
) -> dict[str, str]:
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    json_path = path / "repair_plan.json"
    json_path.write_text(json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"repair_plan_json": str(json_path)}


def write_repair_result(
    result: RepairResult,
    output_dir: str | Path,
) -> dict[str, str]:
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    json_path = path / "repair_result.json"
    json_path.write_text(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"repair_result_json": str(json_path)}


def write_repair_report_markdown(
    *,
    plan: RepairPlan,
    result: RepairResult | None,
    output_path: str | Path,
) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Repair Report",
        "",
        f"- Run ID: `{sanitize_repair_text(plan.run_id, limit=120)}`",
        f"- Plan ID: `{sanitize_repair_text(plan.plan_id, limit=120)}`",
        f"- Status: `{sanitize_repair_text((result.status if result else plan.status), limit=80)}`",
        f"- Issue Count: {len(plan.issues)}",
        f"- Action Count: {len(plan.actions)}",
        f"- Attempt Count: {_metadata_value(result, 'attempt_count')}",
        f"- Success Count: {_metadata_value(result, 'success_count')}",
        f"- Failed Count: {_metadata_value(result, 'failed_count')}",
        f"- Skipped Count: {_metadata_value(result, 'skipped_count')}",
        f"- Repair Success Rate: {_metadata_value(result, 'repair_success_rate')}",
        "",
        "## Issues",
        "",
    ]
    if plan.issues:
        lines.extend(["| issue_id | severity | scope | type | slide | message |", "| --- | --- | --- | --- | --- | --- |"])
        for issue in plan.issues:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _cell(issue.issue_id),
                        _cell(issue.severity.value),
                        _cell(issue.scope.value),
                        _cell(issue.issue_type),
                        _cell(issue.slide_index if issue.slide_index is not None else ""),
                        _cell(issue.message),
                    ]
                )
                + " |"
            )
    else:
        lines.append("(none)")

    lines.extend(["", "## Actions", ""])
    if plan.actions:
        lines.extend(["| action_id | type | scope | target | risk | auto_execute |", "| --- | --- | --- | --- | --- | --- |"])
        for action in plan.actions:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _cell(action.action_id),
                        _cell(action.action_type.value),
                        _cell(action.scope.value),
                        _cell(action.target_tool or action.target_slide_index or ""),
                        _cell(action.risk_level),
                        _cell(action.metadata.get("auto_execute", "")),
                    ]
                )
                + " |"
            )
    else:
        lines.append("(none)")

    lines.extend(["", "## Memory Hits", ""])
    if plan.memory_hits:
        lines.extend([f"- `{_cell(hit.get('memory_id', ''))}` score={_cell(hit.get('score', ''))}" for hit in plan.memory_hits])
    else:
        lines.append("(none)")

    lines.extend(["", "## Attempts", ""])
    if result and result.attempts:
        lines.extend(["| attempt_id | action_id | status | skip_reason | message |", "| --- | --- | --- | --- | --- |"])
        for attempt in result.attempts:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _cell(attempt.attempt_id),
                        _cell(attempt.action_id),
                        _cell(attempt.status),
                        _cell(attempt.metrics.get("skip_reason", "")),
                        _cell(attempt.message),
                    ]
                )
                + " |"
            )
    else:
        lines.append("(none)")

    lines.extend(["", "## Quality Delta", ""])
    delta = result.quality_delta if result else {}
    if delta:
        lines.extend([f"- {sanitize_repair_text(key, limit=120)}: `{sanitize_repair_text(value, limit=120)}`" for key, value in delta.items()])
    else:
        lines.append("(not available)")

    lines.extend(["", "## Notes", "", _notes(plan, result)])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _cell(value: Any) -> str:
    return sanitize_repair_text(value, limit=180).replace("|", "\\|").replace("\n", " ")


def _metadata_value(result: RepairResult | None, key: str) -> str:
    if result is None:
        return "n/a"
    value = result.metadata.get(key)
    return sanitize_repair_text("n/a" if value is None else value, limit=120)


def _notes(plan: RepairPlan, result: RepairResult | None) -> str:
    if result is None:
        return "Repair plan generated; execution result is not available."
    return f"Resolved {len(result.resolved_issue_ids)} issue(s); unresolved {len(result.unresolved_issue_ids)} issue(s)."
