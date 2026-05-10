---
name: outline-planning
description: Plan page-level PPT outlines, audience adaptation, and visual-mode semantics for the deck.
phase: outline-planning
order: 20
default_prompt_mode: static
asset_groups:
  - kind: skill_doc
    pattern: SKILL.md
    purpose: Phase overview for page-level outline planning.
    injection_points:
      - docs:outline-planning
    tags:
      - documentation
  - kind: reference
    pattern: outline_system_prompt.md
    purpose: Base system prompt for outline generation.
    injection_points:
      - outline_generation:system
    tags:
      - system-prompt
  - kind: reference
    pattern: outline_planning_user.txt
    purpose: Base user prompt template for outline planning.
    injection_points:
      - outline_generation:user
    tags:
      - user-prompt
  - kind: reference
    pattern: deck_generation_user.txt
    purpose: Deck-level planning user prompt template used when generating full presentations.
    injection_points:
      - deck_generation:user
    tags:
      - user-prompt
  - kind: reference
    pattern: repair_instruction_map.json
    purpose: Error-signature to repair-instruction mapping for outline generation retries.
    injection_points:
      - outline_generation:repair-map
    tags:
      - repair
  - kind: reference
    pattern: "*.json"
    purpose: Static audience/style catalogs used to normalize outline-planning requests.
    injection_points:
      - runtime:catalog
    tags:
      - catalog
  - kind: template
    pattern: outline_planning_user.txt
    purpose: Rendered user prompt for outline generation.
    injection_points:
      - outline_generation:user
    tags:
      - user-prompt
  - kind: template
    pattern: outline_retry_feedback.txt
    purpose: Retry feedback appended after failed outline attempts.
    injection_points:
      - outline_generation:repair-feedback
    tags:
      - repair-feedback
  - kind: template
    pattern: audience_profile.txt
    purpose: Audience-specific guidance block injected into outline prompts.
    injection_points:
      - outline_generation:user-fragment
    tags:
      - audience
  - kind: template
    pattern: default_style_text.txt
    purpose: Fallback style description when the user does not supply an explicit style.
    injection_points:
      - outline_generation:user-fragment
    tags:
      - style
  - kind: template
    pattern: extra_requirements.txt
    purpose: Optional extra-requirements wrapper injected into outline prompts.
    injection_points:
      - outline_generation:user-fragment
    tags:
      - requirements
---

# Outline Planning

This skill defines page-level outline generation, audience adaptation, and planning catalogs.
