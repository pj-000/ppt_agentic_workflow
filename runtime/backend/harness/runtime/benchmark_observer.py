from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import config
from backend.harness.runtime.benchmark_runner import (
    BenchmarkCaseObservation,
    BenchmarkCaseSpec,
    BenchmarkObservations,
    BenchmarkRunReport,
    BenchmarkRunner,
    GoldenBenchmarkManifest,
)

if TYPE_CHECKING:
    from backend.harness.pipelines import PipelineService


@dataclass
class BenchmarkCaseExecution:
    case_id: str
    generation_succeeded: bool
    output_path: str = ""
    artifact_dir: str = ""
    preview_images: list[str] | None = None
    visual_overall_scores: list[float] | None = None
    content_issue_count: int = 0
    extracted_text: str = ""
    notes: str = ""
    harness_trace: dict[str, object] | None = None


class BenchmarkObservationGenerator:
    def __init__(
        self,
        *,
        pipeline_service: "PipelineService | None" = None,
        observations_root: Path | None = None,
        case_runner: Callable[[BenchmarkCaseSpec], BenchmarkCaseExecution] | None = None,
    ) -> None:
        if pipeline_service is None:
            from backend.harness.pipelines import PipelineService

            pipeline_service = PipelineService()
        self.pipeline_service = pipeline_service
        self.observations_root = observations_root or (config.BENCHMARKS_DIR / "observations")
        self.artifacts_root = config.BENCHMARKS_DIR / "artifacts"
        self.case_runner = case_runner

    def capture_baselines(
        self,
        *,
        manifest_path: str | Path,
        output_path: str | Path | None = None,
        model_provider: str = "minmax",
        no_research: bool = False,
        no_images: bool = False,
        image_source: str = "auto",
    ) -> tuple[GoldenBenchmarkManifest, Path]:
        manifest = GoldenBenchmarkManifest.from_file(manifest_path)
        updated_cases: list[BenchmarkCaseSpec] = []
        captured_at = self._utc_now_iso()

        for case in manifest.cases:
            try:
                execution = self.case_runner(case) if self.case_runner else self._default_case_runner(
                    case,
                    model_provider=model_provider,
                    no_research=no_research,
                    no_images=no_images,
                    image_source=image_source,
                )
                self._persist_case_artifacts(
                    benchmark_id=manifest.benchmark_id,
                    case=case,
                    execution=execution,
                )
                visual_scores = execution.visual_overall_scores or []
                preview_images = execution.preview_images or []
                avg_visual = round(sum(visual_scores) / len(visual_scores), 4) if visual_scores else None
                updated_cases.append(
                    case.model_copy(
                        update={
                            "baseline_average_visual_score": avg_visual,
                            "baseline_slide_overall_scores": list(visual_scores),
                            "baseline_content_issue_count": execution.content_issue_count,
                            "baseline_preview_image_count": len(preview_images),
                            "baseline_captured_at": captured_at,
                            "baseline_capture_error": "",
                        }
                    )
                )
            except Exception as exc:
                updated_cases.append(
                    case.model_copy(
                        update={
                            "baseline_capture_error": str(exc),
                        }
                    )
                )

        updated_manifest = manifest.model_copy(update={"cases": updated_cases})
        saved_path = self._save_manifest(updated_manifest, output_path=output_path)
        return updated_manifest, saved_path

    def generate_observations(
        self,
        *,
        manifest_path: str | Path,
        model_provider: str = "minmax",
        no_research: bool = False,
        no_images: bool = False,
        image_source: str = "auto",
    ) -> tuple[BenchmarkObservations, Path]:
        manifest = GoldenBenchmarkManifest.from_file(manifest_path)
        observations = BenchmarkObservations(
            benchmark_id=manifest.benchmark_id,
            notes=manifest.description,
            cases=[
                self._execute_case(
                    benchmark_id=manifest.benchmark_id,
                    case=case,
                    model_provider=model_provider,
                    no_research=no_research,
                    no_images=no_images,
                    image_source=image_source,
                )
                for case in manifest.cases
            ],
        )
        saved = self._save_observations(observations)
        return observations, saved

    def run_and_gate(
        self,
        *,
        manifest_path: str | Path,
        model_provider: str = "minmax",
        no_research: bool = False,
        no_images: bool = False,
        image_source: str = "auto",
        benchmark_runner: BenchmarkRunner | None = None,
    ) -> tuple[BenchmarkObservations, Path, BenchmarkRunReport]:
        observations, saved = self.generate_observations(
            manifest_path=manifest_path,
            model_provider=model_provider,
            no_research=no_research,
            no_images=no_images,
            image_source=image_source,
        )
        runner = benchmark_runner or BenchmarkRunner()
        report = runner.run(
            manifest_path=manifest_path,
            observations_path=saved,
        )
        return observations, saved, report

    def _execute_case(
        self,
        benchmark_id: str,
        case: BenchmarkCaseSpec,
        *,
        model_provider: str,
        no_research: bool,
        no_images: bool,
        image_source: str,
    ) -> BenchmarkCaseObservation:
        try:
            execution = self.case_runner(case) if self.case_runner else self._default_case_runner(
                case,
                model_provider=model_provider,
                no_research=no_research,
                no_images=no_images,
                image_source=image_source,
            )
        except Exception as exc:
            execution = BenchmarkCaseExecution(
                case_id=case.case_id,
                generation_succeeded=False,
                notes=f"execution failed: {exc}",
            )
        artifact_dir = self._persist_case_artifacts(
            benchmark_id=benchmark_id,
            case=case,
            execution=execution,
        )
        preview_images = execution.preview_images or []
        visual_scores = execution.visual_overall_scores or []
        avg_visual = round(sum(visual_scores) / len(visual_scores), 4) if visual_scores else 0.0
        average_visual_delta = (
            round(avg_visual - case.baseline_average_visual_score, 4)
            if case.baseline_average_visual_score is not None
            else 0.0
        )

        max_visual_drop = 0.0
        if case.baseline_slide_overall_scores and visual_scores:
            compared = zip(case.baseline_slide_overall_scores, visual_scores)
            max_visual_drop = round(max((base - current) for base, current in compared), 4)
            max_visual_drop = max(max_visual_drop, 0.0)

        regression_detected = False
        if case.baseline_average_visual_score is not None and average_visual_delta < 0:
            regression_detected = True
        if max_visual_drop > 0:
            regression_detected = True

        notes: list[str] = []
        if execution.notes:
            notes.append(execution.notes)
        if case.require_visual_eval and not visual_scores:
            notes.append("visual eval missing")
        if case.require_preview_images and not preview_images:
            notes.append("preview images missing")
        if execution.content_issue_count > case.max_content_issue_count:
            notes.append(
                f"content issues {execution.content_issue_count} > budget {case.max_content_issue_count}"
            )
        if case.min_average_visual_score > 0 and avg_visual < case.min_average_visual_score:
            notes.append(
                f"average visual score {avg_visual:.2f} < min {case.min_average_visual_score:.2f}"
            )

        passed = (
            execution.generation_succeeded
            and (not case.require_visual_eval or bool(visual_scores))
            and (not case.require_preview_images or bool(preview_images))
            and execution.content_issue_count <= case.max_content_issue_count
            and (case.min_average_visual_score <= 0 or avg_visual >= case.min_average_visual_score)
        )

        return BenchmarkCaseObservation(
            case_id=case.case_id,
            passed=passed,
            generation_succeeded=execution.generation_succeeded,
            regression_detected=regression_detected,
            output_path=execution.output_path,
            artifact_dir=str(artifact_dir),
            preview_image_count=len(preview_images),
            average_visual_score=avg_visual,
            content_issue_count=execution.content_issue_count,
            average_visual_delta=average_visual_delta,
            max_visual_drop=max_visual_drop,
            notes="; ".join(notes),
            harness_trace=dict(execution.harness_trace or {}),
        )

    def _default_case_runner(
        self,
        case: BenchmarkCaseSpec,
        *,
        model_provider: str,
        no_research: bool,
        no_images: bool,
        image_source: str,
    ) -> BenchmarkCaseExecution:
        request = dict(case.request)
        topic = str(request.pop("topic", case.topic or case.case_id))
        output_filename = str(request.pop("output_filename", f"benchmark_{case.case_id}.pptx"))
        bundle = self.pipeline_service.generate_bundle(
            topic=topic,
            output_filename=output_filename,
            language=str(request.pop("language", "中文")),
            min_slides=int(request.pop("min_slides", 6)),
            max_slides=int(request.pop("max_slides", 10)),
            style=str(request.pop("style", "")),
            audience=str(request.pop("audience", "general")),
            content_requirements=str(request.pop("content_requirements", "")),
            debug_layout=bool(request.pop("debug_layout", False)),
            no_research=bool(request.pop("no_research", no_research)),
            no_images=bool(request.pop("no_images", no_images)),
            image_source=str(request.pop("image_source", image_source)),
            model_provider=str(request.pop("model_provider", model_provider)),
        )
        return BenchmarkCaseExecution(
            case_id=case.case_id,
            generation_succeeded=True,
            output_path=bundle.output_path,
            preview_images=list(bundle.preview_images),
            visual_overall_scores=[item.overall for item in bundle.visual_eval_results],
            content_issue_count=len(bundle.content_issues),
            extracted_text=bundle.extracted_text,
            harness_trace=dict(bundle.harness_trace or {}),
        )

    def _persist_case_artifacts(
        self,
        *,
        benchmark_id: str,
        case: BenchmarkCaseSpec,
        execution: BenchmarkCaseExecution,
    ) -> Path:
        artifact_dir = self.artifacts_root / benchmark_id / case.case_id
        artifact_dir.mkdir(parents=True, exist_ok=True)

        summary = {
            "case_id": case.case_id,
            "topic": case.topic,
            "tags": list(case.tags),
            "request": dict(case.request),
            "generation_succeeded": execution.generation_succeeded,
            "output_path": execution.output_path,
            "preview_images": list(execution.preview_images or []),
            "visual_overall_scores": list(execution.visual_overall_scores or []),
            "content_issue_count": execution.content_issue_count,
            "notes": execution.notes,
            "harness_trace": dict(execution.harness_trace or {}),
        }
        (artifact_dir / "case_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        if execution.extracted_text:
            (artifact_dir / "extracted_text.md").write_text(execution.extracted_text, encoding="utf-8")

        if execution.output_path:
            output_path = Path(execution.output_path)
            if output_path.exists():
                target = artifact_dir / output_path.name
                if not target.exists() or target.resolve() != output_path.resolve():
                    shutil.copy2(output_path, target)

                generated_js_dir = output_path.parent / f"{output_path.stem}_generated_js"
                if generated_js_dir.exists() and generated_js_dir.is_dir():
                    copied_js_dir = artifact_dir / generated_js_dir.name
                    if copied_js_dir.exists():
                        shutil.rmtree(copied_js_dir)
                    shutil.copytree(generated_js_dir, copied_js_dir)

        preview_images = execution.preview_images or []
        if preview_images:
            preview_dir = artifact_dir / "preview_images"
            preview_dir.mkdir(parents=True, exist_ok=True)
            for image_path in preview_images:
                source = Path(image_path)
                if not source.exists():
                    continue
                target = preview_dir / source.name
                if not target.exists() or target.resolve() != source.resolve():
                    shutil.copy2(source, target)

        if not execution.generation_succeeded and execution.notes:
            (artifact_dir / "error.txt").write_text(execution.notes + "\n", encoding="utf-8")

        self._update_artifact_index(
            benchmark_id=benchmark_id,
            case=case,
            execution=execution,
            artifact_dir=artifact_dir,
        )
        return artifact_dir

    def _update_artifact_index(
        self,
        *,
        benchmark_id: str,
        case: BenchmarkCaseSpec,
        execution: BenchmarkCaseExecution,
        artifact_dir: Path,
    ) -> None:
        benchmark_root = self.artifacts_root / benchmark_id
        benchmark_root.mkdir(parents=True, exist_ok=True)
        index_path = benchmark_root / "index.json"
        if index_path.exists():
            try:
                index_data = json.loads(index_path.read_text(encoding="utf-8"))
            except Exception:
                index_data = {}
        else:
            index_data = {}

        entries = index_data.get("cases")
        if not isinstance(entries, list):
            entries = []
        entry = {
            "case_id": case.case_id,
            "topic": case.topic,
            "artifact_dir": str(artifact_dir),
            "output_path": execution.output_path,
            "generation_succeeded": execution.generation_succeeded,
            "preview_image_count": len(execution.preview_images or []),
            "visual_score_count": len(execution.visual_overall_scores or []),
            "content_issue_count": execution.content_issue_count,
            "updated_at": self._utc_now_iso(),
        }
        entries = [item for item in entries if item.get("case_id") != case.case_id]
        entries.append(entry)
        index_data = {
            "benchmark_id": benchmark_id,
            "updated_at": self._utc_now_iso(),
            "cases": sorted(entries, key=lambda item: str(item.get("case_id", ""))),
        }
        index_path.write_text(
            json.dumps(index_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _save_observations(self, observations: BenchmarkObservations) -> Path:
        self.observations_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.observations_root / f"{observations.benchmark_id}-{stamp}.json"
        path.write_text(observations.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _save_manifest(
        self,
        manifest: GoldenBenchmarkManifest,
        *,
        output_path: str | Path | None = None,
    ) -> Path:
        if output_path is None:
            manifests_root = config.BENCHMARKS_DIR / "manifests"
            manifests_root.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            output_path = manifests_root / f"{manifest.benchmark_id}-baseline-{stamp}.json"
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return path
