from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from backend.harness.memory.models import MemoryRecord, MemoryType
from backend.harness.memory.namespace import ORCHESTRATOR_EPISODE, validate_namespace
from backend.harness.memory.safety import sanitize_memory_mapping, sanitize_memory_text
from backend.harness.memory.store import utc_now_iso


def build_episode_memory_from_run_artifacts(
    *,
    run_id: str,
    run_dir: str | Path,
    namespace: str = ORCHESTRATOR_EPISODE,
) -> MemoryRecord:
    safe_namespace = validate_namespace(namespace)
    run_path = Path(run_dir)
    quality_path = run_path / "quality_report.json"
    trace_path = run_path / "trace_summary.json"
    quality, quality_error = _load_json_object(quality_path)
    trace, trace_error = _load_json_object(trace_path)

    missing: list[str] = []
    if quality_error:
        missing.append(quality_error)
    if trace_error:
        missing.append(trace_error)

    quality_run = _dict_value(quality, "run")
    quality_summary = _dict_value(quality, "summary")
    missing_reasons = _dict_value(quality, "missing_reasons")

    quality_context = {
        "topic": quality_run.get("topic"),
        "slide_count": quality_run.get("slide_count"),
        "pptx_exists": quality_run.get("pptx_exists"),
        "preview_success": quality_run.get("preview_success"),
        "visual_score_avg": quality_run.get("visual_score_avg"),
        "visual_score_min": quality_run.get("visual_score_min"),
        "content_issue_count": quality_run.get("content_issue_count"),
        "repair_attempt_count": quality_run.get("repair_attempt_count"),
        "status": quality_summary.get("status"),
        "missing_reasons": missing_reasons,
    }
    trace_context = {
        "status": trace.get("status") if trace else None,
        "tool_call_count": trace.get("tool_call_count") if trace else None,
        "tool_attempt_count": trace.get("tool_attempt_count") if trace else None,
        "failed_tool_count": trace.get("failed_tool_count") if trace else None,
        "skipped_tool_count": trace.get("skipped_tool_count") if trace else None,
        "timeout_tool_count": trace.get("timeout_tool_count") if trace else None,
        "error_signatures": trace.get("error_signatures", []) if trace else [],
    }
    status = _episode_status(quality_summary=quality_summary, trace=trace)
    context = sanitize_memory_mapping(
        {
            "quality": quality_context,
            "trace": trace_context,
            "missing": missing,
        }
    )
    outcome = sanitize_memory_mapping(
        {
            "status": status,
            "quality_status": quality_summary.get("status"),
            "trace_status": trace.get("status") if trace else None,
            "slide_count": quality_run.get("slide_count"),
            "visual_score_avg": quality_run.get("visual_score_avg"),
            "visual_score_min": quality_run.get("visual_score_min"),
            "content_issue_count": quality_run.get("content_issue_count"),
            "repair_attempt_count": quality_run.get("repair_attempt_count"),
            "failed_tool_count": trace.get("failed_tool_count") if trace else None,
            "skipped_tool_count": trace.get("skipped_tool_count") if trace else None,
            "timeout_tool_count": trace.get("timeout_tool_count") if trace else None,
        }
    )
    topic = quality_run.get("topic") or ""
    error_signatures = trace_context["error_signatures"] or []
    content = sanitize_memory_text(
        " ".join(
            str(part)
            for part in [
                f"Run {run_id}:",
                f"topic={topic}" if topic else "",
                f"slides={quality_run.get('slide_count')}",
                f"quality={quality_summary.get('status')}",
                f"trace={trace.get('status') if trace else None}",
                (
                    "tools failed/skipped/timeout="
                    f"{trace_context['failed_tool_count']}/"
                    f"{trace_context['skipped_tool_count']}/"
                    f"{trace_context['timeout_tool_count']}"
                ),
                f"errors={', '.join(error_signatures[:5])}" if error_signatures else "",
            ]
            if part
        ),
        limit=1000,
    )
    source_artifacts = {}
    if quality_path.exists():
        source_artifacts["quality_report"] = str(quality_path)
    if trace_path.exists():
        source_artifacts["trace_summary"] = str(trace_path)

    tags = ["run", "episode", status]
    if error_signatures or any((trace_context["failed_tool_count"], trace_context["timeout_tool_count"])):
        tags.append("tool_failure")
    if missing:
        tags.append("missing_artifacts")

    now = utc_now_iso()
    return MemoryRecord(
        memory_id=_stable_memory_id("episode", safe_namespace, run_id),
        namespace=safe_namespace,
        memory_type=MemoryType.EPISODIC,
        key=str(run_id),
        content=content,
        context=context,
        outcome=outcome,
        tags=tags,
        confidence=0.6 if status in {"success", "warning"} else 0.4,
        source_run_id=str(run_id),
        source_artifacts=source_artifacts,
        created_at=now,
        updated_at=now,
    )


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


def _dict_value(value: dict[str, Any] | None, key: str) -> dict[str, Any]:
    if not value:
        return {}
    item = value.get(key, {})
    return item if isinstance(item, dict) else {}


def _episode_status(*, quality_summary: dict[str, Any], trace: dict[str, Any] | None) -> str:
    quality_status = str(quality_summary.get("status") or "").lower()
    trace_status = str((trace or {}).get("status") or "").lower()
    if quality_status == "failed" or trace_status == "failed":
        return "failed"
    if quality_status == "warning" or trace_status == "warning":
        return "warning"
    if trace and any((trace.get("failed_tool_count", 0), trace.get("timeout_tool_count", 0))):
        return "warning"
    if quality_status == "success" or trace_status == "success":
        return "success"
    return quality_status or trace_status or "unknown"


def _stable_memory_id(*parts: str) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{parts[0]}_{digest}"
