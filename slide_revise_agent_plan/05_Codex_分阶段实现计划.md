# Codex 分阶段实现计划

本文件用于直接指导 Codex 在 `directionai-agent-backend` 仓库中实现 SlideReviseAgent。

每个 Phase 都包含：

```text
目标
需要实现的文件
Codex 任务描述
验收标准
完成后需要交给 ChatGPT 审阅的内容
```

---

## 全局开发要求

Codex 必须遵守：

```text
1. 不破坏现有 ppt_backend/ 和 runtime/ 功能。
2. 新增模块放在 slide_revise_agent/。
3. 所有核心数据结构使用 Pydantic。
4. 所有函数有类型标注。
5. 所有 CLI/脚本有 --help。
6. 每个 Phase 至少提供 smoke test。
7. 每个实验输出必须保存 JSON log。
8. 所有 prompt 放在 slide_revise_agent/prompts/。
9. 所有 benchmark 数据放在 experiments/slide_revise/data/。
10. 所有运行输出放在 experiments/slide_revise/outputs/。
```

---

# Phase 0：项目骨架与依赖检查

## 目标

建立 `slide_revise_agent/` 目录和基础配置。

## 需要实现

```text
slide_revise_agent/__init__.py
slide_revise_agent/configs/default.yaml
slide_revise_agent/index/schema.py
slide_revise_agent/cli.py
experiments/slide_revise/{data,outputs,logs,reports}/.gitkeep
```

## Codex 任务

```text
Create a new independent research module called slide_revise_agent without modifying existing ppt_backend and runtime logic. Add Pydantic schemas for DeckIndex, SlideIndex, ElementIndex, InteractionContext, InteractionIntent, LocalizationResult, EditPlan, EditOperation. Add a minimal CLI entrypoint with subcommands placeholders: build-index, parse-intent, localize, plan, execute, revise.
```

## 验收标准

```text
- python -m slide_revise_agent.cli --help 能运行。
- schema.py 可以被 import。
- 不影响 uv run ppt-backend --help。
- 目录结构完整。
```

## 审阅材料

```text
- 新增目录树
- schema.py
- cli.py
- default.yaml
```

---

# Phase 1：PPT Parser 与基础 DeckIndex

## 目标

输入 PPTX，输出基础 `deck_index.json`。

## 需要实现

```text
slide_revise_agent/parser/ppt_parser.py
slide_revise_agent/parser/ppt_xml_parser.py
slide_revise_agent/parser/text_extractor.py
slide_revise_agent/parser/screenshot_renderer.py
slide_revise_agent/index/builder.py
```

## Codex 任务

```text
Implement a PPTX parser that extracts slide ids, titles, text boxes, bullet lists, basic shapes, images, tables if possible, bounding boxes, font size, and styles. Build SlideIndex and ElementIndex. Output deck_index.json. Use python-pptx for extraction where possible. For screenshots, implement a best-effort renderer using LibreOffice if available; otherwise skip screenshots gracefully.
```

## CLI 示例

```bash
uv run python -m slide_revise_agent.cli build-index \
  --ppt examples/demo.pptx \
  --out experiments/slide_revise/outputs/demo_deck_index.json
```

## 验收标准

```text
- 能解析至少 5 套 PPT。
- 每页有 slide_id、title、text、word_count。
- 每个文本元素有 element_id、slide_id、element_type、text、bbox。
- 如果无法提取 bbox，也要有 graceful fallback。
- 输出 JSON 可被重新加载成 DeckIndex。
```

## 审阅材料

```text
- ppt_parser.py
- 1 个 deck_index.json 样例
- 解析日志
- 遇到无法解析元素的 fallback 说明
```

---

# Phase 2：Slide Semantic Index 与 Element Index 增强

## 目标

让系统知道每页“讲什么”，并支持后续语义定位。

## 需要实现

```text
slide_revise_agent/index/semantic_index.py
slide_revise_agent/index/element_index.py
slide_revise_agent/index/dependency_index.py
```

## Codex 任务

```text
Enhance DeckIndex with slide summaries, key concepts, visual density, section guess, role guess, and optional embeddings. Implement lightweight keyword/keyphrase extraction without requiring model training. Add dependency heuristics: agenda slides, summary slides, section title slides, concept overlap dependencies.
```

## 验收标准

```text
- 每页生成 summary。
- 每页生成 key_concepts。
- 每页 visual_density ∈ {low, medium, high}。
- 能识别可能的目录页、章节页、总结页。
- 能根据概念重叠生成 dependency edges。
```

## 审阅材料

```text
- semantic_index.py
- dependency_index.py
- 1 个增强后的 deck_index.json
- 依赖边示例
```

---

# Phase 3：Interaction Context 与 Intent Parser

## 目标

将用户修改请求解析为结构化意图。

## 需要实现

```text
slide_revise_agent/interaction/context_manager.py
slide_revise_agent/interaction/history_store.py
slide_revise_agent/interaction/reference_resolver.py
slide_revise_agent/interaction/intent_parser.py
slide_revise_agent/prompts/intent_parser.md
```

