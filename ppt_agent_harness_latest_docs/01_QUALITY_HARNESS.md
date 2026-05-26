# Phase 1: Quality Harness

## Goal

Make PPT quality measurable before changing architecture.

This phase should answer:

- Did the PPT generate successfully?
- Are the slides readable?
- Which slides are low quality?
- Did QA or repair improve them?
- What failed: planning, tools, preview, content QA, visual QA, repair?

Do not change the main generation logic yet.

## Add modules

```text
runtime/backend/harness/quality/
  __init__.py
  models.py
  collector.py
  issues.py
  report.py
  thresholds.py
```

## Required models

### `QualityIssue`

Fields:

```python
issue_id: str
issue_type: str
severity: Literal["info", "warning", "error", "critical"]
slide_index: int | None
message: str
evidence: dict[str, Any]
suggested_fix: str | None
source: Literal["content_qa", "visual_qa", "preview", "pptx_parse", "tool", "repair", "system"]
```

### `SlideQualityMetrics`

Fields:

```python
slide_index: int
title: str | None
text_length: int | None
has_image: bool | None
has_chart_or_diagram: bool | None
visual_score: float | None
layout_score: float | None
content_score: float | None
design_score: float | None
issue_count: int
issues: list[QualityIssue]
repaired: bool
repair_attempts: int
before_repair_score: float | None
after_repair_score: float | None
```

### `RunQualityMetrics`

Fields:

```python
run_id: str
topic: str | None
slide_count: int | None
pptx_exists: bool
pptx_path: str | None
preview_success: bool
preview_image_count: int
extracted_text_length: int | None
content_issue_count: int
visual_score_avg: float | None
visual_score_min: float | None
repaired_slide_count: int
repair_attempt_count: int
tool_error_count: int
total_latency_ms: int | None
stage_latency_ms: dict[str, int]
created_at: str
```

### `QualityReport`

Fields:

```python
run: RunQualityMetrics
slides: list[SlideQualityMetrics]
issues: list[QualityIssue]
artifacts: dict[str, str]
missing_reasons: dict[str, str]
summary: dict[str, Any]
```

## Collector behavior

`QualityCollector` should accept available artifacts and be null-safe:

```python
class QualityCollector:
    def collect(
        self,
        *,
        run_id: str,
        topic: str | None,
        pptx_path: str | None,
        preview_images: list[str] | None,
        extracted_text: str | None,
        visual_eval_results: list[Any] | None,
        content_issues: list[Any] | None,
        repair_events: list[Any] | None,
        tool_errors: list[Any] | None,
        stage_latency_ms: dict[str, int] | None,
        artifacts: dict[str, str] | None = None,
        missing_reasons: dict[str, str] | None = None,
    ) -> QualityReport:
        ...
```

If a value is missing, fill `None`, `False`, `0`, or `[]` for the metric value and add the reason to `missing_reasons`. Do not fail the generation run because quality collection is incomplete.

The live finalization hook must not pass placeholder empty values as if they were collected. Until ToolRuntime and stage latency collection are implemented, report at least:

```python
missing_reasons = {
    "tool_errors": "ToolRuntime not implemented yet",
    "stage_latency_ms": "not available from current HarnessRunState",
}
```

`summary` must always include:

```python
status: str
issue_count: int
critical_issue_count: int
low_quality_slide_indices: list[int]
missing_metric_keys: list[str]
```

## Output files

For each run, write:

```text
outputs/runs/{run_id}/quality_report.json
outputs/runs/{run_id}/quality_report.md
```

The markdown report should include:

1. Run summary.
2. Overall quality status.
3. Artifacts.
4. Missing metrics.
5. Slide-level table.
6. Top quality issues.
7. Repair summary.
8. Tool errors.
9. Suggested next debugging steps.

## Integration points

Integrate after:

- PPTX assembly
- preview rendering
- content QA
- visual QA
- repair loop
- finalization

The first implementation can be called at finalization time using already available artifacts.

## Acceptance criteria

- Existing CLI works.
- Existing FastAPI works.
- A generated PPT run produces `quality_report.json` and `quality_report.md`.
- Quality report includes run-level and slide-level metrics.
- Report can identify low-quality slides.
- No agent logic is deleted.

## What not to do

- Do not block PPT generation if quality report creation fails.
- Do not invent visual scores when evaluator results are unavailable.
- Do not add heavy dependencies.
- Do not rewrite QA logic in this phase.
