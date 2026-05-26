---
name: ppt-generation
description: Deprecated compatibility manifest for the PPT Generation Agent Harness. Keep this file for legacy skill discovery; current development should follow the harness positioning docs.
---

# PPT Generation Skill Compatibility Manifest

This root-level skill manifest is kept for compatibility with older skill
discovery and gateway flows. It no longer defines the project architecture.

Current project positioning:

> This project is not a fully autonomous multi-agent system. It is a controlled
> agentic workflow executed by a PPT Generation Agent Harness.

The active architecture direction is documented in:

- [`docs/agent_harness_positioning.md`](docs/agent_harness_positioning.md)
- [`docs/architecture_overview.md`](docs/architecture_overview.md)
- [`docs/refactor_guardrails.md`](docs/refactor_guardrails.md)

For new implementation work, preserve the existing `OrchestratorAgent`,
`PlannerAgent`, `ResearchAgent`, `AssetAgent`, `EvaluatorAgent`,
`RuntimeMemoryStore`, `RepairOrchestrator`, CLI, and FastAPI contracts.
Add harness capability through adapters, wrappers, and facades rather than
rewriting the generation backend.

This compatibility manifest should not be used as the source of truth for new
phase planning.