## 支持意图类型

至少支持：

```text
add_example
expand_content
compress_content
split_slide
merge_slides
delete_content
reorder_content
change_audience_level
change_style
add_diagram
rewrite_text
simplify_language
global_unify
reference_previous_turn
```

## Codex 任务

```text
Implement an intent parser that can work in two modes: rule-based mode and optional LLM mode. It should parse raw user instructions plus interaction context into InteractionIntent. Implement reference resolution for expressions like '这页', '这里', '刚才新增的那页', '前面那部分'. Rule-based mode must work without API keys.
```

## CLI 示例

```bash
uv run python -m slide_revise_agent.cli parse-intent \
  --instruction "这页太满了，拆成两页" \
  --selected-slide 5
```

## 验收标准

```text
- 100 条手写指令可解析。
- intent_type 基本准确。
- explicit_references 可提取。
- candidate_operation_templates 可生成，且使用结构化 operation objects。
- 不使用 LLM 也能运行。
```

## 审阅材料

```text
- intent_parser.py
- reference_resolver.py
- 20 条解析样例
```

---

# Phase 4：Index-guided Slide / Element Localization

## 目标

实现核心定位算法。

## 需要实现

```text
slide_revise_agent/localization/scoring.py
slide_revise_agent/localization/slide_locator.py
slide_revise_agent/localization/element_locator.py
slide_revise_agent/localization/explain.py
```

## 定位信号

```text
ExplicitReferenceScore
SelectionContextScore
SemanticSimilarityScore
ElementRelevanceScore
DependencyScore
InteractionHistoryScore
VisualIssueScore
```

## Codex 任务

```text
Implement scoring-based slide and element localization. Given DeckIndex, InteractionIntent, InteractionContext, and history, return ranked affected slides and elements with confidence and human-readable reasons. Use deterministic scoring and optional embeddings if available. Provide ablation flags to disable each signal.
```

## CLI 示例

```bash
uv run python -m slide_revise_agent.cli localize \
  --index experiments/slide_revise/outputs/demo_deck_index.json \
  --instruction "这里加一个例子" \
  --selected-slide 6
```

## 验收标准

```text
- 能输出 top-k slides。
- 能输出 top-k elements。
- 每个结果有 reason。
- 支持禁用某个信号进行 ablation。
- 对“这页”类请求 selected slide 应排第一。
```

## 审阅材料

```text
- scoring.py
- slide_locator.py
- element_locator.py
- 10 条定位结果样例
```

---

# Phase 5：Edit Planner

## 目标

定位后生成“怎么改”的编辑计划。

## 需要实现

```text
slide_revise_agent/planning/operation_schema.py
slide_revise_agent/index/operation_index.py
slide_revise_agent/planning/rule_policy.py
slide_revise_agent/planning/llm_policy.py
slide_revise_agent/planning/edit_planner.py
slide_revise_agent/prompts/edit_planner.md
```

## Codex 任务

```text
Implement an edit planner that maps InteractionIntent + LocalizationResult + DeckIndex into an EditPlan. Support rule-based planning without API keys and optional LLM-constrained planning. The planner must preserve unaffected slides and explain why each operation is selected.
```

## 验收标准

```text
- “这页太满” → SPLIT_SLIDE or REWRITE_TEXT_BLOCK。
- “这里加例子” → ADD_SLIDE with slide_type="example" and content_intent="add_example", or ADD_TEXT_BLOCK。
- “删掉高级推导” → DELETE_TEXT_BLOCK or DELETE_SLIDE。
- “整体风格统一” → CHANGE_STYLE，并在必要时追加 UPDATE_SUMMARY。
- 输出 EditPlan JSON 可验证。
```

## 审阅材料

```text
- edit_planner.py
- operation_index.py
- 20 条 edit_plan 样例
```

---

# Phase 6：Lightweight PPT Executor

## 目标

实现一个可复现轻量执行器，支持基础局部修改。

## 需要实现

```text
slide_revise_agent/execution/base_executor.py
slide_revise_agent/execution/python_pptx_executor.py
slide_revise_agent/execution/backend_adapter.py
slide_revise_agent/execution/optional_backend_adapter.py  # 可选
```

## 支持操作

MVP 必须支持：

```text
REWRITE_TEXT_BLOCK
ADD_TEXT_BLOCK
ADD_SLIDE
DELETE_SLIDE
REORDER_SLIDE
SPLIT_SLIDE
```

## Codex 任务

```text
Implement a lightweight PPTX executor with python-pptx that can apply basic primitive edit operations. Intent-level requests such as add_example should be represented as ADD_SLIDE or ADD_TEXT_BLOCK with structured parameters. The executor does not need to produce perfect design quality; it must be deterministic, reproducible, and preserve unaffected slides as much as possible. Add an adapter interface so the internal backend can be plugged in later.
```

## CLI 示例

