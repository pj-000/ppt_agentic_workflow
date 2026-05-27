# Agent Harness Positioning

## Name

PPT Generation Agent Harness

## Positioning Statement

This project is not a fully autonomous multi-agent system. It is a controlled Agentic Workflow executed by a PPT Generation Agent Harness.

本项目不是完全自治式多 Agent 系统，而是一个面向文档到 PPT 生成任务的主控式 Agentic Workflow。Orchestrator 负责主流程，Planner / Research / Asset / Evaluator 是专职 agent-like workers，Harness 负责统一执行边界、工具调用、质量评估、trace、benchmark、memory、repair 和 deterministic replanning。

## Why the Distinction Matters

- A fully autonomous multi-agent system usually implies free-form negotiation, autonomous delegation, or agent-to-agent coordination.
- This project keeps orchestration centralized and auditable.
- Workers exchange structured artifacts, not free-form hidden conversations.
- The engineering value is in reliability, measurement, repairability, and reproducibility around PPT generation.

## Implemented Harness Layers

| Layer | Role |
|---|---|
| AgentRuntime | Normalizes agent-like worker execution into `AgentResult`. |
| ToolRuntime | Normalizes tool calls, retries, timeouts, and error signatures. |
| Quality Harness | Produces quality reports for run and slide inspection. |
| Observability | Writes structured trace events and trace summaries. |
| Benchmark | Evaluates existing run artifacts offline. |
| Memory | Stores episodic, semantic, and procedural memory in JSONL. |
| Repair | Builds deterministic repair issues, plans, and reports. |
| Replanner | Proposes deterministic PlanGraph patches without auto-applying them. |
| Runtime Integration | Bundles post-run artifacts into manifest, bundle, and summary. |

## Resume Boundary

Good wording:

- "Built a PPT Generation Agent Harness for controlled document-to-PPT workflows."
- "Unified agent execution, tool execution, quality reporting, trace, benchmark, memory, repair, and deterministic replanning."
- "Designed offline benchmark and post-run bundle artifacts for reproducibility."

Avoid wording:

- "Built a fully autonomous multi-agent system."
- "Agents autonomously negotiate and coordinate."
- "Automatically repairs and replans every PPT run in production."
- "Improved success rate by a fixed percentage" unless backed by a real benchmark report.

## Compatibility Boundary

The harness should preserve the existing `OrchestratorAgent`, `PlannerAgent`, `ResearchAgent`, `AssetAgent`, `EvaluatorAgent`, `RuntimeMemoryStore`, `RepairOrchestrator`, CLI entrypoints, and FastAPI routes. New behavior should be introduced through adapters, wrappers, optional hooks, and post-run helpers before any deeper replacement is considered.
