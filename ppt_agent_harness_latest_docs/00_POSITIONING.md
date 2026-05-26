# Phase 0: Positioning and Scope Guardrails

## Goal

Clarify the project positioning before architecture changes.

The correct positioning is:

> **PPT Generation Agent Harness**
>
> A harness that executes and evaluates a controlled agentic workflow for document-to-PPT generation.

The current pipeline is an **Agentic Workflow**. The engineering project should be branded as a **PPT Agent Harness**.

## Why this distinction matters

- **Agentic Workflow** describes the task flow: planning, retrieval, asset preparation, code generation, PPT execution, QA, repair.
- **Agent Harness** describes the engineering layer: runtime protocol, tool execution, artifacts, memory, trace, benchmark, reliability controls.

Use this sentence consistently:

> This project is not a fully autonomous multi-agent system. It is a controlled agentic workflow executed by a PPT Agent Harness.

## Deliverables

Add:

```text
docs/agent_harness_positioning.md
docs/refactor_guardrails.md
docs/architecture_overview.md
```

## Content requirements for `docs/agent_harness_positioning.md`

Include:

1. Project name:
   - `PPT Generation Agent Harness`
2. One-line description:
   - `An evaluation-and-repair driven Agent Harness for document-to-PPT generation.`
3. What it is:
   - controlled agentic workflow
   - orchestrator-led execution
   - specialist agent-like workers
   - tool-augmented generation
   - QA and repair loop
   - memory-assisted repair
   - benchmarked harness
4. What it is not:
   - not fully autonomous MAS
   - not free-form agent negotiation
   - not a chat-only demo
   - not a frontend-focused PPT app
5. Why it matters for PPT quality:
   - stable execution
   - measurable quality
   - localized repair
   - artifact-level observability
   - reusable repair experience

## Content requirements for `docs/architecture_overview.md`

Include this layered architecture:

```text
Application Layer
  CLI / FastAPI / Streaming APIs

Harness Layer
  Orchestrator / PlanGraph
  AgentRuntime
  ToolRuntime
  Artifact Store
  Quality Harness
  Repair Harness
  Memory Harness
  Observability Harness
  Benchmark Harness

Worker Layer
  PlannerAgent
  ResearchAgent
  AssetAgent
  EvaluatorAgent
  RepairOrchestrator

Tool Layer
  LLM client
  web search
  image search/generation
  document extraction
  PptxGenJS execution
  preview rendering
  PPTX text extraction
  content/visual evaluation
```

## Guardrails

Add `docs/refactor_guardrails.md` with:

- Preserve Orchestrator.
- Preserve existing APIs.
- Preserve current successful generation path.
- Add wrappers before replacing internals.
- Quality metrics first, architecture second.
- Replanning should be deterministic in v1.
- Memory promotion must be benchmark-gated.

## Acceptance criteria

- No runtime behavior is changed.
- New docs accurately describe the project.
- The docs explicitly state the Agentic Workflow vs Agent Harness distinction.
- The docs can be used as a stable north star for later phases.
