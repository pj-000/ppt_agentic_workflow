from __future__ import annotations

from pathlib import Path
from typing import Any


def write_trace_summary_markdown(summary: dict[str, Any], output_path: str | Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Trace Summary",
        "",
        f"- Run ID: {_display(summary.get('run_id'))}",
        f"- Status: {_display(summary.get('status'))}",
        f"- Total Events: {_display(summary.get('total_events'))}",
        f"- Phase Count: {_display(summary.get('phase_count'))}",
        f"- Tool Call Count: {_display(summary.get('tool_call_count'))}",
        f"- Failed Tool Count: {_display(summary.get('failed_tool_count'))}",
        f"- Skipped Tool Count: {_display(summary.get('skipped_tool_count'))}",
        f"- Timeout Tool Count: {_display(summary.get('timeout_tool_count'))}",
        "",
        "## Error Signatures",
        "",
    ]
    error_signatures = summary.get("error_signatures") or []
    if error_signatures:
        lines.extend(f"- {item}" for item in error_signatures)
    else:
        lines.append("- None")

    lines.extend(["", "## Artifacts", ""])
    artifacts = summary.get("artifact_refs") or {}
    if artifacts:
        lines.extend(f"- {key}: {value}" for key, value in sorted(artifacts.items()))
    else:
        lines.append("- None")

    lines.extend(["", "## Quality Reports", ""])
    quality_paths = summary.get("quality_report_paths") or []
    if quality_paths:
        lines.extend(f"- {item}" for item in quality_paths)
    else:
        lines.append("- None")

    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _display(value: Any) -> str:
    return "n/a" if value is None else str(value)
