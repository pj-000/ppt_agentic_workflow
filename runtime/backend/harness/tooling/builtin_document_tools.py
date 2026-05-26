from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.harness.tooling.error_signature import build_error_signature
from backend.harness.tooling.registry import ToolRegistry
from backend.harness.tooling.schema import ToolCall, ToolError, ToolResult, ToolSideEffect, ToolSpec


def register_document_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="document.extract_text",
            description="Extract text and tables from a local document.",
            input_schema=_object_schema({"document_path": "string"}, required=["document_path"]),
            output_schema=_object_schema({"text": "string", "text_length": "integer", "tables": "array"}),
            timeout_s=60,
            retry=0,
            idempotent=True,
            side_effects=[ToolSideEffect.FILESYSTEM, ToolSideEffect.SUBPROCESS],
            tags=["document", "extract"],
        ),
        _extract_text,
    )
    registry.register(
        ToolSpec(
            name="document.summarize",
            description="Summarize extracted document text into key points.",
            input_schema=_object_schema({"text": "string", "language": "string"}, required=["text"]),
            output_schema=_object_schema({"summary": "string", "key_points": "array"}),
            timeout_s=120,
            retry=0,
            idempotent=True,
            side_effects=[ToolSideEffect.LLM, ToolSideEffect.EXTERNAL_API],
            tags=["document", "summary", "llm"],
        ),
        _summarize_skipped,
    )


def _extract_text(call: ToolCall) -> ToolResult | dict[str, Any]:
    from backend.harness.agents.document_summary import extract_document_content

    document_path = str(call.input.get("document_path") or "")
    if not Path(document_path).exists():
        return _failed(call, "FileNotFoundError", "Document file does not exist")
    text, tables, page_count = extract_document_content(document_path)
    return {
        "text": text,
        "text_length": len(text),
        "tables": tables,
        "page_count": page_count,
    }


def _summarize_skipped(call: ToolCall) -> ToolResult:
    reason = "Document summarization requires LLM runtime and is not wired into ToolRuntime yet"
    return ToolResult(
        run_id=call.run_id,
        call_id=call.call_id,
        tool_name=call.tool_name,
        status="skipped",
        output={"summary": "", "key_points": [], "reason": reason},
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


def _failed(call: ToolCall, error_type: str, message: str) -> ToolResult:
    return ToolResult(
        run_id=call.run_id,
        call_id=call.call_id,
        tool_name=call.tool_name,
        status="failed",
        error=ToolError(
            error_type=error_type,
            message=message,
            error_signature=build_error_signature(
                tool_name=call.tool_name,
                error_type=error_type,
                message=message,
            ),
            retryable=False,
            raw_excerpt=message,
        ),
    )


def _object_schema(properties: dict[str, str], *, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {key: {"type": value} for key, value in properties.items()},
        "required": list(required or []),
    }
