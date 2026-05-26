from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from backend.harness.memory import AgentMemory, JsonlMemoryStore, MemoryRecord, MemoryType  # noqa: E402
from backend.harness.memory.namespace import REPAIR_VISUAL  # noqa: E402
from backend.harness.observability import ObservabilityTraceAdapter, TraceStore  # noqa: E402
from backend.harness.repair import (  # noqa: E402
    LegacyRepairOrchestratorAdapter,
    RepairAction,
    RepairActionType,
    RepairAttempt,
    RepairExecutor,
    RepairIssue,
    RepairPlan,
    RepairPlanner,
    RepairPolicy,
    RepairResult,
    RepairScope,
    RepairSeverity,
    RepairSource,
    build_repair_plan_from_run_artifacts,
    compute_quality_delta,
    extract_repair_issues_from_quality_report,
    extract_repair_issues_from_tool_error,
    extract_repair_issues_from_trace_summary,
    write_repair_artifacts_for_run,
)
from backend.harness.repair.models import stable_repair_id, utc_now_iso  # noqa: E402
from backend.harness.repair.report import write_repair_plan, write_repair_report_markdown, write_repair_result  # noqa: E402


def _quality_report() -> dict[str, Any]:
    return {
        "run": {
            "topic": "人工智能导论",
            "slide_count": 8,
            "visual_score_avg": 3.2,
            "visual_score_min": 2.8,
            "content_issue_count": 8,
            "preview_success": False,
            "pptx_exists": False,
        },
        "slides": [{"slide_index": 2, "title": "核心概念", "visual_score": 2.8}],
        "issues": [
            {
                "type": "content_claim_missing_evidence",
                "severity": "warning",
                "scope": "content",
                "slide_index": 3,
                "message": "Need citation for Transformer claim.",
                "metrics": {"count": 1},
            }
        ],
        "missing_reasons": {"tool_errors": "ToolRuntime not implemented yet"},
    }


def _trace_summary() -> dict[str, Any]:
    return {
        "run_id": "run_1",
        "status": "warning",
        "failed_tool_count": 1,
        "skipped_tool_count": 1,
        "timeout_tool_count": 1,
        "error_signatures": [
            "ppt.render_preview:PreviewGenerationFailed:no_images",
            "search.web_text:TimeoutError:provider_unavailable",
        ],
        "artifact_refs": {
            "quality_report": "/private/tmp/project/outputs/runs/run_1/quality_report.json",
        },
    }


def _issue(
    issue_type: str = "visual_score_below_threshold",
    *,
    scope: RepairScope = RepairScope.VISUAL,
    error_signature: str | None = "visual.low_score",
    tool_name: str | None = None,
    message: str = "Visual score is low",
) -> RepairIssue:
    return RepairIssue(
        issue_id=stable_repair_id("issue", "run_1", issue_type, scope.value, error_signature),
        run_id="run_1",
        source=RepairSource.QUALITY,
        scope=scope,
        severity=RepairSeverity.WARNING,
        trigger_stage="visual_qa",
        issue_type=issue_type,
        slide_index=1 if scope == RepairScope.VISUAL else None,
        tool_name=tool_name,
        error_signature=error_signature,
        message=message,
    )


def _plan(actions: list[RepairAction]) -> RepairPlan:
    return RepairPlan(
        plan_id="plan_1",
        run_id="run_1",
        status="planned" if actions else "empty",
        issues=[_issue()],
        actions=actions,
        created_at=utc_now_iso(),
    )


def _action(action_type: RepairActionType = RepairActionType.ADJUST_LAYOUT, issue_id: str = "issue_1") -> RepairAction:
    return RepairAction(
        action_id=stable_repair_id("action", action_type.value, issue_id),
        issue_id=issue_id,
        action_type=action_type,
        scope=RepairScope.VISUAL,
        instruction="Adjust layout",
    )


def test_repair_models_serialize_and_sanitize_sensitive_fields() -> None:
    issue = RepairIssue(
        issue_id="issue_1",
        run_id="run_1",
        source=RepairSource.MANUAL,
        message="api_key=sk-secret123456789 system_prompt=private",
        evidence={"authorization": "Bearer sk-secret123456789", "path": "/private/tmp/file.py"},
    )
    action = RepairAction(
        action_id="action_1",
        issue_id="issue_1",
        action_type=RepairActionType.MANUAL_REVIEW,
        scope=RepairScope.UNKNOWN,
        instruction="hidden_reasoning=private",
        metadata={"raw_model_response": "secret"},
    )
    plan = RepairPlan(
        plan_id="plan_1",
        run_id="run_1",
        status="planned",
        issues=[issue],
        actions=[action],
        created_at=utc_now_iso(),
        metadata={"token": "sk-secret123456789"},
    )
    result = RepairResult(run_id="run_1", plan_id="plan_1", status="success", metadata={"password": "secret"})
    serialized = plan.model_dump_json() + result.model_dump_json()

    assert RepairIssue.model_validate_json(issue.model_dump_json()).issue_id == "issue_1"
    assert RepairPlan.model_validate_json(plan.model_dump_json()).actions[0].action_type == RepairActionType.MANUAL_REVIEW
    assert RepairResult.model_validate_json(result.model_dump_json()).status == "success"
    assert "sk-secret123456789" not in serialized
    assert "system_prompt=private" not in serialized
    assert "hidden_reasoning=private" not in serialized


