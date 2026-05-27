from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from backend.harness.memory.models import MemoryWriteResult  # noqa: E402
from backend.harness.orchestration.integration import (  # noqa: E402
    build_replan_decision_from_run_artifacts,
    write_replan_artifacts_for_run,
)
from backend.harness.runtime_integration import (  # noqa: E402
    HarnessArtifactKind,
    HarnessArtifactRef,
    HarnessBundleResult,
    HarnessIntegrationConfig,
    HarnessManifest,
    PostRunHarnessRunner,
    build_default_post_run_config,
    collect_run_artifacts,
    load_harness_manifest,
    run_post_generation_harness,
    write_harness_bundle_result,
    write_harness_manifest,
    write_harness_summary_markdown,
)
from backend.harness.runtime_integration.post_run import _benchmark_output_refs, _status_from_state  # noqa: E402


def _quality_report() -> dict[str, Any]:
    return {
        "run": {
            "topic": "人工智能导论课程大纲",
            "slide_count": 6,
            "pptx_exists": True,
            "preview_success": True,
            "visual_score_avg": 4.2,
            "visual_score_min": 4.0,
            "content_issue_count": 1,
            "repair_attempt_count": 0,
        },
        "summary": {"status": "success", "issue_count": 1, "critical_issue_count": 0},
        "slides": [{"slide_index": 0, "title": "背景与目标"}],
        "issues": [],
        "missing_reasons": {},
        "artifacts": {},
    }


def _trace_summary() -> dict[str, Any]:
    return {
        "run_id": "run_1",
        "status": "success",
        "phase_count": 5,
        "tool_call_count": 2,
        "tool_attempt_count": 2,
        "failed_tool_count": 0,
        "skipped_tool_count": 0,
        "timeout_tool_count": 0,
        "error_signatures": [],
        "artifact_refs": {"quality": "/private/tmp/project/outputs/runs/run_1/quality_report.json"},
        "quality_report_paths": ["runs/run_1/quality_report.json"],
    }


def _write_run_artifacts(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "quality_report.json").write_text(json.dumps(_quality_report(), ensure_ascii=False), encoding="utf-8")
    (run_dir / "quality_report.md").write_text("# Quality\n", encoding="utf-8")
    (run_dir / "trace_summary.json").write_text(json.dumps(_trace_summary(), ensure_ascii=False), encoding="utf-8")
    (run_dir / "trace.jsonl").write_text("", encoding="utf-8")
    (run_dir / "deck.pptx").write_bytes(b"pptx")
    (run_dir / "preview").mkdir(exist_ok=True)
    (run_dir / "preview" / "slide_1.png").write_bytes(b"png")


class FakeMemory:
    def __init__(self) -> None:
        self.records: list[Any] = []

    def write(self, record: Any) -> MemoryWriteResult:
        self.records.append(record)
        return MemoryWriteResult(memory_id=record.memory_id, created=True)


class BrokenMemory:
    def write(self, record: Any) -> MemoryWriteResult:
        raise RuntimeError("api_key=sk-secret123456789 memory down")


