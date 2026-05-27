# Benchmark Guide

## What Offline Benchmark Means

The Benchmark Harness evaluates existing run artifacts. It does not default to running real PPT generation, LLM calls, search APIs, Node, LibreOffice, or preview rendering.

This is useful for regression checks because benchmark cases can be rerun over saved `quality_report.json` and `trace_summary.json` artifacts.

## Inputs

- `outputs/runs/{run_id}/quality_report.json`
- `outputs/runs/{run_id}/trace_summary.json`
- Benchmark suite JSON, for example `runtime/backend/harness/benchmark/datasets/smoke_cases.json`

## Outputs

- `outputs/benchmarks/{benchmark_id}/benchmark_report.json`
- `outputs/benchmarks/{benchmark_id}/benchmark_report.md`
- `outputs/benchmarks/{benchmark_id}/case_results.jsonl`

## Metric Definitions

| Metric | Meaning |
|---|---|
| Strict Success Rate | `pass` cases divided by total cases. |
| Acceptable Success Rate | `pass + warning` cases divided by total cases. |
| PPTX Exists Rate | Cases where the quality report says a PPTX artifact exists. |
| Preview Success Rate | Cases where preview succeeded when measured. |
| Quality Report Exists Rate | Cases with readable `quality_report.json`. |
| Trace Summary Exists Rate | Cases with readable `trace_summary.json`. |
| Avg Visual Score | Average visual score across cases with available visual metrics. |
| Avg Content Issue Count | Average content issue count across cases with available content metrics. |
| Tool Call Success Rate | Successful tool attempts divided by total tool attempts. |
| Top Error Signatures | Most frequent stable error signatures across cases. |

## Do Not Invent Numbers

Do not put benchmark numbers in README or resume materials unless they come from an actual `benchmark_report`. If no benchmark has been run, use TBD placeholders.

## README Metric Template

| Metric | Baseline | Current | Delta |
|---|---:|---:|---:|
| Strict Success Rate | TBD | TBD | TBD |
| Acceptable Success Rate | TBD | TBD | TBD |
| Preview Success Rate | TBD | TBD | TBD |
| Tool Call Success Rate | TBD | TBD | TBD |
| Avg Visual Score | TBD | TBD | TBD |

## How to Read a Benchmark Report

Start with:

1. `status`
2. strict success rate
3. acceptable success rate
4. missing artifact cases
5. failed / skipped / timeout tool counts
6. top error signatures
7. per-case reasons in `case_results.jsonl`

For interviews, explain the benchmark as an artifact-driven regression harness rather than a claim of production-grade evaluation.

## Raw Markdown Guardrail

Keep benchmark templates and metric lists as real multi-line Markdown.
Do not replace TBD placeholders with numbers unless they come from a real benchmark report.
