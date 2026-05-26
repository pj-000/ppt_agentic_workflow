from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from backend.harness.benchmark import (  # noqa: E402
    BenchmarkCase,
    BenchmarkCaseResult,
    BenchmarkExpected,
    BenchmarkGateThresholds,
    BenchmarkReport,
    BenchmarkRunner,
    BenchmarkSuite,
    aggregate_case_results,
    compare_benchmark_reports,
    evaluate_benchmark_gate,
    evaluate_case_from_artifacts,
    load_benchmark_suite,
    run_offline_benchmark_from_suite_path,
    write_benchmark_report,
)


def _case(
    case_id: str = "case_pass",
    *,
    run_id: str | None = "run_pass",
    require_quality_report: bool = True,
    require_trace_summary: bool = True,
    require_preview: bool = False,
    min_slides: int | None = 3,
    max_slides: int | None = 12,
    min_visual_score: float | None = 3.5,
    max_content_issue_count: int | None = 5,
    required_sections: list[str] | None = None,
    expected_keywords: list[str] | None = None,
) -> BenchmarkCase:
    return BenchmarkCase(
        case_id=case_id,
        run_id=run_id,
        expected=BenchmarkExpected(
            min_slides=min_slides,
            max_slides=max_slides,
            required_sections=required_sections or [],
            expected_keywords=expected_keywords or [],
            min_visual_score=min_visual_score,
            max_content_issue_count=max_content_issue_count,
            require_pptx=True,
            require_preview=require_preview,
            require_quality_report=require_quality_report,
            require_trace_summary=require_trace_summary,
        ),
    )


