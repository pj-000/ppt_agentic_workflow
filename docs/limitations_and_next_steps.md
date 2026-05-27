# Limitations and Next Steps

## Current Limitations

- Orchestrator main flow has not been migrated to `AgentExecutor`.
- Post-run harness is offline integration.
- Repair plans are not automatically executed by default.
- Replan patches are not automatically applied by default.
- Benchmark is offline by default and does not call real generation.
- Memory uses JSONL plus lexical retrieval, not a vector database.
- Semantic memory is not automatically summarized from LLM outputs.
- There is no Trace Viewer UI.
- There is no claimed long-term production dataset in this repository.
- Quantitative improvements should not be claimed without real benchmark reports.

## 当前限制（中文）

- Orchestrator 主流程尚未迁移到 AgentExecutor。
- Post-run harness 是 offline integration。
- Repair plan 默认不自动执行。
- Replan patch 默认不自动应用。
- Benchmark 默认 offline，不调用真实生成。
- Memory 是 JSONL + lexical retrieval，不是 vector DB。
- Semantic memory 不自动从 LLM 总结。
- 没有 Trace Viewer UI。
- 没有真实长期线上数据。
- 没有真实 benchmark 数字时不应写量化提升。

These limits are intentional guardrails.
They keep the project honest for GitHub, resume, and interview review.
They also make it clear which parts are production-path behavior and which parts are offline harness analysis.

## Next Steps

- Add an optional Orchestrator hook that runs post-run harness after generation, disabled by default.
- Gradually route selected tool calls through ToolRuntime in the main chain.
- Add repair result metrics to Benchmark reports.
- Add memory query count and memory hit rate to Benchmark reports.
- Build a simple trace viewer for local debugging.
- Add CI smoke benchmark using offline fixtures.
- Build a real case set and baseline comparison.
- Consider embedding-based semantic memory only after JSONL lexical memory proves insufficient.
- Add controlled execution for low-risk deterministic patches after safety review.

## Suggested Stage Names

- `11_OPTIONAL_ORCHESTRATOR_HOOK`
- `12_BENCHMARK_REPAIR_MEMORY_METRICS`
- `13_TRACE_VIEWER`
- `14_LOW_RISK_PATCH_EXECUTION`

## Documentation Principle

Do not turn these next steps into claims.
Only update the README or resume bullets with measured improvements after a real benchmark report exists.
