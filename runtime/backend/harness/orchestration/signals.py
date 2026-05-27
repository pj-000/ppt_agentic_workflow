from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from backend.harness.orchestration.safety import (
    sanitize_orchestration_artifacts,
    sanitize_orchestration_mapping,
    sanitize_orchestration_path,
    sanitize_orchestration_text,
)


class RunSignals(BaseModel):
    run_id: str

    quality_report_exists: bool = False
    trace_summary_exists: bool = False
    repair_plan_exists: bool = False
    repair_result_exists: bool = False

    pptx_exists: bool | None = None
    preview_success: bool | None = None
    visual_score_min: float | None = None
    visual_score_avg: float | None = None
    content_issue_count: int | None = None

    trace_status: str | None = None
    failed_tool_count: int = 0
    skipped_tool_count: int = 0
    timeout_tool_count: int = 0
    error_signatures: list[str] = Field(default_factory=list)

    repair_issue_count: int = 0
    repair_action_count: int = 0
    repair_auto_executable_action_count: int = 0
    repair_non_auto_action_count: int = 0
    repair_attempt_count: int = 0
    repair_success_rate: float | None = None

    missing_artifacts: list[str] = Field(default_factory=list)
    missing_reasons: dict[str, str] = Field(default_factory=dict)

    artifact_refs: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("run_id")
    @classmethod
    def _safe_run_id(cls, value: str) -> str:
        text = sanitize_orchestration_text(value, limit=160)
        if not text:
            raise ValueError("run_id must not be empty")
        return text

    @field_validator("error_signatures", "missing_artifacts", mode="before")
    @classmethod
    def _safe_list(cls, value: Any) -> list[str]:
        if not isinstance(value, list | tuple | set):
            return []
        return [sanitize_orchestration_text(item, limit=240) for item in value]

    @field_validator("missing_reasons", "metadata", mode="before")
    @classmethod
    def _safe_mapping(cls, value: Any) -> dict[str, Any]:
        return sanitize_orchestration_mapping(value)

    @field_validator("artifact_refs", mode="before")
    @classmethod
    def _safe_artifacts(cls, value: Any) -> dict[str, str]:
        return sanitize_orchestration_artifacts(value)


def extract_run_signals_from_artifacts(
    *,
    run_id: str,
    run_dir: str | Path,
) -> RunSignals:
    path = Path(run_dir)
    missing_artifacts: list[str] = []
    missing_reasons: dict[str, str] = {}
    artifact_refs: dict[str, str] = {}

    quality, quality_error = _load_json_object(path / "quality_report.json")
    trace, trace_error = _load_json_object(path / "trace_summary.json")
    repair_plan, repair_plan_error = _load_json_object(path / "repair_plan.json")
    repair_result, repair_result_error = _load_json_object(path / "repair_result.json")

    optional_missing_artifacts: list[str] = []
    invalid_optional_artifacts: dict[str, str] = {}
    for name, loaded, error, required in (
        ("quality_report.json", quality, quality_error, True),
        ("trace_summary.json", trace, trace_error, True),
        ("repair_plan.json", repair_plan, repair_plan_error, False),
        ("repair_result.json", repair_result, repair_result_error, False),
    ):
        if loaded is None:
            if required:
                missing_artifacts.append(name)
            elif error and error.startswith("missing "):
                optional_missing_artifacts.append(name)
            elif error:
                invalid_optional_artifacts[name] = error
            if error and required:
                missing_reasons[name] = error
        else:
            artifact_refs[name] = str(path / name)

    quality_run = _dict_value(quality, "run")
    quality_missing = _dict_value(quality, "missing_reasons")
    trace_artifacts = _dict_value(trace, "artifact_refs")
    repair_plan_actions = repair_plan.get("actions", []) if isinstance(repair_plan, dict) else []
    repair_plan_issues = repair_plan.get("issues", []) if isinstance(repair_plan, dict) else []
    repair_auto_executable_action_count = _repair_auto_executable_action_count(repair_plan_actions)
    repair_action_count = len(repair_plan_actions) if isinstance(repair_plan_actions, list) else 0
    repair_result_metadata = _dict_value(repair_result, "metadata")

    missing_reasons.update({str(key): str(value) for key, value in quality_missing.items()})
    artifact_refs.update({str(key): str(value) for key, value in trace_artifacts.items()})

    return RunSignals(
        run_id=run_id,
        quality_report_exists=quality is not None,
        trace_summary_exists=trace is not None,
        repair_plan_exists=repair_plan is not None,
        repair_result_exists=repair_result is not None,
        pptx_exists=_optional_bool(quality_run.get("pptx_exists")),
        preview_success=_optional_bool(quality_run.get("preview_success")),
        visual_score_min=_optional_float(quality_run.get("visual_score_min")),
        visual_score_avg=_optional_float(quality_run.get("visual_score_avg")),
        content_issue_count=_optional_int(quality_run.get("content_issue_count")),
        trace_status=str(trace.get("status")) if isinstance(trace, dict) and trace.get("status") is not None else None,
        failed_tool_count=_optional_int(trace.get("failed_tool_count") if isinstance(trace, dict) else None) or 0,
        skipped_tool_count=_optional_int(trace.get("skipped_tool_count") if isinstance(trace, dict) else None) or 0,
        timeout_tool_count=_optional_int(trace.get("timeout_tool_count") if isinstance(trace, dict) else None) or 0,
        error_signatures=trace.get("error_signatures", []) if isinstance(trace, dict) else [],
        repair_issue_count=len(repair_plan_issues) if isinstance(repair_plan_issues, list) else 0,
        repair_action_count=repair_action_count,
        repair_auto_executable_action_count=repair_auto_executable_action_count,
        repair_non_auto_action_count=max(repair_action_count - repair_auto_executable_action_count, 0),
        repair_attempt_count=_repair_attempt_count(repair_result, repair_result_metadata),
        repair_success_rate=_optional_float(repair_result_metadata.get("repair_success_rate")),
        missing_artifacts=missing_artifacts,
        missing_reasons=missing_reasons,
        artifact_refs=artifact_refs,
        metadata={
            "run_dir": sanitize_orchestration_path(path),
            "repair_result_status": repair_result.get("status") if isinstance(repair_result, dict) else "",
            "optional_missing_artifacts": optional_missing_artifacts,
            "invalid_optional_artifacts": invalid_optional_artifacts,
        },
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
    if not isinstance(value, dict):
        return {}
    item = value.get(key, {})
    return item if isinstance(item, dict) else {}


def _repair_attempt_count(repair_result: dict[str, Any] | None, metadata: dict[str, Any]) -> int:
    count = _optional_int(metadata.get("attempt_count"))
    if count is not None:
        return count
    attempts = repair_result.get("attempts", []) if isinstance(repair_result, dict) else []
    return len(attempts) if isinstance(attempts, list) else 0


def _repair_auto_executable_action_count(actions: Any) -> int:
    if not isinstance(actions, list):
        return 0
    count = 0
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("action_type") or "")
        metadata = action.get("metadata", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        if metadata.get("auto_execute") is False:
            continue
        if action_type in {"no_op", "manual_review"}:
            continue
        count += 1
    return count


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