def _write_run_artifacts(
    runs_root: Path,
    run_id: str,
    *,
    quality: dict | None = None,
    trace: dict | None = None,
    write_quality: bool = True,
    write_trace: bool = True,
) -> Path:
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if write_quality:
        (run_dir / "quality_report.json").write_text(
            json.dumps(quality or _quality(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if write_trace:
        (run_dir / "trace_summary.json").write_text(
            json.dumps(trace or _trace(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return run_dir


def _quality(**overrides: object) -> dict:
    run = {
        "run_id": "run_pass",
        "slide_count": 6,
        "pptx_exists": True,
        "preview_success": True,
        "visual_score_avg": 4.2,
        "visual_score_min": 3.8,
        "content_issue_count": 2,
        "repaired_slide_count": 1,
        "repair_attempt_count": 1,
    }
    summary = {"status": "pass", "issue_count": 2, "critical_issue_count": 0}
    missing_reasons = {}
    for key, value in overrides.items():
        if key == "summary":
            summary.update(value)  # type: ignore[arg-type]
        elif key == "missing_reasons":
            missing_reasons.update(value)  # type: ignore[arg-type]
        else:
            run[key] = value
    return {
        "run": run,
        "slides": [],
        "issues": [],
        "summary": summary,
        "artifacts": {"pptx_path": "deck.pptx"},
        "missing_reasons": missing_reasons,
    }


def _trace(**overrides: object) -> dict:
    payload = {
        "run_id": "run_pass",
        "total_events": 8,
        "status": "success",
        "phase_count": 4,
        "tool_call_count": 4,
        "tool_attempt_count": 4,
        "failed_tool_count": 0,
        "skipped_tool_count": 0,
        "timeout_tool_count": 0,
        "error_signatures": [],
        "artifact_refs": {"pptx_path": "deck.pptx"},
        "quality_report_paths": ["quality_report.json"],
    }
    payload.update(overrides)
    return payload


def test_load_benchmark_suite_loads_json(tmp_path: Path) -> None:
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(
        json.dumps(
            {
                "suite_id": "smoke",
                "name": "Smoke Suite",
                "cases": [{"case_id": "case_1", "topic": "AI"}],
            }
        ),
        encoding="utf-8",
    )

    suite = load_benchmark_suite(suite_path)

    assert suite.suite_id == "smoke"
    assert suite.cases[0].case_id == "case_1"


def test_load_benchmark_suite_rejects_duplicate_case_ids(tmp_path: Path) -> None:
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(
        json.dumps({"suite_id": "dup", "cases": [{"case_id": "same"}, {"case_id": "same"}]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Duplicate benchmark case_id"):
        load_benchmark_suite(suite_path)


def test_load_benchmark_suite_missing_file_has_clear_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Benchmark suite file not found"):
        load_benchmark_suite(tmp_path / "missing.json")


def test_evaluate_case_from_artifacts_passes_with_quality_and_trace(tmp_path: Path) -> None:
    run_dir = _write_run_artifacts(tmp_path, "run_pass")

    result = evaluate_case_from_artifacts(_case(), run_dir)

    assert result.status == "pass"
    assert result.quality_report_exists is True
    assert result.trace_summary_exists is True
    assert result.pptx_exists is True
    assert result.preview_success is True
    assert result.slide_count == 6
    assert result.visual_score_avg == 4.2
    assert result.repair_attempt_count == 1


def test_evaluate_case_missing_quality_report_is_missing_artifacts(tmp_path: Path) -> None:
    run_dir = _write_run_artifacts(tmp_path, "run_missing_quality", write_quality=False)

    result = evaluate_case_from_artifacts(_case(run_id="run_missing_quality"), run_dir)

    assert result.status == "missing_artifacts"
    assert "missing quality_report.json" in result.reasons


def test_evaluate_case_missing_trace_summary_is_missing_artifacts(tmp_path: Path) -> None:
    run_dir = _write_run_artifacts(tmp_path, "run_missing_trace", write_trace=False)

    result = evaluate_case_from_artifacts(_case(run_id="run_missing_trace"), run_dir)

    assert result.status == "missing_artifacts"
    assert "missing trace_summary.json" in result.reasons


def test_evaluate_case_invalid_quality_json_object_is_missing_artifacts(tmp_path: Path) -> None:
    run_dir = _write_run_artifacts(tmp_path, "run_invalid_quality", write_quality=False)
    (run_dir / "quality_report.json").write_text("[]", encoding="utf-8")

    result = evaluate_case_from_artifacts(_case(run_id="run_invalid_quality"), run_dir)

    assert result.status == "missing_artifacts"
    assert "invalid quality_report.json: expected object" in result.reasons


def test_evaluate_case_invalid_trace_json_object_is_missing_artifacts(tmp_path: Path) -> None:
    run_dir = _write_run_artifacts(tmp_path, "run_invalid_trace", write_trace=False)
    (run_dir / "trace_summary.json").write_text("[]", encoding="utf-8")

    result = evaluate_case_from_artifacts(_case(run_id="run_invalid_trace"), run_dir)

    assert result.status == "missing_artifacts"
    assert "invalid trace_summary.json: expected object" in result.reasons


def test_evaluate_case_fails_when_required_pptx_missing(tmp_path: Path) -> None:
    run_dir = _write_run_artifacts(tmp_path, "run_no_pptx", quality=_quality(pptx_exists=False))

    result = evaluate_case_from_artifacts(_case(run_id="run_no_pptx"), run_dir)

    assert result.status == "fail"
    assert "required pptx missing" in result.reasons


def test_evaluate_case_missing_slide_count_for_min_slides_is_missing_artifacts(tmp_path: Path) -> None:
    quality = _quality()
    quality["run"].pop("slide_count")
    run_dir = _write_run_artifacts(tmp_path, "run_missing_slide_count", quality=quality)

    result = evaluate_case_from_artifacts(_case(run_id="run_missing_slide_count", min_slides=3), run_dir)

    assert result.status == "missing_artifacts"
    assert "missing required metric run.slide_count" in result.reasons


def test_evaluate_case_missing_visual_score_for_threshold_is_missing_artifacts(tmp_path: Path) -> None:
    quality = _quality()
    quality["run"].pop("visual_score_min")
    run_dir = _write_run_artifacts(tmp_path, "run_missing_visual_score", quality=quality)

    result = evaluate_case_from_artifacts(_case(run_id="run_missing_visual_score", min_visual_score=3.5), run_dir)

    assert result.status == "missing_artifacts"
    assert "missing required metric run.visual_score_min" in result.reasons


def test_evaluate_case_missing_content_issue_count_for_threshold_is_missing_artifacts(tmp_path: Path) -> None:
    quality = _quality()
    quality["run"].pop("content_issue_count")
    run_dir = _write_run_artifacts(tmp_path, "run_missing_content_issue_count", quality=quality)

    result = evaluate_case_from_artifacts(
        _case(run_id="run_missing_content_issue_count", max_content_issue_count=5),
        run_dir,
    )

    assert result.status == "missing_artifacts"
    assert "missing required metric run.content_issue_count" in result.reasons


def test_evaluate_case_missing_pptx_metric_when_required_is_missing_artifacts(tmp_path: Path) -> None:
    quality = _quality()
    quality["run"].pop("pptx_exists")
    run_dir = _write_run_artifacts(tmp_path, "run_missing_pptx_metric", quality=quality)

    result = evaluate_case_from_artifacts(_case(run_id="run_missing_pptx_metric"), run_dir)

    assert result.status == "missing_artifacts"
    assert "missing required metric run.pptx_exists" in result.reasons


def test_evaluate_case_missing_preview_metric_when_required_is_missing_artifacts(tmp_path: Path) -> None:
    quality = _quality()
    quality["run"].pop("preview_success")
    run_dir = _write_run_artifacts(tmp_path, "run_missing_preview_metric", quality=quality)

    result = evaluate_case_from_artifacts(
        _case(run_id="run_missing_preview_metric", require_preview=True),
        run_dir,
    )

    assert result.status == "missing_artifacts"
    assert "missing required metric run.preview_success" in result.reasons


def test_evaluate_case_fails_when_visual_score_below_threshold(tmp_path: Path) -> None:
    run_dir = _write_run_artifacts(tmp_path, "run_low_visual", quality=_quality(visual_score_min=2.8))

    result = evaluate_case_from_artifacts(_case(run_id="run_low_visual"), run_dir)

    assert result.status == "fail"
    assert any("visual_score_min" in reason for reason in result.reasons)


def test_evaluate_case_fails_when_required_section_missing(tmp_path: Path) -> None:
    quality = _quality()
    quality["slides"] = [{"slide_index": 0, "title": "背景与目标"}]
    run_dir = _write_run_artifacts(tmp_path, "run_missing_section", quality=quality)

    result = evaluate_case_from_artifacts(
        _case(run_id="run_missing_section", required_sections=["背景", "核心概念"]),
        run_dir,
    )

    assert result.status == "fail"
    assert "missing required_section: 核心概念" in result.reasons
    assert result.metrics["required_section_coverage"] == 0.5


def test_evaluate_case_passes_when_required_sections_present_in_slide_titles(tmp_path: Path) -> None:
    quality = _quality()
    quality["slides"] = [
        {"slide_index": 0, "title": "背景与目标"},
        {"slide_index": 1, "title": "核心概念"},
    ]
    run_dir = _write_run_artifacts(tmp_path, "run_sections_present", quality=quality)

    result = evaluate_case_from_artifacts(
        _case(run_id="run_sections_present", required_sections=["背景", "核心概念"]),
        run_dir,
    )

    assert result.status == "pass"
    assert result.metrics["required_section_coverage"] == 1.0


def test_evaluate_case_warns_when_required_sections_cannot_be_evaluated(tmp_path: Path) -> None:
    quality = _quality()
    quality["summary"] = {}
    quality["slides"] = []
    quality["issues"] = []
    run_dir = _write_run_artifacts(tmp_path, "run_no_searchable_text", quality=quality)

    result = evaluate_case_from_artifacts(
        _case(run_id="run_no_searchable_text", required_sections=["背景"]),
        run_dir,
    )

    assert result.status == "warning"
    assert "required_sections not evaluated: no searchable text in quality_report" in result.reasons
    assert result.metrics["content_expectations_evaluated"] is False


def test_evaluate_case_fails_when_expected_keyword_missing(tmp_path: Path) -> None:
    quality = _quality()
    quality["slides"] = [{"slide_index": 0, "title": "深度学习基础"}]
    run_dir = _write_run_artifacts(tmp_path, "run_missing_keyword", quality=quality)

    result = evaluate_case_from_artifacts(
        _case(run_id="run_missing_keyword", expected_keywords=["Transformer"]),
        run_dir,
    )

    assert result.status == "fail"
    assert "missing expected_keyword: Transformer" in result.reasons
    assert result.metrics["expected_keyword_coverage"] == 0.0


def test_evaluate_case_warns_for_degraded_tools_and_preserves_signatures(tmp_path: Path) -> None:
    trace = _trace(
        status="warning",
        tool_attempt_count=5,
        failed_tool_count=1,
        skipped_tool_count=1,
        timeout_tool_count=1,
        error_signatures=["ppt.render_preview:TimeoutError:libreoffice_timeout"],
    )
    run_dir = _write_run_artifacts(tmp_path, "run_tool_warning", trace=trace)

    result = evaluate_case_from_artifacts(_case(run_id="run_tool_warning"), run_dir)

    assert result.status == "warning"
    assert result.tool_call_success_rate == 0.4
    assert result.error_signatures == ["ppt.render_preview:TimeoutError:libreoffice_timeout"]
    assert any("failed_tool_count" in reason for reason in result.reasons)
    assert any("skipped_tool_count" in reason for reason in result.reasons)
    assert any("timeout_tool_count" in reason for reason in result.reasons)


def test_degraded_tool_warning_does_not_override_fail(tmp_path: Path) -> None:
    run_dir = _write_run_artifacts(
        tmp_path,
        "run_fail_and_tool_warning",
        quality=_quality(pptx_exists=False),
        trace=_trace(status="warning", skipped_tool_count=1, tool_attempt_count=2),
    )

    result = evaluate_case_from_artifacts(_case(run_id="run_fail_and_tool_warning"), run_dir)

    assert result.status == "fail"
    assert any("required pptx missing" in reason for reason in result.reasons)


def test_aggregate_case_results_computes_suite_metrics() -> None:
    results = [
        BenchmarkCaseResult(
            case_id="pass",
            status="pass",
            quality_report_exists=True,
            trace_summary_exists=True,
            pptx_exists=True,
            preview_success=True,
            visual_score_avg=4.0,
            visual_score_min=3.7,
            content_issue_count=2,
            repair_attempt_count=1,
            tool_attempt_count=4,
            tool_call_success_rate=1.0,
        ),
        BenchmarkCaseResult(
            case_id="fail",
            status="fail",
            quality_report_exists=True,
            trace_summary_exists=True,
            pptx_exists=False,
            preview_success=False,
            visual_score_avg=2.5,
            visual_score_min=2.0,
            content_issue_count=7,
            repair_attempt_count=0,
            tool_attempt_count=2,
            failed_tool_count=1,
            error_signatures=["ppt.run_pptxgenjs:PptxArtifactMissing:file_not_found"],
        ),
        BenchmarkCaseResult(
            case_id="warning",
            status="warning",
            quality_report_exists=True,
            trace_summary_exists=True,
            pptx_exists=True,
            preview_success=False,
            visual_score_avg=3.5,
            visual_score_min=3.2,
            content_issue_count=2,
            repair_attempt_count=2,
            tool_attempt_count=2,
            skipped_tool_count=1,
            error_signatures=["search.image:Skipped:not_implemented"],
        ),
        BenchmarkCaseResult(case_id="missing", status="missing_artifacts"),
    ]

    report = aggregate_case_results(benchmark_id="bench", suite_id="suite", results=results)

    assert report.total_cases == 4
    assert report.passed_cases == 1
    assert report.failed_cases == 1
    assert report.warning_cases == 1
    assert report.missing_artifact_cases == 1
    assert report.end_to_end_success_rate == 0.25
    assert report.acceptable_success_rate == 0.5
    assert report.pptx_exists_rate == 0.6667
    assert report.preview_success_rate == 0.3333
    assert report.quality_report_exists_rate == 0.75
    assert report.trace_summary_exists_rate == 0.75
    assert report.avg_visual_score == 3.3333
    assert report.tool_call_success_rate == 0.75
    assert report.top_error_signatures[0] == ("ppt.run_pptxgenjs:PptxArtifactMissing:file_not_found", 1)


def test_aggregate_case_results_computes_acceptable_success_rate() -> None:
    report = aggregate_case_results(
        benchmark_id="bench",
        suite_id="suite",
        results=[
            BenchmarkCaseResult(case_id="pass", status="pass"),
            BenchmarkCaseResult(case_id="warning", status="warning", reasons=["trace_status warning"]),
            BenchmarkCaseResult(case_id="fail", status="fail"),
            BenchmarkCaseResult(case_id="missing", status="missing_artifacts"),
        ],
    )

    assert report.end_to_end_success_rate == 0.25
    assert report.acceptable_success_rate == 0.5


def test_benchmark_runner_run_offline_writes_reports(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    _write_run_artifacts(runs_root, "run_pass")
    _write_run_artifacts(
        runs_root,
        "run_warning",
        quality=_quality(preview_success=False),
        trace=_trace(status="warning", skipped_tool_count=1, tool_attempt_count=2),
    )
    suite = BenchmarkSuite(
        suite_id="suite",
        cases=[_case("case_pass", run_id="run_pass"), _case("case_warning", run_id="run_warning")],
    )

    report = BenchmarkRunner(output_root=tmp_path, runs_root=runs_root).run_offline(
        suite=suite,
        benchmark_id="bench_test",
    )

    output_dir = tmp_path / "benchmarks" / "bench_test"
    assert report.total_cases == 2
    assert (output_dir / "benchmark_report.json").exists()
    assert (output_dir / "benchmark_report.md").exists()
    assert (output_dir / "case_results.jsonl").exists()


def test_write_benchmark_report_markdown_contains_required_sections(tmp_path: Path) -> None:
    report = aggregate_case_results(
        benchmark_id="bench_report",
        suite_id="suite",
        results=[BenchmarkCaseResult(case_id="case", status="pass")],
    )

    paths = write_benchmark_report(report, tmp_path)
    markdown = Path(paths["markdown_path"]).read_text(encoding="utf-8")

    assert "Benchmark ID" in markdown
    assert "## Core Rates" in markdown
    assert "Acceptable Success Rate" in markdown
    assert "## Quality Metrics" in markdown
    assert "## Tool Metrics" in markdown
    assert "## Case Results" in markdown


def test_benchmark_report_markdown_contains_acceptable_success_rate(tmp_path: Path) -> None:
    report = BenchmarkReport(
        benchmark_id="bench_report",
        suite_id="suite",
        end_to_end_success_rate=0.5,
        acceptable_success_rate=0.75,
    )

    paths = write_benchmark_report(report, tmp_path)
    markdown = Path(paths["markdown_path"]).read_text(encoding="utf-8")

    assert "Strict Success Rate / End-to-end Success Rate" in markdown
    assert "Acceptable Success Rate" in markdown


def test_benchmark_gate_detects_pass_and_failure() -> None:
    passing = BenchmarkReport(
        benchmark_id="passing",
        suite_id="suite",
        status="pass",
        end_to_end_success_rate=0.9,
        acceptable_success_rate=0.95,
        pptx_exists_rate=1.0,
        preview_success_rate=0.8,
        tool_call_success_rate=0.95,
        avg_visual_score=4.1,
        avg_content_issue_count=2,
    )
    failing = passing.model_copy(update={"benchmark_id": "failing", "end_to_end_success_rate": 0.5})
    thresholds = BenchmarkGateThresholds(min_avg_visual_score=3.5, max_avg_content_issue_count=4)

    assert evaluate_benchmark_gate(passing, thresholds).passed is True
    failed_gate = evaluate_benchmark_gate(failing, thresholds)
    assert failed_gate.passed is False
    assert any("end_to_end_success_rate" in reason for reason in failed_gate.reasons)


def test_benchmark_gate_checks_acceptable_success_rate_when_configured() -> None:
    report = BenchmarkReport(
        benchmark_id="bench",
        suite_id="suite",
        end_to_end_success_rate=0.7,
        acceptable_success_rate=0.75,
        pptx_exists_rate=1.0,
        preview_success_rate=1.0,
        tool_call_success_rate=1.0,
    )
    thresholds = BenchmarkGateThresholds(
        min_end_to_end_success_rate=0.5,
        min_acceptable_success_rate=0.8,
    )

    result = evaluate_benchmark_gate(report, thresholds)

    assert result.passed is False
    assert any("acceptable_success_rate" in reason for reason in result.reasons)


def test_compare_benchmark_reports_computes_deltas_and_regressions() -> None:
    baseline = BenchmarkReport(
        benchmark_id="baseline",
        suite_id="suite",
        end_to_end_success_rate=0.8,
        acceptable_success_rate=0.85,
        pptx_exists_rate=0.9,
        preview_success_rate=0.7,
        tool_call_success_rate=0.9,
        avg_visual_score=3.8,
        avg_content_issue_count=3,
        failed_tool_count=1,
        timeout_tool_count=0,
    )
    current = baseline.model_copy(
        update={
            "benchmark_id": "current",
            "end_to_end_success_rate": 0.9,
            "acceptable_success_rate": 0.95,
            "failed_tool_count": 2,
            "avg_content_issue_count": 2,
        }
    )

    comparison = compare_benchmark_reports(current, baseline)

    assert comparison.deltas["end_to_end_success_rate"] == 0.1
    assert comparison.deltas["acceptable_success_rate"] == 0.1
    assert comparison.deltas["failed_tool_count"] == 1.0
    assert any("end_to_end_success_rate" in item for item in comparison.improvements)
    assert any("failed_tool_count" in item for item in comparison.regressions)


def test_compare_benchmark_reports_includes_acceptable_success_rate_delta() -> None:
    baseline = BenchmarkReport(
        benchmark_id="baseline",
        suite_id="suite",
        acceptable_success_rate=0.7,
    )
    current = BenchmarkReport(
        benchmark_id="current",
        suite_id="suite",
        acceptable_success_rate=0.8,
    )

    comparison = compare_benchmark_reports(current, baseline)

    assert comparison.deltas["acceptable_success_rate"] == 0.1
    assert any("acceptable_success_rate" in item for item in comparison.improvements)


def test_run_offline_benchmark_from_suite_path_helper(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    _write_run_artifacts(runs_root, "run_pass")
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(
        json.dumps(
            {
                "suite_id": "suite_helper",
                "cases": [{"case_id": "case_pass", "run_id": "run_pass"}],
            }
        ),
        encoding="utf-8",
    )

    report = run_offline_benchmark_from_suite_path(
        suite_path=suite_path,
        output_root=tmp_path,
        runs_root=runs_root,
        benchmark_id="bench_helper",
    )

    assert report.benchmark_id == "bench_helper"
    assert report.total_cases == 1
    assert (tmp_path / "benchmarks" / "bench_helper" / "benchmark_report.json").exists()
