from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from backend.harness.quality import QualityCollector, QualityIssue, QualityReport, write_quality_report  # noqa: E402
from backend.harness.quality.models import RunQualityMetrics, SlideQualityMetrics  # noqa: E402


def test_quality_report_schema_round_trips_json() -> None:
    issue = QualityIssue(
        issue_id="content_qa:0:0",
        issue_type="content_issue",
        severity="warning",
        slide_index=0,
        message="Slide text is too short.",
        evidence={"text_length": 12},
        suggested_fix="Add more supporting detail.",
        source="content_qa",
    )
    slide = SlideQualityMetrics(
        slide_index=0,
        visual_score=3.2,
        layout_score=3.1,
        content_score=3.4,
        design_score=3.0,
        issue_count=1,
        issues=[issue],
    )
    run = RunQualityMetrics(
        run_id="run_schema",
        topic="schema test",
        slide_count=1,
        pptx_exists=True,
        pptx_path="/tmp/example.pptx",
        preview_success=True,
        preview_image_count=1,
        extracted_text_length=42,
        content_issue_count=1,
        visual_score_avg=3.2,
        visual_score_min=3.2,
        repaired_slide_count=0,
        repair_attempt_count=0,
        tool_error_count=0,
        total_latency_ms=None,
        stage_latency_ms={},
        created_at="2026-05-26T00:00:00+00:00",
    )
    report = QualityReport(run=run, slides=[slide], issues=[issue], summary={"status": "warning"})

    loaded = QualityReport.model_validate_json(report.model_dump_json())

    assert loaded.run.run_id == "run_schema"
    assert loaded.slides[0].issues[0].source == "content_qa"


def test_collector_is_null_safe_for_missing_artifacts() -> None:
    report = QualityCollector().collect(
        run_id="run_missing",
        topic=None,
        pptx_path=None,
        preview_images=None,
        extracted_text=None,
        visual_eval_results=None,
        content_issues=None,
        repair_events=None,
        tool_errors=None,
        stage_latency_ms=None,
    )

    assert report.run.pptx_exists is False
    assert report.run.preview_success is False
    assert report.run.preview_image_count == 0
    assert report.run.slide_count is None
    assert report.run.stage_latency_ms == {}
    assert report.summary["status"] == "critical"
    assert report.issues[0].source == "pptx_parse"


def test_collector_writes_json_and_markdown_reports(tmp_path: Path) -> None:
    pptx_path = tmp_path / "deck.pptx"
    pptx_path.write_bytes(b"fake pptx marker")

    report = QualityCollector(low_score_threshold=3.5).collect(
        run_id="run_artifacts",
        topic="quality harness",
        pptx_path=str(pptx_path),
        preview_images=["slide_1.png", "slide_2.png"],
        extracted_text="hello deck",
        visual_eval_results=[
            {
                "slide_index": 0,
                "layout_score": 2.9,
                "content_score": 3.1,
                "design_score": 3.0,
                "overall": 3.0,
                "issues": ["Low contrast"],
                "suggestions": ["Increase text contrast"],
            }
        ],
        content_issues=[
            {
                "slide_index": 1,
                "issues": ["Missing local topic support"],
                "suggestions": ["Add evidence from source material"],
            }
        ],
        repair_events=[
            {"event": "slide_revision_start", "slide_index": 0, "overall": 3.0},
            {"event": "slide_revision_done", "slide_indices": [0]},
        ],
        tool_errors=[{"stage": "search", "message": "timeout"}],
        stage_latency_ms={"preview": "12", "visual_qa": 30},
    )
    paths = write_quality_report(report, tmp_path)

    json_path = Path(paths["json_path"])
    markdown_path = Path(paths["markdown_path"])
    loaded = QualityReport.model_validate_json(json_path.read_text(encoding="utf-8"))

    assert loaded.run.slide_count == 2
    assert loaded.run.repaired_slide_count == 1
    assert loaded.run.repair_attempt_count == 1
    assert loaded.slides[0].repaired is True
    assert loaded.slides[0].before_repair_score == 3.0
    assert loaded.slides[0].after_repair_score == 3.0
    assert loaded.run.total_latency_ms == 42
    assert markdown_path.exists()
    assert "## Slide-Level Table" in markdown_path.read_text(encoding="utf-8")


def test_tool_error_evidence_is_json_safe(tmp_path: Path) -> None:
    report = QualityCollector().collect(
        run_id="run_json_safe",
        topic="json safe",
        pptx_path=None,
        preview_images=None,
        extracted_text=None,
        visual_eval_results=None,
        content_issues=None,
        repair_events=None,
        tool_errors=[{"stage": "preview", "error": RuntimeError("renderer failed")}],
        stage_latency_ms=None,
    )

    paths = write_quality_report(report, tmp_path)
    loaded = QualityReport.model_validate_json(Path(paths["json_path"]).read_text(encoding="utf-8"))

    tool_issue = next(issue for issue in loaded.issues if issue.source == "tool")
    assert tool_issue.evidence["error"] == "renderer failed"
