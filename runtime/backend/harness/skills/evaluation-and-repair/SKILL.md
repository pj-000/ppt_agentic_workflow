---
name: evaluation-and-repair
description: Visual and content evaluation prompts, metric catalogs, and repair memory.
phase: evaluation-and-repair
order: 50
default_prompt_mode: static
asset_groups:
  - kind: skill_doc
    pattern: SKILL.md
    purpose: Phase overview for content evaluation, visual evaluation, and repair.
    injection_points:
      - docs:evaluation-and-repair
    tags:
      - documentation
  - kind: reference
    pattern: "*_system.md"
    purpose: System prompt reference for evaluation and repair sub-stages.
    injection_points:
      - content_evaluation:system
      - visual_evaluation:system
      - deck_coherence_review:system
    tags:
      - system-prompt
  - kind: reference
    pattern: "*policy.json"
    purpose: Machine-readable evaluation and repair policies.
    injection_points:
      - runtime:evaluation-policy
    tags:
      - policy
  - kind: reference
    pattern: "*aliases.json"
    purpose: Static metric alias catalogs for evaluation normalization.
    injection_points:
      - runtime:catalog
    tags:
      - catalog
  - kind: reference
    pattern: default_principle_descriptions.json
    purpose: Fallback principle descriptions for content evaluation.
    injection_points:
      - content_evaluation:user-fragment
    tags:
      - catalog
  - kind: reference
    pattern: repair_instruction_map.json
    purpose: Error-signature to repair-instruction mapping for evaluation retries.
    injection_points:
      - evaluation:repair-map
    tags:
      - repair
  - kind: reference
    pattern: repair_patterns.md
    purpose: Human-authored repair heuristics for evaluation failures.
    injection_points:
      - docs:repair-heuristics
    tags:
      - repair
  - kind: template
    pattern: content_evaluation_user.txt
    purpose: Main user prompt template for content evaluation.
    injection_points:
      - content_evaluation:user
    tags:
      - user-prompt
  - kind: template
    pattern: visual_evaluation_user.txt
    purpose: Main user prompt template for visual evaluation.
    injection_points:
      - visual_evaluation:user
    tags:
      - user-prompt
  - kind: template
    pattern: deck_coherence_review_user.txt
    purpose: Main user prompt template for deck-level coherence review.
    injection_points:
      - deck_coherence_review:user
    tags:
      - user-prompt
  - kind: template
    pattern: "*retry*.txt"
    purpose: Retry feedback and retry wrappers for evaluation failures.
    injection_points:
      - content_evaluation:repair-feedback
      - visual_evaluation:repair-feedback
      - deck_coherence_review:repair-feedback
    tags:
      - repair-feedback
  - kind: template
    pattern: "*metric*.txt"
    purpose: Metric rendering fragments for content evaluation prompts.
    injection_points:
      - content_evaluation:user-fragment
    tags:
      - metrics
  - kind: template
    pattern: "*progress*.txt"
    purpose: Progress-reporting fragments for streaming evaluation responses.
    injection_points:
      - content_evaluation:streaming
    tags:
      - streaming
  - kind: template
    pattern: "*default*.txt"
    purpose: Fallback reason and suggestion fragments for evaluation normalization.
    injection_points:
      - content_evaluation:normalization
    tags:
      - fallback
  - kind: template
    pattern: "visual_evaluation_failed_*.txt"
    purpose: Canonical failure descriptions and suggestions for visual evaluation retries.
    injection_points:
      - visual_evaluation:repair-feedback
    tags:
      - repair
---

# Evaluation And Repair

This skill governs visual QA, content QA, and repair-oriented evaluation.
