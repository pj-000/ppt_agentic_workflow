# Resume Alignment

## 中文项目名

PPT Generation Agent Harness｜面向文档到 PPT 生成的 Agent 执行框架

## 一句话

围绕文档到 PPT 生成任务，构建 Agent Harness 工程框架，统一 agent runtime、tool runtime、质量评估、trace、benchmark、memory、repair 和 deterministic replanning，使复杂 LLM 产物生成链路可观测、可评估、可修复、可迭代。

## 简历 Bullet 中文版

- 设计 PPT Generation Agent Harness，将文档到 PPT 生成链路抽象为受控 Agentic Workflow，而非完全自治多 Agent 系统。
- 抽象 AgentRuntime，将 Planner / Research / Asset / Evaluator 适配为统一的 `AgentSpec` / `AgentResult` / `AgentExecutor`。
- 构建 ToolRuntime / ToolRegistry，统一 PptxGenJS、preview、search、document、eval 工具调用、timeout、retry 和 `error_signature`。
- 实现 Quality + Observability，输出 run-level / slide-level quality report 和结构化 trace，支持失败复盘。
- 构建 offline Benchmark Harness，统计 strict / acceptable success rate、tool success rate、missing artifacts 和 error signatures。
- 设计 Memory Harness，支持 episodic / semantic / procedural memory，并兼容旧 repair memory。
- 构建 Repair Harness + Deterministic Replanner，从 quality / trace / tool errors / memory hits 生成 repair plan 和 replan patch proposal。

## English Resume Bullets

- Built a PPT Generation Agent Harness that models document-to-PPT generation as a controlled Agentic Workflow rather than a fully autonomous multi-agent system.
- Designed a unified AgentRuntime contract for planner, research, asset, and evaluator workers using structured specs, requests, results, metrics, and errors.
- Implemented a ToolRuntime / ToolRegistry layer for PptxGenJS, preview rendering, search, document processing, and evaluation tools with retry, timeout, and stable error signatures.
- Added Quality and Observability harnesses that generate run-level quality reports and structured trace summaries for post-run diagnosis.
- Built an offline Benchmark Harness to evaluate existing run artifacts using strict success rate, acceptable success rate, tool success rate, missing artifacts, and error signature distributions.
- Designed a layered Memory Harness with episodic, semantic, and procedural records while preserving compatibility with legacy repair memory.
- Implemented Repair and Deterministic Replanner layers that convert quality reports, trace summaries, tool errors, and memory hits into repair plans and auditable patch proposals.

## Do Not Say

- "Built a fully autonomous multi-agent system."
- "Agents autonomously negotiate with each other."
- "Deployed at large production scale" unless there is evidence.
- "Improved success rate by X%" unless backed by a real benchmark report.
- "Uses vector database long-term memory" unless that implementation exists.
- "Automatically executes repair and replan patches" unless that integration is actually enabled.

## Quantitative Template

在 N 个 offline benchmark cases 上：

- Strict success rate: TBD -> TBD
- Acceptable success rate: TBD -> TBD
- Tool call success rate: TBD
- Avg visual score: TBD
- Top error signature count reduced by TBD

Fill these only after running a real benchmark and checking the generated report.