def test_quality_report_issue_extraction_extracts_core_issues() -> None:
    issues = extract_repair_issues_from_quality_report(run_id="run_1", quality_report=_quality_report(), policy=RepairPolicy())
    types = {issue.issue_type for issue in issues}

    assert "visual_score_below_threshold" in types
    assert "content_issue_count_exceeded" in types
    assert "preview_failed" in types
    assert "pptx_missing" in types
    assert "content_claim_missing_evidence" in types
    assert "missing_metric" in types
    assert next(issue for issue in issues if issue.issue_type == "visual_score_below_threshold").slide_index == 2
    assert next(issue for issue in issues if issue.issue_type == "pptx_missing").severity == RepairSeverity.CRITICAL


def test_trace_summary_and_tool_error_issue_extraction() -> None:
    issues = extract_repair_issues_from_trace_summary(run_id="run_1", trace_summary=_trace_summary(), policy=RepairPolicy())
    types = {issue.issue_type for issue in issues}
    signatures = {issue.error_signature for issue in issues}
    tool_issue = extract_repair_issues_from_tool_error(
        run_id="run_1",
        tool_error={
            "tool": "ppt.render_preview",
            "status": "timeout",
            "error_type": "TimeoutError",
            "error_signature": "ppt.render_preview:TimeoutError:libreoffice_timeout",
            "message": "timed out",
        },
    )

    assert {"tool_failed", "tool_skipped", "tool_timeout", "trace_error_signature"}.issubset(types)
    assert "ppt.render_preview:PreviewGenerationFailed:no_images" in signatures
    assert tool_issue.issue_type == "tool_timeout"
    assert tool_issue.tool_name == "ppt.render_preview"
    assert extract_repair_issues_from_trace_summary(run_id="run_1", trace_summary={}, policy=RepairPolicy()) == []


def test_legacy_adapter_calls_fake_and_sanitizes_failures() -> None:
    class FakeLegacy:
        def __init__(self) -> None:
            self.called: list[str] = []

        def classify_error(self, error: str, *, stage: str, image_path: str | None = None) -> str:
            self.called.append("classify")
            return f"signature api_key=sk-secret123456789 {stage}"

        def build_repair_instruction(self, **kwargs: Any) -> str:
            self.called.append("instruction")
            return "Repair using safe layout. system_prompt=private"

        def prevention_section(self, **kwargs: Any) -> str:
            return "Prevention token=sk-secret123456789"

        def repair_section(self, **kwargs: Any) -> str:
            return "Repair section"

    adapter = LegacyRepairOrchestratorAdapter(FakeLegacy())

    assert adapter.classify_error(error="bad", stage="visual_qa")
    assert "sk-secret123456789" not in adapter.classify_error(error="bad", stage="visual_qa")
    assert "system_prompt=private" not in adapter.build_repair_instruction(error_signature="x", error="bad")
    assert "sk-secret123456789" not in adapter.prevention_section(trigger_stage="visual_qa")

    class BrokenLegacy:
        def classify_error(self, **kwargs: Any) -> str:
            raise RuntimeError("boom api_key=sk-secret123456789")

    assert LegacyRepairOrchestratorAdapter(BrokenLegacy()).classify_error(error="bad", stage="visual_qa") == "generic_retry"