```bash
uv run python -m slide_revise_agent.cli execute \
  --ppt examples/demo.pptx \
  --index experiments/slide_revise/outputs/demo_deck_index.json \
  --plan experiments/slide_revise/outputs/edit_plan.json \
  --out experiments/slide_revise/outputs/demo_revised.pptx
```

## 验收标准

```text
- 修改后 PPTX 可打开。
- 能修改标题/正文。
- 能新增页/删除页。
- 能执行简单拆页。
- 执行后能重新 build index。
```

## 审阅材料

```text
- executor 代码
- before/after PPTX
- execution_log.json
- updated_index.json
```

---

# Phase 7：Dynamic Index Update 与多轮交互

## 目标

支持连续多轮修改。

## 需要实现

```text
slide_revise_agent/index/updater.py
slide_revise_agent/interaction/history_store.py
```

## Codex 任务

```text
After executing an EditPlan, update the DeckIndex: re-parse affected and created slides, update slide ids if ordering changed, update elements, dependencies, and interaction history. Support references such as '刚才新增的那页'. Save versioned index files.
```

## 验收标准

```text
- 连续 3 轮修改后 index 仍然可用。
- 新增页面有新的 slide index 和 element index。
- 删除页面后后续定位不会指向不存在的 slide。
- “刚才新增的那页”可以解析到正确 slide。
```

## 审阅材料

```text
- updater.py
- multi_turn_demo log
- deck_index_v000/v001/v002.json
```

---

# Phase 8：Benchmark Loader 与 Evaluation Scripts

## 目标

支持 SlideReviseBench-ZH 数据集和离线实验。

## 需要实现

```text
slide_revise_agent/benchmark/dataset_schema.py
slide_revise_agent/benchmark/loader.py
slide_revise_agent/benchmark/annotation_tool.py
slide_revise_agent/evaluation/metrics.py
slide_revise_agent/evaluation/run_localization_eval.py
slide_revise_agent/evaluation/run_operation_eval.py
slide_revise_agent/evaluation/run_ablation.py
```

## Codex 任务

```text
Implement dataset schemas and evaluation scripts for slide/element localization and operation prediction. Add metrics: Top-1, Top-k Recall, F1, MRR, Operation Accuracy, Macro-F1, Exact Match. Support ablations that disable context, element, dependency, operation, and history signals.
```

## 验收标准

```text
- 能加载 benchmark JSONL。
- 能跑 baseline 和 ours。
- 能输出 CSV/Markdown 表格。
- 能自动保存评测报告。
```

## 审阅材料

```text
- dataset_schema.py
- metrics.py
- 一份 eval_report.md
- 结果 CSV
```

---

# Phase 9：UI Demo

## 目标

做 IUI 论文和 demo video 用的最小 UI。

前端界面不是论文的核心方法贡献，但为了 IUI 投稿、demo 展示和用户研究，需要实现一个轻量级 research prototype。界面支持上传 PPT、浏览页面缩略图、选择页面/元素、输入自然语言修改请求、展示系统定位结果、编辑计划和修改前后对比。论文主贡献仍然是 Multi-granularity Revision Index 以及 index-guided localization/planning。

## 需要实现

```text
slide_revise_agent/ui_demo/app.py
slide_revise_agent/ui_demo/components.py
```

## UI 功能

```text
1. 上传 PPTX；
2. 展示 slide thumbnails；
3. 选择 slide；
4. 可选选择 element；
5. 输入自然语言修改请求；
6. 显示定位结果；
7. 显示 edit plan；
8. 执行修改；
9. 展示 before/after；
10. 显示解释。
```

## Codex 任务

```text
Implement a minimal demo UI using Streamlit or FastAPI + simple frontend. Prioritize clarity over visual polish. The UI should support a complete before/after local revision demo.
```

## 验收标准

```text
- 能完成一个完整 demo。
- 能录制 3–5 分钟视频。
- 能展示定位解释和编辑计划。
```

---

# Phase 10：论文实验日志与复现包

## 目标

为 IUI 投稿准备匿名补充材料。

## 需要实现

```text
experiments/slide_revise/reproduce.sh
experiments/slide_revise/README.md
experiments/slide_revise/configs/*.yaml
```

## Codex 任务

```text
Create reproducibility scripts that run a small benchmark subset, generate result tables, and produce demo outputs. Ensure paths are anonymized and no private API keys or internal prompts are included.
```

## 验收标准

```text
- 一条命令跑通小规模 benchmark。
- 结果表可复现。
- 不包含隐私数据和 API key。
- 可作为 IUI supplemental material。
```

---

## 开发优先级

必须完成：

```text
P0:
- Schema
- PPT Parser
- Revision Index
- Intent Parser
- Localization
- Edit Planner
- Evaluation scripts
```

强烈建议完成：

```text
P1:
- Lightweight Executor
- Dynamic Index Update
- Benchmark 300+ tasks
- User Study UI
```

有时间再做：

```text
P2:
- Internal backend high-quality revision
- Rich visual checker
- More complex diagram editing
- Advanced UI
```
