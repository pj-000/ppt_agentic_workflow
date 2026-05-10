---
name: document-understanding
description: Summarize uploaded documents into PPT-ready structure while preserving source grounding.
phase: document-understanding
order: 10
default_prompt_mode: static
asset_groups:
  - kind: skill_doc
    pattern: SKILL.md
    purpose: Phase overview for document extraction and structured summarization.
    injection_points:
      - docs:document-understanding
    tags:
      - documentation
  - kind: reference
    pattern: repair_patterns.md
    purpose: Repair heuristics for document-summary retries.
    injection_points:
      - docs:repair-heuristics
    tags:
      - repair
  - kind: template
    pattern: document_summary_system_wrapper.txt
    purpose: System wrapper around document-summary prompts.
    injection_points:
      - document_summary:system
    tags:
      - system-prompt
  - kind: template
    pattern: document_summary_user.txt
    purpose: Base user prompt template for document summarization.
    injection_points:
      - document_summary:user
    tags:
      - user-prompt
  - kind: template
    pattern: document_summary_retry_feedback.txt
    purpose: Retry feedback appended after failed document-summary attempts.
    injection_points:
      - document_summary:repair-feedback
    tags:
      - repair-feedback
  - kind: template
    pattern: document_summary_*.txt
    purpose: Formatting and rendering fragments for document-summary prompts and outputs.
    injection_points:
      - document_summary:formatting
    tags:
      - formatting
  - kind: template
    pattern: planner_context_*.txt
    purpose: Structured planner-context fragments derived from summarized documents.
    injection_points:
      - outline_generation:document-context
    tags:
      - planner-context
---

# Document Understanding

This skill governs document extraction and structured summarization before PPT planning.
