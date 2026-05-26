from __future__ import annotations

from typing import Any

from backend.harness.tooling.error_signature import build_error_signature
from backend.harness.tooling.registry import ToolRegistry
from backend.harness.tooling.schema import ToolCall, ToolError, ToolResult, ToolSideEffect, ToolSpec


def register_eval_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="eval.visual_slides",
            description="Evaluate rendered slide previews with the configured visual evaluator.",
            input_schema=_object_schema({"preview_images": "array", "outline": "object"}, required=["preview_images", "outline"]),
            output_schema=_object_schema({"results": "array"}),
            timeout_s=180,
            retry=0,
            idempotent=True,
            side_effects=[ToolSideEffect.LLM, ToolSideEffect.EXTERNAL_API],
            tags=["eval", "visual", "vlm"],
        ),
        _visual_eval_skipped,
    )
    registry.register(
        ToolSpec(
            name="eval.content_text",
            description="Evaluate generated PPT text against outline and content quality criteria.",
            input_schema=_object_schema({"text": "string", "outline": "object"}, required=["text", "outline"]),
            output_schema=_object_schema({"issues": "array"}),
            timeout_s=180,
            retry=0,
            idempotent=True,
            side_effects=[ToolSideEffect.LLM, ToolSideEffect.EXTERNAL_API],
            tags=["eval", "content", "llm"],
        ),
        _content_eval_skipped,
    )


def _visual_eval_skipped(call: ToolCall) -> ToolResult:
    return _skipped(call, "Visual evaluation requires VLM runtime and is not wired into ToolRuntime yet", {"results": []})


def _content_eval_skipped(call: ToolCall) -> ToolResult:
    return _skipped(call, "Content evaluation requires LLM runtime and is not wired into ToolRuntime yet", {"issues": []})


def _skipped(call: ToolCall, reason: str, output: dict[str, Any]) -> ToolResult:
    return ToolResult(
        run_id=call.run_id,
        call_id=call.call_id,
        tool_name=call.tool_name,
        status="skipped",
        output={**output, "reason": reason},
        error=ToolError(
            error_type="ToolSkipped",
            message=reason,
            error_signature=build_error_signature(
                tool_name=call.tool_name,
                error_type="ToolSkipped",
                message=reason,
            ),
            retryable=False,
            raw_excerpt=reason,
        ),
    )


def _object_schema(properties: dict[str, str], *, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {key: {"type": value} for key, value in properties.items()},
        "required": list(required or []),
    }
