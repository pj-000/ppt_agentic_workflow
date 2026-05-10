---
name: shared-core
description: Shared runtime rules and output discipline for all harness phases.
phase: shared-core
order: 0
default_prompt_mode: static
asset_groups:
  - kind: skill_doc
    pattern: SKILL.md
    purpose: Shared harness conventions and documentation entrypoint.
    injection_points:
      - docs:skill-overview
    tags:
      - shared
      - documentation
  - kind: reference
    pattern: output_rules.md
    purpose: Shared output discipline reference for all phases.
    injection_points:
      - runtime:shared-output-rules
    tags:
      - rules
  - kind: reference
    pattern: skill_policy.json
    purpose: Policy map for learned-skill catalog loading across trigger stages.
    injection_points:
      - runtime:skill-policy
    tags:
      - policy
      - progressive-loading
  - kind: template
    pattern: "*.txt"
    purpose: Shared section wrappers for learned skills, promoted lessons, and runtime memories.
    injection_points:
      - runtime:dynamic-memory-rendering
    tags:
      - prompt-wrapper
---

# Shared Core

This skill holds the shared runtime conventions for the local harness.