class FakeTrace:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.records: list[tuple[str, dict[str, Any]]] = []

    def record(self, stage: str, payload: dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("trace down")
        self.records.append((stage, payload))


class FakeBenchmarkReport:
    def __init__(self) -> None:
        self.benchmark_id = "bench_x"
        self.status = "pass"
        self.metadata: dict[str, Any] = {}


class BenchmarkFallbackRunner(PostRunHarnessRunner):
    def _run_one_run_benchmark(self, *, run_id: str, run_path: Path) -> FakeBenchmarkReport:
        del run_id, run_path
        return FakeBenchmarkReport()


def test_runtime_integration_models_serialize_and_sanitize_paths() -> None:
    artifact = HarnessArtifactRef(
        name="quality_report",
        kind=HarnessArtifactKind.QUALITY,
        path="/private/tmp/project/outputs/runs/run_1/quality_report.json",
        exists=True,
        required=True,
    )
    manifest = HarnessManifest(
        run_id="run_1",
        artifacts=[artifact],
        generated_artifacts={"secret": "/Users/me/project/outputs/runs/run_1/harness_bundle.json"},
    )
    result = HarnessBundleResult(run_id="run_1", manifest=manifest, errors=["system_prompt=private"])
    serialized = artifact.model_dump_json() + manifest.model_dump_json() + result.model_dump_json()

    assert HarnessArtifactRef.model_validate_json(artifact.model_dump_json()).kind == HarnessArtifactKind.QUALITY
    assert HarnessManifest.model_validate_json(manifest.model_dump_json()).run_id == "run_1"
    assert HarnessBundleResult.model_validate_json(result.model_dump_json()).run_id == "run_1"
    assert "/private/tmp" not in serialized
    assert "/Users/" not in serialized
    assert "system_prompt=private" not in serialized


def test_harness_manifest_has_schema_version_and_generated_at() -> None:
    manifest = HarnessManifest(run_id="run_1")

    assert manifest.schema_version == "1.0.0"
    assert manifest.generated_at


def test_collect_run_artifacts_required_optional_and_safe_paths(tmp_path: Path) -> None:
    run_dir = tmp_path / "outputs" / "runs" / "run_1"
    _write_run_artifacts(run_dir)
    artifacts = collect_run_artifacts(run_id="run_1", run_dir=run_dir)
    by_name = {artifact.name: artifact for artifact in artifacts}
    serialized = json.dumps([artifact.model_dump(mode="json") for artifact in artifacts], ensure_ascii=False)

    assert by_name["quality_report.json"].required is True
    assert by_name["quality_report.json"].exists is True
    assert by_name["trace_summary.json"].required is True
    assert by_name["trace_summary.json"].exists is True
    assert by_name["repair_plan.json"].required is False
    assert by_name["repair_plan.json"].exists is False
    assert any(artifact.kind == HarnessArtifactKind.PPTX for artifact in artifacts)
    assert any(artifact.kind == HarnessArtifactKind.PREVIEW for artifact in artifacts)
    assert str(tmp_path) not in serialized


def test_collect_run_artifacts_missing_quality_is_required_missing(tmp_path: Path) -> None:
    run_dir = tmp_path / "outputs" / "runs" / "run_1"
    run_dir.mkdir(parents=True)
    (run_dir / "trace_summary.json").write_text(json.dumps(_trace_summary()), encoding="utf-8")

    artifacts = collect_run_artifacts(run_id="run_1", run_dir=run_dir)
    quality = next(artifact for artifact in artifacts if artifact.name == "quality_report.json")
    repair = next(artifact for artifact in artifacts if artifact.name == "repair_plan.json")

    assert quality.required is True
    assert quality.exists is False
    assert repair.required is False
    assert repair.exists is False


def test_collect_run_artifacts_limits_preview_artifacts(tmp_path: Path) -> None:
    run_dir = tmp_path / "outputs" / "runs" / "run_1"
    run_dir.mkdir(parents=True)
    (run_dir / "quality_report.json").write_text(json.dumps(_quality_report()), encoding="utf-8")
    (run_dir / "trace_summary.json").write_text(json.dumps(_trace_summary()), encoding="utf-8")
    preview_dir = run_dir / "preview"
    preview_dir.mkdir()
    for index in range(25):
        (preview_dir / f"slide_{index}.png").write_bytes(b"png")

    artifacts = collect_run_artifacts(run_id="run_1", run_dir=run_dir)
    preview_pngs = [artifact for artifact in artifacts if artifact.kind == HarnessArtifactKind.PREVIEW and artifact.name.endswith(".png")]

    assert len(preview_pngs) == 20
    assert any(artifact.name == "preview_images_truncated" for artifact in artifacts)


def test_harness_manifest_and_bundle_writers(tmp_path: Path) -> None:
    manifest = HarnessManifest(run_id="run_1", status="success")
    result = HarnessBundleResult(run_id="run_1", status="success", manifest=manifest)

    refs = write_harness_manifest(manifest, tmp_path)
    bundle_refs = write_harness_bundle_result(result, tmp_path)
    loaded = load_harness_manifest(tmp_path / "harness_manifest.json")
    (tmp_path / "bad.json").write_text("[]", encoding="utf-8")

    assert (tmp_path / "harness_manifest.json").exists()
    assert (tmp_path / "harness_bundle.json").exists()
    assert loaded is not None and loaded.run_id == "run_1"
    assert load_harness_manifest(tmp_path / "bad.json") is None
    assert "harness_manifest.json" in json.dumps(refs)
    assert "harness_bundle.json" in json.dumps(bundle_refs)


def test_harness_summary_markdown_is_safe(tmp_path: Path) -> None:
    artifact = HarnessArtifactRef(
        name="quality_report.json",
        kind=HarnessArtifactKind.QUALITY,
        path="/home/user/project/outputs/runs/run_1/quality_report.json",
        exists=False,
        required=True,
    )
    manifest = HarnessManifest(
        run_id="run_1",
        status="warning",
        artifacts=[artifact],
        missing_required_artifacts=["quality_report.json"],
        generated_artifacts={"bundle": "/private/tmp/project/outputs/runs/run_1/harness_bundle.json"},
    )
    result = HarnessBundleResult(
        run_id="run_1",
        status="warning",
        manifest=manifest,
        errors=["api_key=sk-secret123456789 hidden_reasoning=private"],
    )

    write_harness_summary_markdown(result=result, output_path=tmp_path / "harness_summary.md")
    markdown = (tmp_path / "harness_summary.md").read_text(encoding="utf-8")

    assert "## Required Artifacts" in markdown
    assert "## Optional Artifacts" in markdown
    assert "## Generated Artifacts" in markdown
    assert "## Missing Artifacts" in markdown
    assert "## Errors" in markdown
    assert "## Status Reason" in markdown
    assert "/private/tmp" not in markdown
    assert "/home/" not in markdown
    assert "/Users/" not in markdown
    assert "sk-secret123456789" not in markdown
    assert "hidden_reasoning=private" not in markdown


def test_harness_summary_contains_status_reason(tmp_path: Path) -> None:
    manifest = HarnessManifest(
        run_id="run_1",
        status="warning",
        metadata={"status_reasons": ["replan patch proposed"]},
    )
    result = HarnessBundleResult(run_id="run_1", status="warning", manifest=manifest)

    write_harness_summary_markdown(result=result, output_path=tmp_path / "harness_summary.md")
    markdown = (tmp_path / "harness_summary.md").read_text(encoding="utf-8")

    assert "## Status Reason" in markdown
    assert "replan patch proposed" in markdown


def test_post_run_runner_generates_bundle_without_memory(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    run_dir = output_root / "runs" / "run_1"
    _write_run_artifacts(run_dir)
    trace = FakeTrace()

    result = PostRunHarnessRunner(output_root=output_root, trace=trace).run(run_id="run_1", run_dir=run_dir)

    assert result.status in {"success", "warning"}
    assert "episode memory skipped" in json.dumps(result.manifest.metadata)
    assert (run_dir / "harness_manifest.json").exists()
    assert (run_dir / "harness_bundle.json").exists()
    assert (run_dir / "harness_summary.md").exists()
    assert (run_dir / "repair_plan.json").exists()
    assert (run_dir / "repair_report.md").exists()
    assert (run_dir / "plan_graph.json").exists()
    assert (run_dir / "replan_decision.json").exists()
    assert (run_dir / "replan_report.md").exists()
    assert not (output_root / "benchmarks").exists()
    assert any(stage == "artifact.created" for stage, _ in trace.records)


def test_post_run_runner_final_manifest_does_not_mark_self_generated_artifacts_missing(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    run_dir = output_root / "runs" / "run_1"
    _write_run_artifacts(run_dir)

    PostRunHarnessRunner(output_root=output_root).run(run_id="run_1", run_dir=run_dir)
    manifest = json.loads((run_dir / "harness_manifest.json").read_text(encoding="utf-8"))
    by_name = {artifact["name"]: artifact for artifact in manifest["artifacts"]}

    assert by_name["harness_manifest.json"]["exists"] is True
    assert by_name["harness_bundle.json"]["exists"] is True
    assert by_name["harness_summary.md"]["exists"] is True
    assert "harness_manifest.json" not in manifest["missing_optional_artifacts"]
    assert "harness_bundle.json" not in manifest["missing_optional_artifacts"]
    assert "harness_summary.md" not in manifest["missing_optional_artifacts"]


def test_post_run_runner_manifest_generated_artifacts_match_result_refs(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    run_dir = output_root / "runs" / "run_1"
    _write_run_artifacts(run_dir)

    result = PostRunHarnessRunner(output_root=output_root).run(run_id="run_1", run_dir=run_dir)
    bundle = json.loads((run_dir / "harness_bundle.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "harness_manifest.json").read_text(encoding="utf-8"))

    assert result.manifest.generated_artifacts == result.artifact_refs
    assert bundle["artifact_refs"] == manifest["generated_artifacts"]


def test_post_run_artifact_created_trace_contains_final_harness_refs(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    run_dir = output_root / "runs" / "run_1"
    _write_run_artifacts(run_dir)
    trace = FakeTrace()

    PostRunHarnessRunner(output_root=output_root, trace=trace).run(run_id="run_1", run_dir=run_dir)
    payload = next(payload for stage, payload in trace.records if stage == "artifact.created")

    assert "harness_manifest_json" in payload["artifact_refs"]
    assert "harness_bundle_json" in payload["artifact_refs"]
    assert "harness_summary_md" in payload["artifact_refs"]


def test_post_run_runner_writes_episode_memory_with_fake_memory(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    run_dir = output_root / "runs" / "run_1"
    _write_run_artifacts(run_dir)
    memory = FakeMemory()

    result = PostRunHarnessRunner(output_root=output_root, memory=memory).run(run_id="run_1", run_dir=run_dir)

    assert memory.records
    assert result.memory_write_ids == [memory.records[0].memory_id]
    assert result.manifest.memory_writes == result.memory_write_ids


def test_post_run_status_policy() -> None:
    assert _status_from_state(missing_required=["quality_report.json"], errors=[]) == "failed"
    assert _status_from_state(missing_required=[], errors=[], trace_status="failed") == "failed"
    assert _status_from_state(missing_required=[], errors=[], quality_status="failed") == "failed"
    assert _status_from_state(missing_required=[], errors=[], benchmark_status="fail") == "warning"
    assert _status_from_state(missing_required=[], errors=["warning"]) == "warning"
    assert _status_from_state(missing_required=[], errors=[], replan_status="patch_proposed") == "warning"
    assert _status_from_state(missing_required=[], errors=[], repair_plan_status="planned") == "warning"
    assert _status_from_state(missing_required=[], errors=[], repair_plan_status="empty", replan_status="no_change") == "success"


def test_post_run_status_failed_when_trace_failed(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    run_dir = output_root / "runs" / "run_1"
    _write_run_artifacts(run_dir)
    trace = _trace_summary()
    trace["status"] = "failed"
    (run_dir / "trace_summary.json").write_text(json.dumps(trace), encoding="utf-8")

    result = PostRunHarnessRunner(output_root=output_root).run(run_id="run_1", run_dir=run_dir)

    assert result.status == "failed"


def test_post_run_status_warning_when_repair_plan_planned() -> None:
    status = _status_from_state(missing_required=[], errors=[], repair_plan_status="planned")

    assert status == "warning"


def test_post_run_status_warning_when_replan_patch_proposed(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    run_dir = output_root / "runs" / "run_1"
    _write_run_artifacts(run_dir)
    quality = _quality_report()
    quality["run"]["pptx_exists"] = False
    (run_dir / "quality_report.json").write_text(json.dumps(quality), encoding="utf-8")

    result = PostRunHarnessRunner(output_root=output_root).run(run_id="run_1", run_dir=run_dir)

    assert result.status == "warning"
    assert result.manifest.replan_status == "patch_proposed"


def test_post_run_status_success_when_repair_empty_and_replan_no_change(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    run_dir = output_root / "runs" / "run_1"
    _write_run_artifacts(run_dir)

    result = PostRunHarnessRunner(output_root=output_root).run(run_id="run_1", run_dir=run_dir)

    assert result.manifest.repair_plan_status in {"empty", "skipped"}
    assert result.manifest.replan_status == "no_change"
    assert result.status == "success"


def test_post_run_runner_one_run_benchmark_is_optional(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    run_dir = output_root / "runs" / "run_1"
    _write_run_artifacts(run_dir)
    config = HarnessIntegrationConfig(enable_episode_memory=False, enable_one_run_benchmark=True)

    result = PostRunHarnessRunner(output_root=output_root, config=config).run(run_id="run_1", run_dir=run_dir)

    assert result.benchmark_id
    assert result.manifest.benchmark_status in {"pass", "warning", "fail", "empty"}
    assert (output_root / "benchmarks" / result.benchmark_id / "benchmark_report.json").exists()


def test_post_run_one_run_benchmark_refs_fallback_when_metadata_missing(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    run_dir = output_root / "runs" / "run_1"
    _write_run_artifacts(run_dir)
    config = HarnessIntegrationConfig(enable_episode_memory=False, enable_one_run_benchmark=True)

    result = BenchmarkFallbackRunner(output_root=output_root, config=config).run(run_id="run_1", run_dir=run_dir)

    assert result.benchmark_id == "bench_x"
    assert "benchmark_report_json" in result.artifact_refs
    assert "benchmark_report_md" in result.artifact_refs
    assert "case_results_jsonl" in result.artifact_refs


def test_benchmark_output_refs_fallback_when_metadata_missing(tmp_path: Path) -> None:
    refs = _benchmark_output_refs(output_root=tmp_path / "outputs", benchmark_id="bench_x", metadata={})

    assert "benchmark_report_json" in refs
    assert "benchmark_report_md" in refs
    assert "case_results_jsonl" in refs
    assert str(tmp_path) not in json.dumps(refs)


def test_post_run_runner_fail_soft_and_fail_hard(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    run_dir = output_root / "runs" / "run_1"
    _write_run_artifacts(run_dir)

    soft = PostRunHarnessRunner(output_root=output_root, memory=BrokenMemory()).run(run_id="run_1", run_dir=run_dir)
    assert soft.errors
    assert soft.status == "warning"
    assert "sk-secret123456789" not in json.dumps(soft.errors)

    config = HarnessIntegrationConfig(fail_soft=False)
    with pytest.raises(RuntimeError):
        PostRunHarnessRunner(output_root=output_root, memory=BrokenMemory(), config=config).run(
            run_id="run_1",
            run_dir=run_dir,
        )


def test_post_run_runner_trace_failure_is_best_effort(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    run_dir = output_root / "runs" / "run_1"
    _write_run_artifacts(run_dir)

    result = PostRunHarnessRunner(output_root=output_root, trace=FakeTrace(fail=True)).run(run_id="run_1", run_dir=run_dir)

    assert result.artifact_refs


def test_replan_artifacts_for_run_carries_signals_into_report(tmp_path: Path) -> None:
    run_dir = tmp_path / "outputs" / "runs" / "run_1"
    _write_run_artifacts(run_dir)
    plan, signals, decision = build_replan_decision_from_run_artifacts(run_id="run_1", run_dir=run_dir)

    write_replan_artifacts_for_run(run_id="run_1", run_dir=run_dir, plan=plan, decision=decision, signals=signals)
    markdown = (run_dir / "replan_report.md").read_text(encoding="utf-8")

    assert "## Run Signals" in markdown
    assert "pptx_exists" in markdown

    write_replan_artifacts_for_run(run_id="run_1", run_dir=run_dir, plan=plan, decision=decision)
    markdown_without_signals = (run_dir / "replan_report.md").read_text(encoding="utf-8")
    assert "# Replan Report" in markdown_without_signals


def test_run_post_generation_harness_helper_and_default_config(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    run_dir = output_root / "runs" / "run_1"
    _write_run_artifacts(run_dir)

    config = build_default_post_run_config()
    result = run_post_generation_harness(run_id="run_1", run_dir=run_dir, output_root=output_root, config=config)

    assert config.execute_repair is False
    assert config.apply_replan_patches is False
    assert config.enable_one_run_benchmark is False
    assert config.fail_soft is True
    assert result.run_id == "run_1"
    assert result.artifact_refs
