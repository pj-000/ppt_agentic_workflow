# AGENTS.md

## Project Goal

This repository is being extended with a research module named `slide_revise_agent`.

The target paper direction is:

SlideReviseAgent: Index-Guided Interactive Local Revision for Presentation Slides.

The core contribution is NOT one-shot PPT generation. The core contribution is a
Multi-granularity Revision Index for interactive slide revision.

## Development Rules

1. Do not rewrite or break the existing PPT generation backend.
2. Add new implementation code under `slide_revise_agent/` unless explicitly
   instructed.
3. Keep existing `ppt_backend/` and `runtime/` behavior unchanged.
4. Prefer small, testable modules.
5. Every implementation phase must include tests.
6. Every public schema must support JSON serialization and deserialization.
7. Avoid hard-coded API keys, credentials, private prompts, or secrets.
8. Do not commit generated large PPTX, screenshots, temporary files, or
   benchmark artifacts unless explicitly requested.
9. Keep the implementation backend-agnostic. Existing PPT generation can be
   treated as an optional backend adapter.
10. Preserve research reproducibility: logs, configs, and evaluation outputs
    should be deterministic when possible.
11. Use `Multi-granularity Revision Index` and `多粒度 Revision Index`
    consistently.
12. Treat `ADD_EXAMPLE` as an intent, not as a primitive edit operation.
13. Use `ADD_SLIDE` with structured parameters such as
    `slide_type="example"` and `content_intent="add_example"`.
14. Use structured edit operation objects instead of plain operation string
    arrays.
15. Do not start implementing Phase 0 or any business logic unless explicitly
    requested.

## Review Guidelines

When reviewing changes, check:

1. Whether the change supports the IUI 2027 paper direction.
2. Whether the Revision Index remains the central method contribution.
3. Whether operation names and dataset schemas are consistent.
4. Whether tests cover serialization, edge cases, and error handling once
   implementation begins.
5. Whether the code avoids modifying unrelated existing backend behavior.
6. Whether the module can be used for benchmark and evaluation later.
7. Whether the code leaks private credentials, prompts, or proprietary workflow
   details.
