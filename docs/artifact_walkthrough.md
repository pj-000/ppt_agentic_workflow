# Artifact Walkthrough

This document explains the main artifacts produced or consumed by the harness.
Most artifacts are safe for demos after checking that they do not contain private input text or proprietary content.

| Artifact | Producer | When Generated | Contains | Consumers | Sensitive Data Policy | Demo Friendly |
|---|---|---|---|---|---|---|
| `quality_report.json` | Quality Harness | After generation/evaluation | Run metrics, slide metrics, issues, artifacts, missing reasons | Benchmark, Repair, Memory, Runtime Integration | Should not contain prompts or secrets | Yes, after content review |
| `quality_report.md` | Quality Harness | With JSON report | Human-readable quality summary | Demo, review | Same as quality report | Yes |
| `trace.jsonl` | Observability | During run / tool / agent / harness events | One trace event per line | Trace summary, debugging | Redacted payloads only | Sometimes, usually summarize |
| `trace_summary.json` | Observability | Post trace aggregation | Status, event counts, tool counts, error signatures, artifact refs | Benchmark, Repair, Replanner, Runtime Integration | No raw prompts or full tracebacks | Yes |
| `trace_summary.md` | Observability | With trace summary | Human-readable trace summary | Demo, review | Same as trace summary | Yes |
| `benchmark_report.json` | Benchmark Harness | Offline benchmark run | Suite metrics and case results | Gate, baseline comparison, demo | Uses existing artifact summaries | Yes |
| `benchmark_report.md` | Benchmark Harness | With benchmark JSON | Human-readable benchmark metrics | README/demo updates | Do not add fabricated numbers | Yes |
| `case_results.jsonl` | Benchmark Harness | Offline benchmark run | One benchmark case result per line | Debugging, regression review | No prompts or secrets | Yes |
| `repair_plan.json` | Repair Harness | Post-run repair planning | Repair issues, actions, memory refs, summaries | Repair review, Replanner | Sanitized messages/evidence | Yes |
| `repair_result.json` | Repair Harness | Lightweight/future repair execution | Attempts, resolved/unresolved issues, quality delta | Benchmark, memory promotion | Sanitized messages | Yes if generated |
| `repair_report.md` | Repair Harness | With repair artifacts | Issues, actions, attempts, memory hits | Demo, review | No hidden reasoning | Yes |
| `plan_graph.json` | Replanner | Post-run replanning | Default PPT workflow plan graph | Replan simulation, review | Sanitized metadata | Yes |
| `replan_decision.json` | Replanner | Post-run replanning | Patch proposals, risk levels, evidence | Runtime integration, future optional hooks | Sanitized evidence | Yes |
| `replan_report.md` | Replanner | With replan decision | Plan, patches, evidence, run signals | Demo, review | No raw reports or prompts | Yes |
| `harness_manifest.json` | Runtime Integration | Post-run bundle | Artifact refs, missing artifacts, statuses, generated refs | Demo, README, downstream tooling | Path refs are sanitized | Yes |
| `harness_bundle.json` | Runtime Integration | Post-run bundle | Manifest, refs, IDs, errors, metadata | Demo, automation, future benchmark | Sanitized errors and refs | Yes |
| `harness_summary.md` | Runtime Integration | Post-run bundle | Human-readable run bundle summary | Demo, interview walkthrough | No local absolute paths or secrets | Yes |

## Optional versus Required

Required core artifacts for post-run integration:

- `quality_report.json`
- `trace_summary.json`

Optional artifacts:

- Markdown versions of reports
- repair artifacts
- replan artifacts
- benchmark artifacts
- harness bundle artifacts
- PPTX and preview images

Optional artifacts may be missing on successful runs, especially when no repair or replan is needed.
