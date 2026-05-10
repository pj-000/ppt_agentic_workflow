from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from pydantic import BaseModel, Field

from backend.harness.runtime.runtime_memory import RuntimeMemoryStore


class BenchmarkCaseSpec(BaseModel):
    case_id: str
    topic: str = ""
    tags: list[str] = Field(default_factory=list)
    request: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""
    baseline_average_visual_score: float | None = None
    baseline_slide_overall_scores: list[float] = Field(default_factory=list)
    baseline_content_issue_count: int | None = None
    baseline_preview_image_count: int | None = None
    baseline_captured_at: str = ""
    baseline_capture_error: str = ""
    min_average_visual_score: float = 0.0
    max_content_issue_count: int = 0
    require_visual_eval: bool = False
    require_preview_images: bool = False


class BenchmarkTarget(BaseModel):
    phase: str
    error_signature: str
    memory_id: str = ""
    layout_scope: str = "*"
    visual_mode_scope: str = "*"
    include_case_ids: list[str] = Field(default_factory=list)
    include_tags: list[str] = Field(default_factory=list)
    min_case_count: int = 1
    required_success_rate: float = 1.0
    min_average_visual_delta: float = 0.0
    max_single_case_visual_drop: float = 0.0
    disallow_regressions: bool = True


class GoldenBenchmarkManifest(BaseModel):
    benchmark_id: str
    description: str = ""
    cases: list[BenchmarkCaseSpec] = Field(default_factory=list)
    targets: list[BenchmarkTarget] = Field(default_factory=list)

    @classmethod
    def from_file(cls, path: str | Path) -> "GoldenBenchmarkManifest":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class BenchmarkCaseObservation(BaseModel):
    case_id: str
    passed: bool = True
    generation_succeeded: bool = True
    regression_detected: bool = False
    output_path: str = ""
    artifact_dir: str = ""
    preview_image_count: int = 0
    average_visual_score: float = 0.0
    content_issue_count: int = 0
    average_visual_delta: float = 0.0
    max_visual_drop: float = 0.0
    notes: str = ""
    harness_trace: dict[str, Any] = Field(default_factory=dict)


class BenchmarkObservations(BaseModel):
    benchmark_id: str
    notes: str = ""
    cases: list[BenchmarkCaseObservation] = Field(default_factory=list)

    @classmethod
    def from_file(cls, path: str | Path) -> "BenchmarkObservations":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class BenchmarkTargetResult(BaseModel):
    phase: str
    error_signature: str
    memory_id: str = ""
    layout_scope: str = "*"
    visual_mode_scope: str = "*"
    selected_case_ids: list[str] = Field(default_factory=list)
    passed: bool
    success_rate: float
    average_visual_delta: float
    max_visual_drop: float
    regression_detected: bool
    notes: str = ""


class BenchmarkCaseComparison(BaseModel):
    case_id: str
    artifact_dir: str = ""
    passed: bool = True
    regression_detected: bool = False
    baseline_average_visual_score: float | None = None
    current_average_visual_score: float = 0.0
    average_visual_delta: float = 0.0
    baseline_content_issue_count: int | None = None
    current_content_issue_count: int = 0
    content_issue_delta: int = 0
    baseline_preview_image_count: int | None = None
    current_preview_image_count: int = 0
    preview_image_delta: int = 0
    notes: str = ""


class BenchmarkRunReport(BaseModel):
    benchmark_id: str
    manifest_path: str
    observations_path: str
    recorded_at: str
    target_results: list[BenchmarkTargetResult] = Field(default_factory=list)
    case_comparisons: list[BenchmarkCaseComparison] = Field(default_factory=list)
    summary_markdown_path: str = ""

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)


