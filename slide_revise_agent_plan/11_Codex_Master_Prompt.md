# Codex Master Prompt

下面这段可以直接交给 Codex，作为整个项目的总指令。

---

## Master Prompt

You are working in the repository:

```text
https://github.com/pj-000/directionai-agent-backend
```

This repository currently contains a standalone PPT generation backend with:

```text
ppt_backend/   CLI and FastAPI entrypoints
runtime/       original PPT generation runtime, prompts, vendor skills, and workspace
outputs/       PPT output directory
```

Your task is to add a new independent research module called:

```text
slide_revise_agent/
```

Do not break or rewrite the existing `ppt_backend/` and `runtime/` logic unless
explicitly asked. The existing PPT backend should be treated as an optional
backend adapter. The research contribution is not one-shot PPT generation. The
new module is for:

> SlideReviseAgent: Index-Guided Interactive Local Revision for Presentation Slides.

The goal is to support interactive local PPT revision. Given an existing PPT, a
user revision request, an interaction context such as selected slide/element and
history, the system should:

```text
1. build a Multi-granularity Revision Index;
2. parse user revision intent;
3. localize affected slides and elements;
4. generate a structured edit operation plan;
5. execute local edits with a lightweight executor;
6. update the index for multi-turn interaction;
7. support benchmark evaluation.
```

Given an existing PPT, a user revision request, the current interaction context,
and the interaction history, the system should localize affected slides and
elements, generate an edit operation plan, and perform minimal local revisions
while preserving unaffected slides, the original visual style, and deck-level
coherence.

---

## Required Directory Structure

The following is the final target directory structure across all phases. Do not
create all files at once. For each phase, create only the files required by that
phase.

```text
slide_revise_agent/
  __init__.py

  parser/
    __init__.py
    ppt_parser.py
    ppt_xml_parser.py
    screenshot_renderer.py
    text_extractor.py

  index/
    __init__.py
    schema.py
    builder.py
    updater.py
    semantic_index.py
    element_index.py
    dependency_index.py
    operation_index.py

  interaction/
    __init__.py
    intent_parser.py
    reference_resolver.py
    context_manager.py
    history_store.py

  localization/
    __init__.py
    slide_locator.py
    element_locator.py
    scoring.py
    explain.py

  planning/
    __init__.py
    operation_schema.py
    edit_planner.py
    rule_policy.py
    llm_policy.py

  execution/
    __init__.py
    base_executor.py
    python_pptx_executor.py
    pptxgenjs_executor.py
    backend_adapter.py
    optional_backend_adapter.py

  verification/
    __init__.py
    preservation_checker.py
    visual_checker.py
    task_success_checker.py

  benchmark/
    __init__.py
    dataset_schema.py
    dataset_builder.py
    annotation_tool.py
    loader.py

  evaluation/
    __init__.py
    metrics.py
    run_localization_eval.py
    run_operation_eval.py
    run_revision_eval.py
    run_ablation.py

  ui_demo/
    __init__.py
    app.py
    components.py

  prompts/
    intent_parser.md
    edit_planner.md
    verifier.md

  configs/
    default.yaml

  cli.py
```

Also create:

```text
experiments/slide_revise/data/
experiments/slide_revise/outputs/
experiments/slide_revise/logs/
experiments/slide_revise/reports/
```

These experiment directories are part of the later reproducibility and
evaluation workflow. Do not create them during Phase 0 unless that phase
explicitly asks for them.

---

## Global Requirements

Follow these rules:

```text
1. Keep the new module independent.
2. Do not modify existing PPT generation behavior.
3. Use Pydantic for all core schemas.
4. Add type hints to all functions.
5. Make rule-based modes work without API keys.
6. LLM-based modes must be optional.
7. Every output should be serializable as JSON.
8. Save run logs for experiments.
9. Provide CLI commands for each major step.
10. Write smoke tests where possible.
```

---

## Core Schemas

Implement these schemas in `slide_revise_agent/index/schema.py`:

```text
DeckIndex
SlideIndex
ElementIndex
BoundingBox
SlideDependency
InteractionContext
InteractionTurn
InteractionIntent
SlideTarget
ElementTarget
LocalizationResult
EditOperation
EditPlan
OperationRule
OperationIndex
```

The schema should support:

```text
- selected slide / selected element;
- slide summary and key concepts;
- element bbox and style;
- dependency between slides;
- edit operation planning;
- dynamic index update;
- multi-turn history.
```

---

## CLI Requirements

Implement CLI subcommands:

```bash
python -m slide_revise_agent.cli build-index \
  --ppt input.pptx \
  --out deck_index.json

python -m slide_revise_agent.cli parse-intent \
  --instruction "这页太满了" \
  --selected-slide 5

python -m slide_revise_agent.cli localize \
  --index deck_index.json \
  --instruction "这里加个例子" \
  --selected-slide 6

python -m slide_revise_agent.cli plan \
  --index deck_index.json \
  --instruction "这页太满了" \
  --selected-slide 5 \
  --out edit_plan.json

python -m slide_revise_agent.cli execute \
  --ppt input.pptx \
  --plan edit_plan.json \
  --out revised.pptx

python -m slide_revise_agent.cli revise \
  --ppt input.pptx \
  --instruction "这页太满了，拆成两页" \
  --selected-slide 5 \
  --out revised.pptx
```

---

## Phase Order

Implement in this order:

```text
Phase 0: Project skeleton and schemas
Phase 1: PPT parser and basic DeckIndex
Phase 2: Semantic, element, and dependency indexes
Phase 3: Interaction context and intent parser
Phase 4: Index-guided slide/element localization
Phase 5: Edit planner
Phase 6: Lightweight PPT executor
Phase 7: Dynamic index update and multi-turn interaction
Phase 8: Benchmark loader and evaluation scripts
Phase 9: Minimal UI demo
Phase 10: Reproducibility package
```

Do not skip phases.

---

## Research Contribution Reminder

The paper contribution is not:

```text
- one-shot PPT generation;
- prompt engineering;
- calling a PPT skill;
- a pure demo system.
```

The paper contribution is:

```text
1. interactive local slide revision task;
2. Multi-granularity Revision Index;
3. index-guided slide/element localization;
4. index-guided edit planning;
5. dynamic index update for multi-turn interaction;
6. benchmark and evaluation.
```

Keep the implementation aligned with this research direction.

---

## First Task

Start with Phase 0 only:

```text
1. Create only the minimal schema package files listed below.
2. Implement Pydantic schemas needed for serialization and deserialization.
3. Add a minimal schema serialization test.
4. Do not create parser, executor, benchmark, evaluation, UI, CLI, prompt, config, or experiment files yet.
```

Phase 0 should only create:

```text
slide_revise_agent/__init__.py
slide_revise_agent/index/__init__.py
slide_revise_agent/index/schema.py
slide_revise_agent/interaction/__init__.py
slide_revise_agent/interaction/schema.py
slide_revise_agent/localization/__init__.py
slide_revise_agent/localization/schema.py
slide_revise_agent/planning/__init__.py
slide_revise_agent/planning/operation_schema.py
tests/slide_revise_agent/test_schema_serialization.py
```

After Phase 0, stop and report:

```text
- files created;
- schema overview;
- tests run;
- any assumptions.
```
