from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, TypeVar

from backend.harness.benchmark.cases import BenchmarkCase, BenchmarkExpected, BenchmarkSuite
from backend.harness.benchmark.runner import BenchmarkRunner
from backend.harness.memory.integration import write_episode_memory_for_run
from backend.harness.orchestration.integration import (
    build_replan_decision_from_run_artifacts,
    write_replan_artifacts_for_run,
)
from backend.harness.repair.integration import (
    build_repair_plan_from_run_artifacts,
    write_repair_artifacts_for_run,
)
from backend.harness.runtime_integration.artifact_manifest import (
    write_harness_bundle_result,
    write_harness_manifest,
)
from backend.harness.runtime_integration.collector import collect_run_artifacts
from backend.harness.runtime_integration.models import (
    HarnessBundleResult,
    HarnessIntegrationConfig,
    HarnessManifest,
)
from backend.harness.runtime_integration.safety import (
    sanitize_runtime_artifacts,
    sanitize_runtime_mapping,
    sanitize_runtime_text,
)
from backend.harness.runtime_integration.summary import write_harness_summary_markdown

T = TypeVar("T")


class PostRunHarnessRunner:
    def __init__(
        self,
        *,
        output_root: str | Path,
        memory: Any | None = None,
        trace: Any | None = None,
        config: HarnessIntegrationConfig | None = None,
    ):
        self.output_root = Path(output_root)
        self.memory = memory
        self.trace = trace
        self.config = config or HarnessIntegrationConfig()

    def run(
        self,
        *,
        run_id: str,
        run_dir: str | Path,
    ) -> HarnessBundleResult:
        run_path = Path(run_dir)
        errors: list[str] = []
        generated_artifacts: dict[str, str] = {}
        metadata: dict[str, Any] = {"episode_memory": ""}
        memory_write_ids: list[str] = []
        repair_plan_id: str | None = None
        repair_plan_status: str | None = None
        replan_decision_id: str | None = None
        replan_status: str | None = None
        benchmark_id: str | None = None
        benchmark_status: str | None = None

        if self.config.execute_repair:
            errors.append("execute_repair is not supported in runtime integration phase")
        if self.config.apply_replan_patches:
            errors.append("apply_replan_patches is not supported in runtime integration phase")

        if self.config.enable_episode_memory:
            if self.memory is None:
                metadata["episode_memory"] = "episode memory skipped: memory not configured"
            else:
                memory_result = self._fail_soft(
                    "episode memory",
                    lambda: write_episode_memory_for_run(run_id=run_id, run_dir=run_path, memory=self.memory),
                    errors,
                )
                if memory_result is not None and not memory_result.skipped:
                    memory_write_ids.append(memory_result.memory_id)
                    metadata["episode_memory"] = "written"

        if self.config.enable_repair_planning:
            repair_plan = self._fail_soft(
                "repair planning",
                lambda: build_repair_plan_from_run_artifacts(run_id=run_id, run_dir=run_path, trace=self.trace),
                errors,
            )
            if repair_plan is not None:
                repair_plan_id = repair_plan.plan_id
                repair_plan_status = repair_plan.status
                refs = self._fail_soft(
                    "repair artifact writing",
                    lambda: write_repair_artifacts_for_run(run_id=run_id, run_dir=run_path, plan=repair_plan),
                    errors,
                )
                if refs:
                    generated_artifacts.update(refs)

        if self.config.enable_replan_decision:
            replan_tuple = self._fail_soft(
                "replan decision",
                lambda: build_replan_decision_from_run_artifacts(run_id=run_id, run_dir=run_path, trace=self.trace),
                errors,
            )
            if replan_tuple is not None:
                plan, signals, decision = replan_tuple
                replan_decision_id = decision.decision_id
                replan_status = decision.status
                refs = self._fail_soft(
                    "replan artifact writing",
                    lambda: write_replan_artifacts_for_run(
                        run_id=run_id,
                        run_dir=run_path,
                        plan=plan,
                        decision=decision,
                        signals=signals,
                    ),
                    errors,
                )
                if refs:
                    generated_artifacts.update(refs)

        if self.config.enable_one_run_benchmark:
            benchmark_report = self._fail_soft(
                "one-run benchmark",
                lambda: self._run_one_run_benchmark(run_id=run_id, run_path=run_path),
                errors,
            )
            if benchmark_report is not None:
                benchmark_id = benchmark_report.benchmark_id
                benchmark_status = benchmark_report.status
                output_paths = benchmark_report.metadata.get("output_paths", {})
                if isinstance(output_paths, dict):
                    generated_artifacts.update(sanitize_runtime_artifacts(output_paths))

        artifacts = collect_run_artifacts(run_id=run_id, run_dir=run_path)
        missing_required = [artifact.name for artifact in artifacts if artifact.required and not artifact.exists]
        missing_optional = [
            artifact.name
            for artifact in artifacts
            if not artifact.required and not artifact.exists and self.config.include_optional_artifacts
        ]
        quality_status = _read_nested_status(run_path / "quality_report.json", "summary")
        trace_status = _read_status(run_path / "trace_summary.json")
        status = _status_from_state(missing_required=missing_required, errors=errors)
        manifest = HarnessManifest(
            run_id=run_id,
            status=status if status != "partial" else "partial",
            artifacts=artifacts,
            missing_required_artifacts=missing_required,
            missing_optional_artifacts=missing_optional,
            generated_artifacts=generated_artifacts,
            quality_status=quality_status,
            trace_status=trace_status,
            repair_plan_status=repair_plan_status,
            replan_status=replan_status,
            benchmark_status=benchmark_status,
            memory_writes=memory_write_ids,
            summary=_summary_text(status, missing_required, errors),
            metadata=sanitize_runtime_mapping({**metadata, **self.config.metadata}),
        )
        result = HarnessBundleResult(
            run_id=run_id,
            status=status,
            manifest=manifest,
            artifact_refs=generated_artifacts,
            memory_write_ids=memory_write_ids,
            repair_plan_id=repair_plan_id,
            replan_decision_id=replan_decision_id,
            benchmark_id=benchmark_id,
            errors=errors,
            metadata=sanitize_runtime_mapping({"output_root": str(self.output_root)}),
        )

        manifest_refs = write_harness_manifest(manifest, run_path)
        result.artifact_refs.update(manifest_refs)
        bundle_refs = write_harness_bundle_result(result, run_path)
        result.artifact_refs.update(bundle_refs)
        result.manifest.generated_artifacts = sanitize_runtime_artifacts(result.artifact_refs)
        summary_path = run_path / "harness_summary.md"
        write_harness_summary_markdown(result=result, output_path=summary_path)
        result.artifact_refs.update(sanitize_runtime_artifacts({"harness_summary_md": str(summary_path)}))
        result.manifest.generated_artifacts = sanitize_runtime_artifacts(result.artifact_refs)
        write_harness_manifest(result.manifest, run_path)
        write_harness_bundle_result(result, run_path)
        self._record_artifact_created(result)
        return result

    def _run_one_run_benchmark(self, *, run_id: str, run_path: Path):
        suite = BenchmarkSuite(
            suite_id=self.config.benchmark_suite_id,
            name="Single-run offline harness benchmark",
            cases=[
                BenchmarkCase(
                    case_id=run_id,
                    name=f"Post-run benchmark for {run_id}",
                    run_id=run_id,
                    expected=BenchmarkExpected(
                        require_pptx=False,
                        require_preview=False,
                        require_quality_report=True,
                        require_trace_summary=True,
                    ),
                    tags=["single-run", "offline", "post-run"],
                )
            ],
        )
        benchmark_id = f"bench_{self.config.benchmark_suite_id}_{run_id}"
        runner = BenchmarkRunner(output_root=self.output_root, runs_root=run_path.parent)
        return runner.run_offline(suite=suite, benchmark_id=benchmark_id)

    def _fail_soft(self, label: str, func: Callable[[], T], errors: list[str]) -> T | None:
        try:
            return func()
        except Exception as exc:
            message = sanitize_runtime_text(f"{label} failed: {exc}", limit=500)
            if not self.config.fail_soft:
                raise RuntimeError(message) from exc
            errors.append(message)
            return None

    def _record_artifact_created(self, result: HarnessBundleResult) -> None:
        if not self.trace:
            return
        record = getattr(self.trace, "record", None)
        if not callable(record):
            return
        try:
            record(
                stage="artifact.created",
                payload=sanitize_runtime_mapping(
                    {
                        "run_id": result.run_id,
                        "status": result.status,
                        "artifact_refs": result.artifact_refs,
                        "memory_write_count": len(result.memory_write_ids),
                        "repair_plan_id": result.repair_plan_id,
                        "replan_decision_id": result.replan_decision_id,
                        "benchmark_id": result.benchmark_id,
                    }
                ),
            )
        except Exception:
            return


def _read_status(path: Path) -> str | None:
    payload = _read_json_object(path)
    if not payload:
        return None
    status = payload.get("status")
    return sanitize_runtime_text(status, limit=120) if status is not None else None


def _read_nested_status(path: Path, key: str) -> str | None:
    payload = _read_json_object(path)
    nested = payload.get(key) if payload else None
    if isinstance(nested, dict) and nested.get("status") is not None:
        return sanitize_runtime_text(nested.get("status"), limit=120)
    return _read_status(path)


def _read_json_object(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _status_from_state(*, missing_required: list[str], errors: list[str]) -> str:
    if missing_required:
        return "failed"
    if errors:
        return "warning"
    return "success"


def _summary_text(status: str, missing_required: list[str], errors: list[str]) -> str:
    if missing_required:
        return f"Post-run harness completed with missing required artifacts: {', '.join(missing_required)}"
    if errors:
        return f"Post-run harness completed with {len(errors)} warning(s)."
    return "Post-run harness completed successfully."
