# Refactor Guardrails

## Core Rule

Preserve the working PPT generation path. Add harness layers around it before replacing internals.

## Guardrails

- Preserve `OrchestratorAgent`.
- Preserve `PlannerAgent`, `ResearchAgent`, `AssetAgent`, and `EvaluatorAgent`.
- Preserve `RuntimeMemoryStore` and `RepairOrchestrator`.
- Preserve existing CLI entrypoints.
- Preserve FastAPI routes including `/generate_ppt`, `/stream_ppt_outline`, `/stream_ppt_from_outline`, `/stream_evaluate/ppt`, download routes, and preview routes.
- Preserve the current successful generation path.
- Add wrappers, adapters, and facades before replacing internals.
- Prioritize quality metrics before broader architecture changes.
- Keep first-pass replanning deterministic and conservative.
- Gate memory promotion with benchmark evidence.
- Do not expose hidden reasoning, private prompts, API keys, environment variables, or sensitive workflow details in traces or reports.

## Allowed First Moves

- Add documentation that clarifies the Agentic Workflow versus Agent Harness distinction.
- Add null-safe quality collection and reporting.
- Add tests for schema serialization, edge cases, report writing, and failure tolerance.
- Add optional integration hooks that do not block generation when reporting fails.

## Deferred Work

- Do not introduce autonomous agent negotiation.
- Do not add agent names only for resume optics.
- Do not rewrite the planner or orchestrator as a new framework.
- Do not change existing API response contracts unless a later phase explicitly requires it.
- Do not add heavy dependencies for quality reporting in the first pass.
