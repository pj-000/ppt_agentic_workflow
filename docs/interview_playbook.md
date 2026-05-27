# Interview Playbook

## Two-Minute Version

This project started from a document-to-PPT generation backend. The challenge is that LLM-generated PPTs are long-chain artifacts: planning, retrieval, image assets, slide code, PPTX assembly, preview rendering, quality evaluation, and repair can all fail in different ways.

I built a PPT Generation Agent Harness around the existing workflow. It is not a fully autonomous multi-agent system. The Orchestrator still controls the flow, while Planner, Research, Asset, and Evaluator are specialist workers. The harness adds AgentRuntime, ToolRuntime, quality reports, observability trace, offline benchmark, memory, repair planning, deterministic replanning, and post-run bundles.

The key engineering value is making complex LLM artifact generation observable, measurable, repairable, and reproducible.

## Ten-Minute Version

1. **Business problem**: LLM PPT generation is unstable because failures can occur in planning, retrieval, asset acquisition, slide code, PPTX generation, preview, and evaluation.
2. **Why not a normal PPT tool**: The output is not a single file transform; it is a multi-stage generation workflow with external tools and quality feedback.
3. **Why not fully autonomous MAS**: Free-form autonomous agent coordination would make failures harder to reproduce. The project uses controlled orchestration and structured artifacts.
4. **Architecture**: Orchestrator + specialist workers + AgentRuntime + ToolRuntime.
5. **Tool execution**: ToolRuntime wraps PptxGenJS, preview, search, document, and evaluation tools with structured `ToolResult`.
6. **Quality**: Quality Harness writes `quality_report.json` and `.md` with metrics, issues, missing reasons, and artifacts.
7. **Observability**: Trace events capture run, phase, agent, tool, QA, repair, memory, artifact, and replan signals.
8. **Benchmark**: Offline Benchmark consumes saved reports and trace summaries to compare run quality without invoking live generation.
9. **Memory**: Memory is split into episodic, semantic, and procedural records using JSONL and lexical retrieval.
10. **Repair**: Repair Harness extracts issues from quality and trace artifacts, then builds a deterministic repair plan.
11. **Replanning**: Deterministic Replanner proposes PlanGraph patches, with risk levels and `auto_apply=False` by default.
12. **Post-run bundle**: Runtime Integration collects artifacts into `harness_manifest.json`, `harness_bundle.json`, and `harness_summary.md`.

## Questions and Answers

### 1. Why not build a fully autonomous multi-agent system?

Because the target problem is reliability of a long artifact-generation chain. A controlled workflow with structured artifacts is easier to test, benchmark, and debug than free-form agent negotiation.

### 2. Are Planner / Research / Asset / Evaluator agents?

They are agent-like specialist workers. The project treats them as workers behind an AgentRuntime contract, not as autonomous peers that negotiate with one another.

### 3. What is the boundary between AgentRuntime and ToolRuntime?

AgentRuntime standardizes worker-level tasks and results. ToolRuntime standardizes lower-level tool calls, retries, timeouts, artifacts, and error signatures.

### 4. How is memory designed?

Memory has three layers: episodic memory for run summaries, semantic memory for reusable stable knowledge, and procedural memory for repair experience. The current implementation uses JSONL and lexical retrieval.

### 5. How do you avoid repair memory pollution?

Memory records include confidence, success/failure counts, lifecycle state, and promotion state. Promotion is conservative and should be tied to benchmark evidence before being trusted broadly.

### 6. How do you prove PPT quality improved?

The project does not claim improvement without benchmark data. Quality reports and offline benchmark reports provide the measurement surface. Actual improvement claims should reference real benchmark results.

### 7. How is Benchmark designed?

Benchmark is offline. It evaluates saved `quality_report.json` and `trace_summary.json` artifacts against a suite of cases, producing case-level and suite-level reports.

### 8. How do you avoid leaking prompt or chain-of-thought in trace?

Trace payloads go through redaction helpers. Sensitive keys such as API keys, authorization, system prompts, hidden reasoning, chain-of-thought, and raw model responses are redacted or truncated.

### 9. Why does the Replanner not use an LLM?

The first replanner is deterministic so patch proposals are auditable, reproducible, and safe by default. LLM-assisted replanning can be explored later behind strict guardrails.

### 10. How would you connect this to the real Orchestrator?

Add an optional post-run hook that is disabled by default, fail-soft, and only runs after core artifacts are written. Then gradually wire ToolRuntime or AgentRuntime into specific low-risk paths while preserving CLI and FastAPI contracts.
