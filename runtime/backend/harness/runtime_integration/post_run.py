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
                generated_artifacts.update(
                    _benchmark_output_refs(
                        output_root=self.output_root,
                        benchmark_id=benchmark_id,
                        metadata=benchmark_report.metadata,
                    )
                )

        artifacts = collect_run_artifacts(run_id=run_id, run_dir=run_path)
        missing_required, missing_optional = _missing_artifact_names(
            artifacts,
            include_optional=self.config.include_optional_artifacts,
        )
        quality_status = _read_nested_status(run_path / "quality_report.json", "summary")
        trace_status = _read_status(run_path / "trace_summary.json")
        status = _status_from_state(
            missing_required=missing_required,
            errors=errors,
            quality_status=quality_status,
            trace_status=trace_status,
            repair_plan_status=repair_plan_status,
            replan_status=replan_status,
            benchmark_status=benchmark_status,
        )
        status_reasons = _status_reasons(
            missing_required=missing_required,
            errors=errors,
            quality_status=quality_status,
            trace_status=trace_status,
            repair_plan_status=repair_plan_status,
            replan_status=replan_status,
            benchmark_status=benchmark_status,
        )
        manifest = HarnessManifest(
            run_id=run_id,
            status=status,
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
            summary=_summary_text(status, missing_required, errors, status_reasons),
            metadata=sanitize_runtime_mapping({**metadata, "status_reasons": status_reasons, **self.config.metadata}),
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

        manifest_refs = self._fail_soft("harness manifest writing", lambda: write_harness_manifest(manifest, run_path), errors) or {}
        result.artifact_refs.update(manifest_refs)
        bundle_refs = self._fail_soft("harness bundle writing", lambda: write_harness_bundle_result(result, run_path), errors) or {}
        result.artifact_refs.update(bundle_refs)
        result.manifest.generated_artifacts = sanitize_runtime_artifacts(result.artifact_refs)
        summary_path = run_path / "harness_summary.md"
        self._fail_soft("harness summary writing", lambda: write_harness_summary_markdown(result=result, output_path=summary_path), errors)
        result.artifact_refs.update(sanitize_runtime_artifacts({"harness_summary_md": str(summary_path)}))
        self._finalize_result(
            result=result,
            run_path=run_path,
            errors=errors,
            quality_status=quality_status,
            trace_status=trace_status,
            repair_plan_status=repair_plan_status,
            replan_status=replan_status,
            benchmark_status=benchmark_status,
        )
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

    def _finalize_result(
        self,
        *,
        result: HarnessBundleResult,
        run_path: Path,
        errors: list[str],
        quality_status: str | None,
        trace_status: str | None,
        repair_plan_status: str | None,
        replan_status: str | None,
        benchmark_status: str | None,
    ) -> None:
        final_artifacts = collect_run_artifacts(run_id=result.run_id, run_dir=run_path)
        missing_required, missing_optional = _missing_artifact_names(
            final_artifacts,
            include_optional=self.config.include_optional_artifacts,
        )
        final_status = _status_from_state(
            missing_required=missing_required,
            errors=errors,
            quality_status=quality_status,
            trace_status=trace_status,
            repair_plan_status=repair_plan_status,
            replan_status=replan_status,
            benchmark_status=benchmark_status,
        )
        status_reasons = _status_reasons(
            missing_required=missing_required,
            errors=errors,
            quality_status=quality_status,
            trace_status=trace_status,
            repair_plan_status=repair_plan_status,
            replan_status=replan_status,
            benchmark_status=benchmark_status,
        )
        final_refs = sanitize_runtime_artifacts(result.artifact_refs)
        result.artifact_refs = final_refs
        result.status = final_status
        result.errors = list(errors)
        result.manifest = result.manifest.model_copy(
            update={
                "status": final_status,
                "artifacts": final_artifacts,
                "missing_required_artifacts": missing_required,
                "missing_optional_artifacts": missing_optional,
                "generated_artifacts": final_refs,
                "summary": _summary_text(final_status, missing_required, errors, status_reasons),
                "metadata": sanitize_runtime_mapping(
                    {
                        **result.manifest.metadata,
                        "status_reasons": status_reasons,
                    }
                ),
            }
        )
        self._fail_soft("final harness manifest writing", lambda: write_harness_manifest(result.manifest, run_path), errors)
        self._fail_soft("final harness bundle writing", lambda: write_harness_bundle_result(result, run_path), errors)
        self._fail_soft(
            "final harness summary writing",
            lambda: write_harness_summary_markdown(result=result, output_path=run_path / "harness_summary.md"),
            errors,
        )


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


def _missing_artifact_names(
    artifacts: list[Any],
    *,
    include_optional: bool,
) -> tuple[list[str], list[str]]:
    missing_required = [artifact.name for artifact in artifacts if artifact.required and not artifact.exists]
    missing_optional = [
        artifact.name
        for artifact in artifacts
        if include_optional and not artifact.required and not artifact.exists
    ]
    return missing_required, missing_optional


def _status_from_state(
    *,
    missing_required: list[str],
    errors: list[str],
    quality_status: str | None = None,
    trace_status: str | None = None,
    repair_plan_status: str | None = None,
    replan_status: str | None = None,
    benchmark_status: str | None = None,
) -> str:
    if missing_required:
        return "failed"
    if trace_status == "failed":
        return "failed"
    if quality_status in {"failed", "fail"}:
        return "failed"
    if benchmark_status == "fail":
        return "warning"
    if errors:
        return "warning"
    if quality_status == "warning" or trace_status == "warning":
        return "warning"
    if repair_plan_status in {"planned", "created"}:
        return "warning"
    if replan_status == "patch_proposed":
        return "warning"
    return "success"


def _status_reasons(
    *,
    missing_required: list[str],
    errors: list[str],
    quality_status: str | None = None,
    trace_status: str | None = None,
    repair_plan_status: str | None = None,
    replan_status: str | None = None,
    benchmark_status: str | None = None,
) -> list[str]:
    reasons: list[str] = []
    if missing_required:
        reasons.append(f"missing required artifacts: {', '.join(missing_required)}")
    if trace_status == "failed":
        reasons.append("trace status failed")
    if quality_status in {"failed", "fail"}:
        reasons.append("quality status failed")
    if benchmark_status == "fail":
        reasons.append("optional one-run benchmark failed")
    if errors:
        reasons.append(f"{len(errors)} integration warning/error(s)")
    if quality_status == "warning":
        reasons.append("quality status warning")
    if trace_status == "warning":
        reasons.append("trace status warning")
    if repair_plan_status in {"planned", "created"}:
        reasons.append(f"repair plan status {repair_plan_status}")
    if replan_status == "patch_proposed":
        reasons.append("replan patch proposed")
    return [sanitize_runtime_text(reason, limit=300) for reason in reasons]


def _summary_text(status: str, missing_required: list[str], errors: list[str], status_reasons: list[str] | None = None) -> str:
    if missing_required:
        return f"Post-run harness completed with missing required artifacts: {', '.join(missing_required)}"
    if errors:
        return f"Post-run harness completed with {len(errors)} warning(s)."
    if status == "warning" and status_reasons:
        return f"Post-run harness completed with warning: {status_reasons[0]}"
    if status == "failed" and status_reasons:
        return f"Post-run harness failed: {status_reasons[0]}"
    return "Post-run harness completed successfully."


def _benchmark_output_refs(
    *,
    output_root: Path,
    benchmark_id: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, str]:
    output_paths = metadata.get("output_paths", {}) if isinstance(metadata, dict) else {}
    if isinstance(output_paths, dict) and output_paths:
        return sanitize_runtime_artifacts(output_paths)
    output_dir = output_root / "benchmarks" / benchmark_id
    return sanitize_runtime_artifacts(
        {
            "benchmark_report_json": str(output_dir / "benchmark_report.json"),
            "benchmark_report_md": str(output_dir / "benchmark_report.md"),
            "case_results_jsonl": str(output_dir / "case_results.jsonl"),
        }
    )
