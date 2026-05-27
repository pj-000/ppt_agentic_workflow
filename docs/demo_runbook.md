# Demo Runbook

This runbook is for project demos, interviews, and local artifact walkthroughs. Offline harness steps do not require real API keys.

## 1. Generate a PPT

```bash
uv run ppt-backend generate \
  --topic "大语言模型微调与对齐" \
  --min-slides 6 \
  --max-slides 8 \
  --image-mode off \
  --output-dir ./outputs
```

If external providers are not configured, use existing synthetic run artifacts or tests for the offline harness walkthrough.

## 2. Find the Run ID

Look under:

```text
outputs/runs/
```

The run directory is usually:

```text
outputs/runs/{run_id}/
```

## 3. Inspect Quality

Open:

```text
outputs/runs/{run_id}/quality_report.md
outputs/runs/{run_id}/quality_report.json
```

Explain:

- PPTX existence
- preview success
- slide count
- visual score if available
- content issue count
- missing metrics and reasons

## 4. Inspect Trace

Open:

```text
outputs/runs/{run_id}/trace_summary.md
outputs/runs/{run_id}/trace_summary.json
```

Explain:

- run status
- phase count
- tool call count
- failed/skipped/timeout tool counts
- error signatures
- artifact refs

## 5. Run the Post-run Harness Helper

There is no required CLI for this internal helper yet. Use a Python snippet from a runtime-aware environment:

```python
from backend.harness.runtime_integration import (
    build_default_post_run_config,
    run_post_generation_harness,
)

result = run_post_generation_harness(
    run_id="your_run_id",
    run_dir="outputs/runs/your_run_id",
    output_root="outputs",
    config=build_default_post_run_config(),
)
print(result.status)
```

Expected outputs:

```text
outputs/runs/{run_id}/harness_manifest.json
outputs/runs/{run_id}/harness_bundle.json
outputs/runs/{run_id}/harness_summary.md
```

## 6. Inspect the Harness Summary

Open:

```text
outputs/runs/{run_id}/harness_summary.md
```

Use it as the demo entry point because it links the run status, required artifacts, optional artifacts, generated artifacts, missing artifacts, errors, and next suggested actions.

## 7. Run Offline Benchmark

The benchmark runner is currently an internal Python API. It reads existing artifacts.

```python
from backend.harness.benchmark.cases import load_benchmark_suite
from backend.harness.benchmark.runner import BenchmarkRunner

suite = load_benchmark_suite("runtime/backend/harness/benchmark/datasets/smoke_cases.json")
report = BenchmarkRunner(output_root="outputs").run_offline(suite=suite)
print(report.status)
```

Do not present benchmark numbers until you have a real `benchmark_report.md`.

## 8. How to Explain a Failure Chain

Example narrative:

1. A tool fails or skips, such as preview rendering or search.
2. ToolRuntime captures structured status and `error_signature`.
3. Observability records the tool and run status in trace summary.
4. Quality Harness reports missing metrics or degraded quality.
5. Repair Harness extracts a `RepairIssue` and creates a `RepairPlan`.
6. Memory Harness may retrieve procedural repair experience.
7. Deterministic Replanner proposes a low/medium/high risk patch.
8. Runtime Integration writes a harness summary for review.
9. Offline Benchmark counts the failure pattern across cases.

This demonstrates controlled Agent Harness Engineering rather than autonomous agent improvisation.

## Raw Markdown Guardrail

Keep demo commands, Python snippets, and numbered walkthroughs on separate lines.
The runbook should remain readable in both GitHub rendered view and raw Markdown view.
