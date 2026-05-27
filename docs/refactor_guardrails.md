# Refactor Guardrails

## Core Rule

Preserve the working PPT generation path. Add harness layers around it before replacing internals.

## Compatibility Guardrails

- Do not rewrite `OrchestratorAgent` for architecture optics.
- Do not rewrite `PlannerAgent`, `ResearchAgent`, `AssetAgent`, or `EvaluatorAgent` just to fit a new abstraction.
- Do not rewrite or delete `RepairOrchestrator`.
- Do not delete `RuntimeMemoryStore` or change the old repair memory file format.
- Do not change existing CLI entrypoints.
- Do not change FastAPI response contracts or existing route semantics.
- Do not add required UI or required API endpoints for harness features.
- Prefer adapters, wrappers, facades, and optional hooks.

## Agent Harness Guardrails

- Do not describe this project as a fully autonomous multi-agent system.
- Do not add agent-to-agent negotiation, debate, voting, or free-form coordination.
- Do not let Planner / Research / Asset / Evaluator "chat" with one another.
- Do not default to automatic repair execution.
- Do not default to applying replan patches.
- Keep deterministic replanning conservative and auditable.

## Data and Safety Guardrails

- Do not write API keys, tokens, passwords, authorization headers, system prompts, hidden reasoning, chain-of-thought, raw model responses, full tracebacks, or environment dumps into trace, memory, reports, benchmark artifacts, or summaries.
- Do not store local absolute paths in public artifacts when a basename or `runs/{run_id}/...` reference is enough.
- Do not store full prompt bundles in memory.
- Do not promote memory without evidence from successful outcomes or benchmark gates.

## Benchmark and Documentation Guardrails

- Do not invent benchmark numbers.
- Do not claim production deployment or real user scale unless backed by evidence.
- Do not state fixed success-rate improvements without a real `benchmark_report`.
- Use TBD placeholders for metrics that have not been measured.
- Distinguish offline benchmark, post-run bundle generation, and live PPT generation.

## Allowed First Moves

- Add internal helpers that are disabled by default.
- Add post-run artifact integration that is fail-soft.
- Add tests for schema serialization, report writing, artifact missing cases, and safety redaction.
- Add documentation and demo runbooks that explain current capabilities and limitations.

## Deferred Work

- Optional Orchestrator hook for automatic post-run bundle generation.
- Partial ToolRuntime wiring into the live generation path.
- Repair execution integration for low-risk actions.
- Benchmark integration with memory hit rate and repair metrics.
- Trace Viewer UI.
