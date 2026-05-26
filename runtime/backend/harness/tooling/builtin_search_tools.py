from __future__ import annotations

from typing import Any

from backend.harness.tooling.error_signature import build_error_signature
from backend.harness.tooling.registry import ToolRegistry
from backend.harness.tooling.schema import ToolCall, ToolError, ToolResult, ToolSideEffect, ToolSpec


def register_search_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="search.web_text",
            description="Search web text snippets through the configured search backend.",
            input_schema=_object_schema({"query": "string", "max_results": "integer", "language": "string"}, required=["query"]),
            output_schema=_object_schema({"results": "array"}),
            timeout_s=30,
            retry=1,
            idempotent=True,
            side_effects=[ToolSideEffect.NETWORK, ToolSideEffect.EXTERNAL_API],
            tags=["search", "web", "text"],
        ),
        _search_web_text,
    )
    registry.register(
        ToolSpec(
            name="search.image",
            description="Search image URLs through the configured search backend.",
            input_schema=_object_schema({"query": "string", "max_results": "integer"}, required=["query"]),
            output_schema=_object_schema({"results": "array"}),
            timeout_s=30,
            retry=1,
            idempotent=True,
            side_effects=[ToolSideEffect.NETWORK, ToolSideEffect.EXTERNAL_API],
            tags=["search", "image"],
        ),
        _search_image,
    )


async def _search_web_text(call: ToolCall) -> ToolResult | dict[str, Any]:
    from backend.tools.search_backend import SearchBackend

    backend = SearchBackend()
    if not backend.enabled:
        return _skipped(call, "No search provider is configured")
    max_results = int(call.input.get("max_results") or 5)
    results = await backend.search_text_results(str(call.input.get("query") or ""), max_results=max_results)
    return {"results": results}


async def _search_image(call: ToolCall) -> ToolResult | dict[str, Any]:
    from backend.tools.search_backend import SearchBackend

    backend = SearchBackend()
    if not backend.enabled:
        return _skipped(call, "No image search provider is configured")
    max_results = int(call.input.get("max_results") or 5)
    urls = await backend.search_images(str(call.input.get("query") or ""), max_results=max_results)
    return {"results": [{"url": url} for url in urls]}


def _skipped(call: ToolCall, reason: str) -> ToolResult:
    return ToolResult(
        run_id=call.run_id,
        call_id=call.call_id,
        tool_name=call.tool_name,
        status="skipped",
        output={"reason": reason, "results": []},
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