def test_repair_planner_maps_issue_types_to_actions_and_policy_limits() -> None:
    issues = [
        _issue("visual_score_below_threshold", scope=RepairScope.VISUAL, error_signature="visual.low_score"),
        _issue("preview_failed", scope=RepairScope.TOOL, error_signature="ppt.render_preview:PreviewGenerationFailed", tool_name="ppt.render_preview"),
        _issue("preview_dependency", scope=RepairScope.TOOL, error_signature="ppt.render_preview:DependencyMissing:soffice_not_found"),
        _issue("content_issue_count_exceeded", scope=RepairScope.CONTENT, error_signature="content.issue_count_exceeded"),
        _issue("pptx_missing", scope=RepairScope.TOOL, error_signature="ppt.run_pptxgenjs:PptxArtifactMissing", tool_name="ppt.run_pptxgenjs"),
        _issue("image_generation_failed", scope=RepairScope.ASSET, error_signature="asset.image_failed"),
        _issue("unknown_issue", scope=RepairScope.UNKNOWN, error_signature=None),
    ]
    plan = RepairPlanner(policy=RepairPolicy(max_total_actions=10, max_actions_per_issue=1)).plan(run_id="run_1", issues=issues)
    action_types = [action.action_type for action in plan.actions]

    assert RepairActionType.ADJUST_LAYOUT in action_types
    assert RepairActionType.RERENDER_PREVIEW in action_types
    assert RepairActionType.MANUAL_REVIEW in action_types
    assert RepairActionType.CONTENT_REWRITE in action_types
    assert RepairActionType.RETRY_TOOL in action_types
    assert RepairActionType.FALLBACK_NO_IMAGE in action_types
    content_action = next(action for action in plan.actions if action.action_type == RepairActionType.CONTENT_REWRITE)
    assert content_action.metadata["auto_execute"] is False
    assert len(RepairPlanner(policy=RepairPolicy(max_total_actions=2)).plan(run_id="run_1", issues=issues).actions) == 2


def test_repair_planner_integrates_memory_legacy_and_trace(tmp_path: Path) -> None:
    class FakeTrace:
        def __init__(self) -> None:
            self.records: list[tuple[str, dict[str, Any]]] = []

        def record(self, stage: str, payload: dict[str, Any]) -> None:
            self.records.append((stage, payload))

    class FakeLegacy:
        def build_repair_instruction(self, **kwargs: Any) -> str:
            return "Legacy repair instruction"

        def prevention_section(self, **kwargs: Any) -> str:
            return "Legacy prevention"

        def repair_section(self, **kwargs: Any) -> str:
            return "Legacy repair section"

    memory = AgentMemory(JsonlMemoryStore(tmp_path / "memory"))
    long_content = ("Use a safer layout with stronger contrast. " * 5) + ("FULL_CONTENT_SHOULD_NOT_APPEAR " * 20)
    memory.write(
        MemoryRecord(
            memory_id="mem_visual",
            namespace=REPAIR_VISUAL,
            memory_type=MemoryType.PROCEDURAL,
            key="visual.low_score",
            content=long_content,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
        )
    )
    trace = FakeTrace()
    plan = RepairPlanner(memory=memory, legacy_repair=FakeLegacy(), trace=trace).plan(run_id="run_1", issues=[_issue()])
    serialized = plan.model_dump_json()

    assert plan.actions[0].memory_refs == ["mem_visual"]
    assert plan.memory_hits[0]["memory_id"] == "mem_visual"
    assert "FULL_CONTENT_SHOULD_NOT_APPEAR" not in serialized
    assert "Legacy repair instruction" in plan.actions[0].instruction
    assert {"repair.started", "repair.finished"}.issubset({stage for stage, _ in trace.records})


def test_repair_planner_trace_failure_is_best_effort() -> None:
    class BrokenTrace:
        def record(self, stage: str, payload: dict[str, Any]) -> None:
            raise RuntimeError("trace down")

    plan = RepairPlanner(trace=BrokenTrace()).plan(run_id="run_1", issues=[_issue()])

    assert plan.actions


def test_repair_executor_skips_successes_failures_and_summarizes() -> None:
    success_action = _action(RepairActionType.ADJUST_LAYOUT, issue_id="issue_success")
    skipped_action = _action(RepairActionType.MANUAL_REVIEW, issue_id="issue_skipped")
    failed_action = _action(RepairActionType.CONTENT_REWRITE, issue_id="issue_failed")

    def success_handler(action: RepairAction) -> RepairAttempt:
        return RepairAttempt(
            attempt_id="attempt_success",
            plan_id="will_normalize",
            action_id=action.action_id,
            issue_id=action.issue_id,
            run_id="run_1",
            status="success",
            message="ok",
        )

    def failing_handler(action: RepairAction) -> RepairAttempt:
        raise RuntimeError("api_key=sk-secret123456789 failed")

    executor = RepairExecutor(
        handlers={
            RepairActionType.ADJUST_LAYOUT: success_handler,
            RepairActionType.CONTENT_REWRITE: failing_handler,
        }
    )
    partial = executor.execute_plan(_plan([success_action, skipped_action, failed_action]))
    success = executor.execute_plan(_plan([success_action]))
    skipped = RepairExecutor().execute_plan(_plan([skipped_action]))
    failed = executor.execute_plan(_plan([failed_action]))
    empty = RepairExecutor().execute_plan(_plan([]))

    assert partial.status == "partial"
    assert success.status == "success"
    assert skipped.status == "skipped"
    assert failed.status == "failed"
    assert empty.status == "not_executed"
    assert all("sk-secret123456789" not in attempt.model_dump_json() for attempt in partial.attempts)


