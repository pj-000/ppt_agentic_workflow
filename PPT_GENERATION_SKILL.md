---
name: ppt-generation
description: Use this skill when the user requests PPT/PPTX generation. It bundles the full outline planning, slide generation, evaluation, preview, and download workflow inside the skill runtime rather than scattering the logic across prompts.
---

# PPT Generation Skill

This skill owns the complete PPT workflow as executable runtime code. The orchestration logic lives in [`assets/runtime`](assets/runtime), and the reusable bridge entrypoint lives in [`scripts/runtime.py`](scripts/runtime.py).

## When To Use

Use this skill when the request is to:

- generate a new PPT or teaching presentation
- upload source documents and derive a PPT outline
- confirm an outline and render the final PPT
- evaluate an existing PPT draft
- fetch preview images or download the generated PPT

## Runtime Contract

The host should call the skill runtime entrypoints in `scripts/runtime.py` instead of re-implementing the flow in prompts.

Available entrypoints:

- `upload_document_route`
- `stream_ppt_outline_route`
- `stream_ppt_from_outline_route`
- `stream_evaluate_ppt_route`
- `download_ppt_route`
- `preview_ppt_image_route`

## Bundled Assets

- `assets/runtime/api.py`: full FastAPI-compatible PPT workflow
- `assets/runtime/backend/`: orchestrator, planning, rendering, evaluation, preview helpers
- `scripts/generate.py`: standalone PPTX assembly utility for image-to-deck composition

## Notes

- Keep the skill body lean; the detailed implementation belongs in the bundled runtime.
- When extending the PPT workflow, update `assets/runtime/` or `scripts/runtime.py` instead of pushing complex logic back into prompt text.
- This skill is designed to be triggered by DeerFlow or a thin gateway bridge, so the API contract should remain stable.
