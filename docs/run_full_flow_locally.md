# Run the Full Harness Flow Locally

This guide shows how to run the PPT Generation Agent Harness end to end on a local machine.
It is designed for understanding the harness artifacts, not for changing the production generation path.

## 1. Why the Main Generation Flow Looks Unchanged

This is intentional.

The current project keeps the existing Orchestrator-controlled PPT generation flow compatible.
The Harness is a sidecar, post-run, and offline analysis layer around that flow.
It adds quality reporting, trace summaries, benchmark evaluation, memory writing, repair planning,
deterministic replan proposals, and a final harness bundle.

The Harness does not automatically execute repair actions.
It does not automatically apply replan patches.
It does not turn the project into a fully autonomous multi-agent system.

That means the main PPT generation command can look the same, while the run becomes easier to inspect,
evaluate, repair, and compare.

## 2. Synthetic Demo

Use synthetic mode when you want to inspect the complete Harness flow without API keys,
LLM calls, Node, LibreOffice, or external services.

```bash
uv run python scripts/run_full_harness_flow.py \
  --mode synthetic \
  --run-id demo_synthetic_001 \
  --output-root ./outputs \
  --enable-benchmark
```

This creates synthetic artifacts under:

```text
outputs/runs/demo_synthetic_001/
```

The synthetic core artifacts include:

```text
quality_report.json
quality_report.md
trace.jsonl
trace_summary.json
trace_summary.md
```

Then the post-run Harness generates:

```text
repair_plan.json
repair_report.md
plan_graph.json
replan_decision.json
replan_report.md
harness_manifest.json
harness_bundle.json
harness_summary.md
```

If `--enable-benchmark` is set, it also writes a one-run offline benchmark under:

```text
outputs/benchmarks/{benchmark_id}/benchmark_report.json
outputs/benchmarks/{benchmark_id}/benchmark_report.md
outputs/benchmarks/{benchmark_id}/case_results.jsonl
```

Synthetic demo metrics are only for understanding the pipeline.
Do not use synthetic metrics in a resume or benchmark claim.

## 3. Existing Run Demo

Use existing-run mode after you have already generated a PPT and have a run directory with:

```text
quality_report.json
trace_summary.json
```

Command:

```bash
uv run python scripts/run_full_harness_flow.py \
  --mode existing-run \
  --run-dir ./outputs/runs/<run_id> \
  --output-root ./outputs \
  --enable-benchmark
```

If the required artifacts are missing, the script prints an actionable message and exits cleanly:

```text
Missing quality_report.json or trace_summary.json.
Please run PPT generation first, or use --mode synthetic to inspect the Harness flow without external dependencies.
```

## 4. Latest Run Demo

Use latest-run mode when you want the script to select the newest run under `outputs/runs/`.

```bash
uv run python scripts/run_full_harness_flow.py \
  --mode latest-run \
  --output-root ./outputs \
  --enable-benchmark
```

The script prefers the newest directory that contains `quality_report.json` or `trace_summary.json`.
If no such run exists, use synthetic mode or run generation first.

## 5. Real PPT Generation First

Real PPT generation still uses the existing CLI contract.
Check the current CLI before running real generation:

```bash
uv run ppt-backend --help
```

Example generation command:

```bash
uv run ppt-backend generate \
  --topic "大语言模型微调与对齐" \
  --min-slides 6 \
  --max-slides 8 \
  --image-mode off \
  --output-dir ./outputs
```

Real generation may require API keys, Node.js, PptxGenJS, LibreOffice, or `pdftoppm`,
depending on the options and environment.

After generation, run:

```bash
uv run python scripts/run_full_harness_flow.py \
  --mode latest-run \
  --output-root ./outputs \
  --enable-benchmark
```

The demo script does not implement real generation itself.
It keeps the existing CLI as the source of truth.

## 6. Where to Look

Open these files after a run:

```text
outputs/runs/{run_id}/quality_report.md
outputs/runs/{run_id}/trace_summary.md
outputs/runs/{run_id}/repair_report.md
outputs/runs/{run_id}/replan_report.md
outputs/runs/{run_id}/harness_summary.md
outputs/benchmarks/{benchmark_id}/benchmark_report.md
```

The terminal summary also prints key metrics and artifact paths.

## 7. How to Read Benchmark Metrics

The one-run benchmark is artifact-based.
It reads `quality_report.json` and `trace_summary.json`.

Important fields:

- strict success rate: pass-only case rate.
- acceptable success rate: pass or warning case rate.
- pptx exists rate: how often a PPTX artifact exists.
- preview success rate: how often preview generation succeeded.
- tool call success rate: successful tool attempts divided by total attempts.
- average visual score: average visual quality score.
- minimum visual score: lowest visual quality score.
- content issue count: number of content issues reported by quality evaluation.
- top error signatures: most frequent stable tool or trace error signatures.

Synthetic demo metrics are only for understanding the pipeline.
Do not use synthetic metrics in a resume.
真实简历数字必须来自真实 benchmark cases。

## 8. How to Understand Whether PPT Quality Improved

The Harness does not magically improve PPT quality by itself.
The main generation flow has not been rewritten.

The value of the Harness is that quality problems become visible, diagnosable, repairable,
and measurable across repeated runs.

To claim improvement, run a real benchmark case set before and after a change.
Use real `benchmark_report.md` results, not synthetic demo numbers.

## 9. Safety Boundaries

- Synthetic mode uses fake demo data.
- Existing-run and latest-run modes read existing artifacts.
- Real generation requires the existing `ppt-backend` CLI and real environment setup.
- Benchmark only runs when `--enable-benchmark` is passed.
- Repair plans are generated but not executed.
- Replan patches are proposed but not applied.
- Synthetic metrics should not appear in resumes or production claims.
