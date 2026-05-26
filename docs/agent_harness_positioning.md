# PPT Generation Agent Harness Positioning

## Project Name

PPT Generation Agent Harness

## One-Line Description

An evaluation-and-repair driven Agent Harness for document-to-PPT generation.

## Positioning

This project is not a fully autonomous multi-agent system. It is a controlled agentic workflow executed by a PPT Agent Harness.

The existing generation path is an agentic workflow: it plans a deck, gathers supporting material, prepares assets, generates slide code, assembles a PPTX, evaluates quality, and performs localized repair when needed.

The engineering contribution is the harness around that workflow: runtime contracts, tool execution boundaries, artifact capture, quality measurement, repair control, memory reuse, observability, and benchmark reporting.

## What It Is

- A controlled agentic workflow for document-to-PPT generation.
- Orchestrator-led execution with explicit stage boundaries.
- Specialist agent-like workers for planning, research, assets, evaluation, and repair.
- Tool-augmented generation using document, search, image, PPTX, preview, and evaluation tools.
- A QA and repair loop focused on improving generated PPT quality.
- Memory-assisted repair that can reuse validated lessons conservatively.
- A benchmarked harness that can measure quality and reliability across repeatable cases.

## What It Is Not

- Not a fully autonomous multi-agent system.
- Not free-form agent negotiation, debate, or voting.
- Not a chat-only demo.
- Not a frontend-focused PPT application.
- Not a rewrite of the existing PPT generation backend.

## Why This Matters for PPT Quality

- Stable execution keeps planning, generation, preview, QA, and repair in known stages.
- Measurable quality makes regressions visible before architecture changes.
- Localized repair improves weak slides without regenerating an entire deck.
- Artifact-level observability makes failures diagnosable from outputs, traces, previews, and reports.
- Reusable repair experience helps the system avoid repeating known failure patterns.

## Compatibility Stance

The harness should preserve the existing `OrchestratorAgent`, `PlannerAgent`, `ResearchAgent`, `AssetAgent`, `EvaluatorAgent`, `RuntimeMemoryStore`, `RepairOrchestrator`, CLI entrypoints, and FastAPI routes.

New architecture should be added through adapters, wrappers, and facades before any internal replacement is considered.