def test_repair_executor_records_trace_and_trace_failure_is_best_effort() -> None:
    trace_store = TraceStore(Path("/tmp") / "repair_trace_test")
    trace = ObservabilityTraceAdapter(run_id="run_1", trace_store=trace_store)
    action = _action()

    result = RepairExecutor(trace=trace).execute_plan(_plan([action]))

    assert result.status == "skipped"
    assert {"repair.started", "repair.finished"}.issubset({event.event_type for event in trace_store.load("run_1")})

    class BrokenTrace:
        def record(self, stage: str, payload: dict[str, Any]) -> None:
            raise RuntimeError("trace down")

    assert RepairExecutor(trace=BrokenTrace()).execute_plan(_plan([action])).status == "skipped"


def test_compute_quality_delta_handles_present_and_missing_fields() -> None:
    delta = compute_quality_delta(
        before={"run": {"visual_score_avg": 3.0, "visual_score_min": 2.5, "content_issue_count": 5, "preview_success": False}},
        after={"run": {"visual_score_avg": 4.0, "visual_score_min": 3.5, "content_issue_count": 2, "preview_success": True}},
    )

    assert delta["visual_score_avg_delta"] == 1.0
    assert delta["visual_score_min_delta"] == 1.0
    assert delta["content_issue_count_delta"] == -3.0
    assert delta["preview_success_changed"] is True
    assert compute_quality_delta(before={}, after={}) == {}


def test_repair_report_writers_create_json_and_markdown_without_sensitive_text(tmp_path: Path) -> None:
    plan = RepairPlan(
        plan_id="plan_1",
        run_id="run_1",
        status="planned",
        issues=[_issue(message="api_key=sk-secret123456789")],
        actions=[_action()],
        created_at=utc_now_iso(),
    )
    result = RepairResult(
        run_id="run_1",
        plan_id="plan_1",
        status="success",
        attempts=[
            RepairAttempt(
                attempt_id="attempt_1",
                plan_id="plan_1",
                action_id=plan.actions[0].action_id,
                issue_id=plan.actions[0].issue_id,
                run_id="run_1",
                status="success",
                message="done",
            )
        ],
        quality_delta={"visual_score_avg_delta": 1.0},
    )

    plan_paths = write_repair_plan(plan, tmp_path)
    result_paths = write_repair_result(result, tmp_path)
    write_repair_report_markdown(plan=plan, result=result, output_path=tmp_path / "repair_report.md")
    markdown = (tmp_path / "repair_report.md").read_text(encoding="utf-8")

    assert Path(plan_paths["repair_plan_json"]).exists()
    assert Path(result_paths["repair_result_json"]).exists()
    assert "# Repair Report" in markdown
    assert "sk-secret123456789" not in markdown


def test_integration_builds_plan_from_artifacts_and_writes_outputs(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run_1"
    run_dir.mkdir(parents=True)
    (run_dir / "quality_report.json").write_text(json.dumps(_quality_report(), ensure_ascii=False), encoding="utf-8")
    (run_dir / "trace_summary.json").write_text(json.dumps(_trace_summary(), ensure_ascii=False), encoding="utf-8")

    plan = build_repair_plan_from_run_artifacts(run_id="run_1", run_dir=run_dir)
    artifacts = write_repair_artifacts_for_run(run_id="run_1", run_dir=run_dir, plan=plan)

    assert plan.actions
    assert Path(artifacts["repair_plan_json"]).name == "repair_plan.json"
    assert Path(artifacts["repair_report_md"]).name == "repair_report.md"
    assert (run_dir / "repair_plan.json").exists()


def test_integration_handles_missing_artifacts_without_throwing(tmp_path: Path) -> None:
    empty_plan = build_repair_plan_from_run_artifacts(run_id="missing", run_dir=tmp_path / "missing")
    assert empty_plan.status == "empty"
    assert empty_plan.metadata["missing_artifacts"]

    run_dir = tmp_path / "quality_only"
    run_dir.mkdir()
    quality = _quality_report()
    quality["run"]["visual_score_min"] = 2.0
    (run_dir / "quality_report.json").write_text(json.dumps(quality, ensure_ascii=False), encoding="utf-8")

    plan = build_repair_plan_from_run_artifacts(run_id="quality_only", run_dir=run_dir)
    assert plan.actions