class BenchmarkRunner:
    def __init__(
        self,
        memory_store: RuntimeMemoryStore | None = None,
        runs_root: Path | None = None,
    ) -> None:
        self.memory_store = memory_store or RuntimeMemoryStore()
        self.runs_root = runs_root or (config.BENCHMARKS_DIR / "runs")

    def run(
        self,
        *,
        manifest_path: str | Path,
        observations_path: str | Path,
    ) -> BenchmarkRunReport:
        manifest = GoldenBenchmarkManifest.from_file(manifest_path)
        observations = BenchmarkObservations.from_file(observations_path)
        if observations.benchmark_id != manifest.benchmark_id:
            raise ValueError(
                f"benchmark_id 不匹配：manifest={manifest.benchmark_id} observations={observations.benchmark_id}"
            )

        target_results = [
            self._evaluate_target(manifest, observations, target)
            for target in manifest.targets
        ]
        report = BenchmarkRunReport(
            benchmark_id=manifest.benchmark_id,
            manifest_path=str(Path(manifest_path)),
            observations_path=str(Path(observations_path)),
            recorded_at=self._utc_now_iso(),
            target_results=target_results,
            case_comparisons=self._build_case_comparisons(manifest, observations),
        )
        summary_path = self._save_report(report)
        report = report.model_copy(update={"summary_markdown_path": str(summary_path)})
        self._save_report(report)
        return report

    @staticmethod
    def _build_case_comparisons(
        manifest: GoldenBenchmarkManifest,
        observations: BenchmarkObservations,
    ) -> list[BenchmarkCaseComparison]:
        observation_map = {item.case_id: item for item in observations.cases}
        comparisons: list[BenchmarkCaseComparison] = []
        for case in manifest.cases:
            observed = observation_map.get(case.case_id)
            if observed is None:
                comparisons.append(
                    BenchmarkCaseComparison(
                        case_id=case.case_id,
                        baseline_average_visual_score=case.baseline_average_visual_score,
                        baseline_content_issue_count=case.baseline_content_issue_count,
                        baseline_preview_image_count=case.baseline_preview_image_count,
                        passed=False,
                        notes="missing observation",
                    )
                )
                continue
            comparisons.append(
                BenchmarkCaseComparison(
                    case_id=case.case_id,
                    artifact_dir=observed.artifact_dir,
                    passed=observed.passed,
                    regression_detected=observed.regression_detected,
                    baseline_average_visual_score=case.baseline_average_visual_score,
                    current_average_visual_score=observed.average_visual_score,
                    average_visual_delta=observed.average_visual_delta,
                    baseline_content_issue_count=case.baseline_content_issue_count,
                    current_content_issue_count=observed.content_issue_count,
                    content_issue_delta=observed.content_issue_count - (case.baseline_content_issue_count or 0),
                    baseline_preview_image_count=case.baseline_preview_image_count,
                    current_preview_image_count=observed.preview_image_count,
                    preview_image_delta=observed.preview_image_count - (case.baseline_preview_image_count or 0),
                    notes=observed.notes,
                )
            )
        return comparisons

    def _evaluate_target(
        self,
        manifest: GoldenBenchmarkManifest,
        observations: BenchmarkObservations,
        target: BenchmarkTarget,
    ) -> BenchmarkTargetResult:
        selected_cases = self._select_cases(manifest, target)
        observation_map = {item.case_id: item for item in observations.cases}
        case_observations: list[BenchmarkCaseObservation] = []
        notes: list[str] = []

        for case in selected_cases:
            observed = observation_map.get(case.case_id)
            if observed is None:
                case_observations.append(
                    BenchmarkCaseObservation(
                        case_id=case.case_id,
                        passed=False,
                        generation_succeeded=False,
                        regression_detected=False,
                        average_visual_delta=-1.0,
                        max_visual_drop=1.0,
                        notes="missing observation",
                    )
                )
                notes.append(f"missing observation: {case.case_id}")
                continue
            case_observations.append(observed)

        if len(case_observations) < target.min_case_count:
            passed = False
            success_rate = 0.0
            avg_delta = 0.0
            max_drop = 0.0
            regression_detected = False
            notes.append(
                f"insufficient cases: expected>={target.min_case_count}, got={len(case_observations)}"
            )
        else:
            success_count = sum(
                1 for item in case_observations if item.passed and item.generation_succeeded
            )
            success_rate = round(success_count / max(len(case_observations), 1), 4)
            avg_delta = round(
                sum(item.average_visual_delta for item in case_observations) / len(case_observations),
                4,
            )
            max_drop = round(max(item.max_visual_drop for item in case_observations), 4)
            regression_detected = any(item.regression_detected for item in case_observations)
            passed = (
                success_rate >= target.required_success_rate
                and avg_delta >= target.min_average_visual_delta
                and max_drop <= target.max_single_case_visual_drop
                and (not regression_detected or not target.disallow_regressions)
            )

            if success_rate < target.required_success_rate:
                notes.append(
                    f"success_rate {success_rate:.2f} < required {target.required_success_rate:.2f}"
                )
            if avg_delta < target.min_average_visual_delta:
                notes.append(
                    f"average_visual_delta {avg_delta:.2f} < min {target.min_average_visual_delta:.2f}"
                )
            if max_drop > target.max_single_case_visual_drop:
                notes.append(
                    f"max_visual_drop {max_drop:.2f} > max {target.max_single_case_visual_drop:.2f}"
                )
            if regression_detected and target.disallow_regressions:
                notes.append("regression detected in observed cases")

        verdict_notes = "; ".join(notes)
        self.memory_store.record_benchmark_result(
            phase=target.phase,
            error_signature=target.error_signature,
            benchmark_id=manifest.benchmark_id,
            passed=passed,
            memory_id=target.memory_id,
            regression_detected=regression_detected,
            average_visual_delta=avg_delta,
            notes=verdict_notes,
            layout_scope=target.layout_scope,
            visual_mode_scope=target.visual_mode_scope,
        )

        return BenchmarkTargetResult(
            phase=target.phase,
            error_signature=target.error_signature,
            memory_id=target.memory_id,
            layout_scope=target.layout_scope,
            visual_mode_scope=target.visual_mode_scope,
            selected_case_ids=[item.case_id for item in selected_cases],
            passed=passed,
            success_rate=success_rate,
            average_visual_delta=avg_delta,
            max_visual_drop=max_drop,
            regression_detected=regression_detected,
            notes=verdict_notes,
        )

    @staticmethod
    def _select_cases(
        manifest: GoldenBenchmarkManifest,
        target: BenchmarkTarget,
    ) -> list[BenchmarkCaseSpec]:
        selected: list[BenchmarkCaseSpec] = []
        target_case_ids = set(target.include_case_ids)
        target_tags = set(target.include_tags)

        for case in manifest.cases:
            by_id = bool(target_case_ids) and case.case_id in target_case_ids
            by_tag = bool(target_tags.intersection(case.tags))
            if not target_case_ids and not target_tags:
                selected.append(case)
                continue
            if by_id or by_tag:
                selected.append(case)
        return selected

    def _save_report(self, report: BenchmarkRunReport) -> Path:
        self.runs_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base = self.runs_root / f"{report.benchmark_id}-{stamp}"
        path = base.with_suffix(".json")
        path.write_text(report.to_json() + "\n", encoding="utf-8")
        summary_path = base.with_suffix(".md")
        summary_path.write_text(self._render_summary_markdown(report), encoding="utf-8")
        return summary_path

    @staticmethod
    def _render_summary_markdown(report: BenchmarkRunReport) -> str:
        lines = [
            f"# Benchmark Summary: {report.benchmark_id}",
            "",
            f"- recorded_at: {report.recorded_at}",
            f"- manifest_path: {report.manifest_path}",
            f"- observations_path: {report.observations_path}",
            "",
            "## Target Results",
        ]
        for item in report.target_results:
            status = "PASS" if item.passed else "FAIL"
            lines.extend(
                [
                    f"### {status} {item.phase}:{item.error_signature}",
                    f"- memory_id: {item.memory_id or '(bucket)'}",
                    f"- layout_scope: {item.layout_scope}",
                    f"- visual_mode_scope: {item.visual_mode_scope}",
                    f"- success_rate: {item.success_rate}",
                    f"- average_visual_delta: {item.average_visual_delta}",
                    f"- max_visual_drop: {item.max_visual_drop}",
                    f"- regression_detected: {item.regression_detected}",
                    f"- notes: {item.notes or '(none)'}",
                    "",
                ]
            )
        lines.append("## Case Comparisons")
        for item in report.case_comparisons:
            status = "PASS" if item.passed else "FAIL"
            lines.extend(
                [
                    f"### {status} {item.case_id}",
                    f"- artifact_dir: {item.artifact_dir or '(none)'}",
                    f"- baseline_average_visual_score: {item.baseline_average_visual_score}",
                    f"- current_average_visual_score: {item.current_average_visual_score}",
                    f"- average_visual_delta: {item.average_visual_delta}",
                    f"- baseline_content_issue_count: {item.baseline_content_issue_count}",
                    f"- current_content_issue_count: {item.current_content_issue_count}",
                    f"- content_issue_delta: {item.content_issue_delta}",
                    f"- baseline_preview_image_count: {item.baseline_preview_image_count}",
                    f"- current_preview_image_count: {item.current_preview_image_count}",
                    f"- preview_image_delta: {item.preview_image_delta}",
                    f"- regression_detected: {item.regression_detected}",
                    f"- notes: {item.notes or '(none)'}",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
