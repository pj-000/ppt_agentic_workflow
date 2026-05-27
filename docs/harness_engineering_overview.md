# Harness Engineering Overview

## What Harness Means Here

In this project, a harness is not a UI and not a thin wrapper around an LLM call. It is the engineering boundary that makes a long document-to-PPT generation workflow controlled, inspectable, and reproducible.

The harness covers:

- agent execution contracts
- tool execution contracts
- run state and artifacts
- quality reports
- trace events
- offline benchmark reports
- memory records
- repair planning
- deterministic replanning proposals
- post-run bundles

## Why It Is Not a Fully Autonomous MAS

The Orchestrator remains the main controller. Planner, Research, Asset, and Evaluator are specialist worker components. They do not freely negotiate, vote, or autonomously reassign work. The workflow passes structured artifacts between stages, and harness modules inspect or adapt those artifacts.

This is deliberate. PPT generation is a long chain with brittle external tools, file artifacts, preview rendering, evaluation, and repair. A controlled workflow is easier to debug and benchmark than an unconstrained multi-agent conversation.

## Why It Is Agentic Workflow

The workflow is still agentic because it includes:

- outline planning
- tool use
- external retrieval
- image and asset handling
- slide code generation
- quality evaluation
- repair planning
- memory reuse
- deterministic replan proposals

The distinction is that agentic behavior happens inside a bounded, auditable workflow.

## Harness Layers

| Layer | Engineering Boundary |
|---|---|
| AgentRuntime | Normalizes worker execution into specs, requests, context, results, metrics, and errors. |
| ToolRuntime | Normalizes tool calls, retries, timeouts, artifacts, metrics, and error signatures. |
| Quality | Converts generated artifacts and evaluations into structured quality reports. |
| Observability | Records trace events and aggregates run summaries. |
| Benchmark | Compares run artifacts across offline cases. |
| Memory | Stores episodic, semantic, and procedural records without requiring a vector database. |
| Repair | Converts quality and trace failures into structured repair plans. |
| Replanner | Creates deterministic patch proposals over a PlanGraph. |
| Runtime Integration | Collects post-run outputs into manifest, bundle, and summary artifacts. |

## Practical Value

Harness Engineering makes it possible to answer questions like:

- Which tool failed, and is the error signature stable?
- Did this run produce a PPTX, preview images, quality report, and trace summary?
- Which quality metrics are missing?
- Which repair actions are safe to review?
- Does the benchmark show a regression?
- Is a memory record worth keeping or promoting?
- Which artifacts should be shown in a demo?
