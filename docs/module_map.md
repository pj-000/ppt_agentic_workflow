# Module Map

## Harness Directory

```text
runtime/backend/harness/
  agent_runtime/
  tooling/
  quality/
  observability/
  benchmark/
  memory/
  repair/
  orchestration/
  runtime_integration/
  runtime/
  agents/
```

## Module Table

| Directory | Purpose | Key Classes / Functions | Inputs | Outputs | Online Execution | Offline Analysis | Main Artifacts |
|---|---|---|---|---|---|---|---|
| `agent_runtime/` | Unified agent-like worker contract | `AgentSpec`, `AgentRequest`, `AgentResult`, `AgentRegistry`, `AgentExecutor` | Worker impl, request, context | Structured agent result | Optional adapter use | Yes, with fake agents | `AgentResult` |
| `tooling/` | Unified tool schema and executor | `ToolSpec`, `ToolCall`, `ToolResult`, `ToolRegistry`, `ToolExecutor` | Tool call input | Structured tool result and error signature | Optional tool wrapper use | Yes, with fake tools | `ToolResult` |
| `quality/` | Quality report generation | `QualityReport`, integration helpers | Run artifacts, metrics, issues | JSON and Markdown quality reports | Light integration | Yes | `quality_report.json`, `quality_report.md` |
| `observability/` | Structured trace runtime | `TraceEvent`, `TraceStore`, `ObservabilityTraceAdapter` | Stage records, tool/agent events | Trace JSONL and summaries | Light integration | Yes | `trace.jsonl`, `trace_summary.json`, `trace_summary.md` |
| `benchmark/` | Offline benchmark harness | `BenchmarkCase`, `BenchmarkSuite`, `BenchmarkRunner`, gates | Existing run artifacts and suite JSON | Case and suite reports | No by default | Yes | `benchmark_report.json`, `benchmark_report.md`, `case_results.jsonl` |
| `memory/` | Agent memory facade | `MemoryRecord`, `JsonlMemoryStore`, `AgentMemory` | Memory query/write, run artifacts | JSONL records and memory hits | Optional | Yes | `outputs/memory/...` |
| `repair/` | Offline repair planning | `RepairIssue`, `RepairPlan`, `RepairPlanner`, `RepairExecutor` | Quality report, trace summary, tool errors, memory hits | Repair plan/report | No real repair by default | Yes | `repair_plan.json`, `repair_result.json`, `repair_report.md` |
| `orchestration/` | Deterministic PlanGraph and replanner | `PlanGraph`, `PlanPatch`, `DeterministicReplanner` | Run signals, repair artifacts | Patch proposals and reports | No patch application by default | Yes | `plan_graph.json`, `replan_decision.json`, `replan_report.md` |
| `runtime_integration/` | Post-run bundle integration | `HarnessManifest`, `PostRunHarnessRunner` | Existing run artifacts | Manifest, bundle, summary | Optional helper | Yes | `harness_manifest.json`, `harness_bundle.json`, `harness_summary.md` |
| `runtime/` | Existing runtime support modules | Existing runtime classes | Existing generation inputs | Existing runtime outputs | Yes | Some | Existing runtime artifacts |
| `agents/` | Existing specialist agent-like workers | Planner, Research, Asset, Evaluator | Orchestrator calls | Generation/eval results | Yes | No by default | Worker outputs |

## Reading Order

For a quick code tour:

1. `runtime/backend/harness/tooling/schema.py`
2. `runtime/backend/harness/agent_runtime/schema.py`
3. `runtime/backend/harness/quality/`
4. `runtime/backend/harness/observability/`
5. `runtime/backend/harness/benchmark/metrics.py`
6. `runtime/backend/harness/memory/models.py`
7. `runtime/backend/harness/repair/models.py`
8. `runtime/backend/harness/orchestration/models.py`
9. `runtime/backend/harness/runtime_integration/post_run.py`

## Raw Markdown Guardrail

Keep this module map as real multi-line Markdown.
Each table row should remain on its own line.
Do not collapse the directory map or table into a single compressed paragraph.
