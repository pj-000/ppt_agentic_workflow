---
name: research-synthesis
description: Search-backed content synthesis for page-level PPT research results.
phase: research-synthesis
order: 30
default_prompt_mode: static
asset_groups:
  - kind: skill_doc
    pattern: SKILL.md
    purpose: Phase overview for search-backed research synthesis.
    injection_points:
      - docs:research-synthesis
    tags:
      - documentation
  - kind: reference
    pattern: research_synthesis_system.md
    purpose: Base system prompt for research synthesis.
    injection_points:
      - research_synthesis:system
    tags:
      - system-prompt
  - kind: reference
    pattern: research_synthesis_user.txt
    purpose: Base user prompt template for research synthesis.
    injection_points:
      - research_synthesis:user
    tags:
      - user-prompt
  - kind: reference
    pattern: repair_patterns.md
    purpose: Human-authored repair heuristics for research synthesis failures.
    injection_points:
      - docs:repair-heuristics
    tags:
      - repair
  - kind: template
    pattern: research_degraded_notice.txt
    purpose: Degraded-mode notice inserted when search backends fail.
    injection_points:
      - research_synthesis:degraded-notice
    tags:
      - fallback
---

# Research Synthesis

This skill governs research summarization, fact density, and fallback behavior.
