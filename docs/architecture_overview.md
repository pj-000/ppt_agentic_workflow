# Architecture Overview

## North Star

This project is not a fully autonomous multi-agent system. It is a controlled agentic workflow executed by a PPT Agent Harness.

The harness exists to make PPT generation more reliable, measurable, repairable, observable, and benchmarkable while preserving the existing backend.

## Layered Architecture

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

## Current Preservation Boundary

The current `ppt_backend/` and `runtime/` behavior remains the compatibility baseline. The harness should call into existing workers and tools through thin integration points, then collect artifacts and reports around the run.

## Phase Order

1. Positioning and scope guardrails.
2. Quality Harness.
3. Agent Runtime.
4. Tool Runtime.
5. Observability.
6. Memory and Repair Harnesses.
7. Benchmark Harness.
8. Conservative deterministic replanning.

Quality measurement comes before deeper runtime changes so later refactors can be judged by PPT quality and reliability, not by architectural appearance.
