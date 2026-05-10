---
name: visual-production
description: PPT visual production rules, local enhancements, and vendor skill composition for theme, assets, and slide generation.
phase: visual-production
order: 40
default_prompt_mode: static
asset_groups:
  - kind: skill_doc
    pattern: SKILL.md
    purpose: Phase overview for visual production, theme planning, and slide generation.
    injection_points:
      - docs:visual-production
    tags:
      - documentation
  - kind: reference
    pattern: "*_system.md"
    purpose: System prompt reference for visual-production sub-stages.
    injection_points:
      - theme_decision:system
      - image_prompt_enrichment:system
      - slide_generation:system
      - deck_generation:system
    tags:
      - system-prompt
  - kind: reference
    pattern: "*rules*.md"
    purpose: Static visual rules used to reconstruct local prompt guidance.
    injection_points:
      - slide_generation:system-fragment
    tags:
      - rules
  - kind: reference
    pattern: "*policy.json"
    purpose: Static machine-readable policy used during visual validation and generation.
    injection_points:
      - runtime:validation-policy
    tags:
      - policy
  - kind: reference
    pattern: "*.json"
    purpose: Static visual-production catalogs and maps.
    injection_points:
      - runtime:catalog
    tags:
      - catalog
  - kind: template
    pattern: slide_user.txt
    purpose: Main user prompt template for single-slide generation.
    injection_points:
      - slide_generation:user
    tags:
      - user-prompt
  - kind: template
    pattern: theme_decision_user.txt
    purpose: Main user prompt template for global theme selection.
    injection_points:
      - theme_decision:user
    tags:
      - user-prompt
  - kind: template
    pattern: image_prompt_enrichment_user.txt
    purpose: Main user prompt template for image-prompt enrichment.
    injection_points:
      - image_prompt_enrichment:user
    tags:
      - user-prompt
  - kind: template
    pattern: "*context*.txt"
    purpose: Context fragments injected into slide-generation prompts.
    injection_points:
      - slide_generation:user-fragment
    tags:
      - context
  - kind: template
    pattern: "*section*.txt"
    purpose: Prompt section fragments for theme, layout, research, and page info.
    injection_points:
      - slide_generation:user-fragment
    tags:
      - prompt-fragment
  - kind: template
    pattern: "*feedback*.txt"
    purpose: Repair and revision feedback blocks for retries and QA-driven regeneration.
    injection_points:
      - slide_generation:repair-feedback
      - theme_decision:repair-feedback
    tags:
      - repair-feedback
  - kind: template
    pattern: "*error*.txt"
    purpose: Canonicalized repair instructions for image and visual-generation failures.
    injection_points:
      - slide_generation:repair-feedback
    tags:
      - repair
  - kind: template
    pattern: image_generation_prompt.txt
    purpose: Prompt template used when requesting image generation.
    injection_points:
      - asset_generation:user
    tags:
      - image-generation
  - kind: template
    pattern: "visual_strategy_*.txt"
    purpose: Visual-mode-specific planning blocks injected into slide prompts.
    injection_points:
      - slide_generation:user-fragment
    tags:
      - visual-mode
  - kind: template
    pattern: "*.txt"
    purpose: Additional visual-production fragments and wrappers.
    injection_points:
      - slide_generation:user-fragment
    tags:
      - prompt-fragment
---

# Visual Production

This skill wraps the vendor Anthropic PPTX skill with local equivalent enhancements.
