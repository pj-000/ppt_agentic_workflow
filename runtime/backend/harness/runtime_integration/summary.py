from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.harness.runtime_integration.models import HarnessArtifactRef, HarnessBundleResult
from backend.harness.runtime_integration.safety import sanitize_runtime_text


def write_harness_summary_markdown(
    *,
    result: HarnessBundleResult,
    output_path: str | Path,
) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = result.manifest
    required = [artifact for artifact in manifest.artifacts if artifact.required]
    optional = [artifact for artifact in manifest.artifacts if not artifact.required]
    lines = [
        "# Harness Summary",
        "",
        f"- Run ID: `{_cell(result.run_id)}`",
        f"- Status: `{_cell(result.status)}`",
        f"- Quality Status: `{_cell(manifest.quality_status or '')}`",
        f"- Trace Status: `{_cell(manifest.trace_status or '')}`",
        f"- Repair Plan Status: `{_cell(manifest.repair_plan_status or '')}`",
        f"- Replan Status: `{_cell(manifest.replan_status or '')}`",
        f"- Benchmark Status: `{_cell(manifest.benchmark_status or '')}`",
        f"- Memory Writes: {len(result.memory_write_ids)}",
        "",
        "## Status Reason",
        "",
    ]
    lines.extend(_status_reasons(result))
    lines.extend(
        [
            "",
            "## Required Artifacts",
            "",
        ]
    )
    lines.extend(_artifact_table(required))
    lines.extend(["", "## Optional Artifacts", ""])
    lines.extend(_artifact_table(optional))
    lines.extend(["", "## Generated Artifacts", ""])
    if manifest.generated_artifacts:
        for name, value in manifest.generated_artifacts.items():
            lines.append(f"- {_cell(name)}: `{_cell(value)}` (exists: {_generated_ref_exists(manifest.artifacts, value)})")
    else:
        lines.append("(none)")
    lines.extend(["", "## Missing Artifacts", ""])
    missing = [*manifest.missing_required_artifacts, *manifest.missing_optional_artifacts]
    if missing:
        lines.extend(f"- {_cell(item)}" for item in missing)
    else:
        lines.append("(none)")
    lines.extend(["", "## Errors", ""])
    if result.errors:
        lines.extend(f"- {_cell(error)}" for error in result.errors)
    else:
        lines.append("(none)")
    lines.extend(["", "## Next Suggested Actions", ""])
    lines.extend(_next_actions(result))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _artifact_table(artifacts: list[HarnessArtifactRef]) -> list[str]:
    lines = ["| name | kind | exists | path |", "| --- | --- | --- | --- |"]
    if not artifacts:
        lines.append("| (none) |  |  |  |")
        return lines
    for artifact in artifacts:
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(artifact.name),
                    _cell(artifact.kind.value),
                    _cell(artifact.exists),
                    _cell(artifact.path),
                ]
            )
            + " |"
        )
    return lines


def _next_actions(result: HarnessBundleResult) -> list[str]:
    actions = []
    if result.repair_plan_id:
        actions.append("Review `repair_plan.json`.")
    if result.replan_decision_id:
        actions.append("Review `replan_decision.json`.")
    if result.manifest.missing_required_artifacts:
        actions.append("Re-run generation or inspect trace artifacts.")
    if result.memory_write_ids:
        actions.append("Inspect episodic memory record.")
    return actions or ["No immediate follow-up action."]


def _status_reasons(result: HarnessBundleResult) -> list[str]:
    manifest = result.manifest
    reasons = list(manifest.metadata.get("status_reasons", []))
    if not reasons:
        if manifest.missing_required_artifacts:
            reasons.append(f"Missing required artifacts: {', '.join(manifest.missing_required_artifacts)}")
        if result.errors:
            reasons.append(f"{len(result.errors)} integration warning/error(s).")
        if manifest.repair_plan_status in {"planned", "created"}:
            reasons.append(f"Repair plan status is {manifest.repair_plan_status}.")
        if manifest.replan_status == "patch_proposed":
            reasons.append("Replanner proposed deterministic patches.")
        if manifest.benchmark_status == "fail":
            reasons.append("Optional one-run benchmark failed.")
    return [f"- {_cell(reason)}" for reason in (reasons or ["No warning or failure reason."])]


def _generated_ref_exists(artifacts: list[HarnessArtifactRef], value: str) -> bool:
    return any(artifact.path == value and artifact.exists for artifact in artifacts)


def _cell(value: Any) -> str:
    return sanitize_runtime_text(value, limit=260).replace("|", "\\|").replace("\n", " ")
