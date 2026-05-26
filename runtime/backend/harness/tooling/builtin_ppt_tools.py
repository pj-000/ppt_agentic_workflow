from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.harness.tooling.error_signature import build_error_signature, sanitize_error_message
from backend.harness.tooling.registry import ToolRegistry
from backend.harness.tooling.schema import ToolCall, ToolError, ToolResult, ToolSideEffect, ToolSpec


def register_ppt_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="ppt.check_js_syntax",
            description="Check JavaScript syntax before running PptxGenJS.",
            input_schema=_object_schema({"code": "string"}, required=["code"]),
            output_schema=_object_schema({"valid": "boolean", "stdout_excerpt": "string", "stderr_excerpt": "string"}),
            timeout_s=20,
            retry=0,
            idempotent=True,
            side_effects=[ToolSideEffect.SUBPROCESS],
            tags=["ppt", "javascript", "validation"],
        ),
        _check_js_syntax,
    )
    registry.register(
        ToolSpec(
            name="ppt.run_pptxgenjs",
            description="Run PptxGenJS JavaScript and write a PPTX artifact.",
            input_schema=_object_schema({"code": "string", "output_path": "string", "timeout_s": "integer"}, required=["code", "output_path"]),
            output_schema=_object_schema({"pptx_path": "string", "exists": "boolean", "file_size": "integer"}),
            timeout_s=120,
            retry=0,
            idempotent=False,
            side_effects=[ToolSideEffect.FILESYSTEM, ToolSideEffect.SUBPROCESS],
            tags=["ppt", "pptxgenjs", "artifact"],
        ),
        _run_pptxgenjs,
    )
    registry.register(
        ToolSpec(
            name="ppt.read_pptx_text",
            description="Extract readable text from a PPTX file.",
            input_schema=_object_schema({"pptx_path": "string"}, required=["pptx_path"]),
            output_schema=_object_schema({"text": "string", "text_length": "integer"}),
            timeout_s=45,
            retry=0,
            idempotent=True,
            side_effects=[ToolSideEffect.SUBPROCESS],
            tags=["ppt", "extract", "text"],
        ),
        _read_pptx_text,
    )
    registry.register(
        ToolSpec(
            name="ppt.render_preview",
            description="Render a PPTX into preview slide images.",
            input_schema=_object_schema({"pptx_path": "string", "output_dir": "string"}, required=["pptx_path", "output_dir"]),
            output_schema=_object_schema({"preview_images": "array", "preview_count": "integer"}),
            timeout_s=240,
            retry=0,
            idempotent=True,
            side_effects=[ToolSideEffect.FILESYSTEM, ToolSideEffect.SUBPROCESS],
            tags=["ppt", "preview", "render"],
        ),
        _render_preview,
    )


def _check_js_syntax(call: ToolCall) -> dict[str, Any]:
    from backend.tools import pptx_skill

    valid, stderr = pptx_skill.check_js_syntax(
        str(call.input.get("code") or ""),
        timeout=int(call.input.get("timeout_s") or 20),
    )
    return {
        "valid": bool(valid),
        "stdout_excerpt": "",
        "stderr_excerpt": sanitize_error_message(stderr, limit=1200),
    }


def _run_pptxgenjs(call: ToolCall) -> ToolResult | dict[str, Any]:
    from backend.tools import pptx_skill

    output_path = str(call.input.get("output_path") or "")
    timeout_s = int(call.input.get("timeout_s") or 60)
    pptx_path = pptx_skill.run_js(str(call.input.get("code") or ""), output_path, timeout=timeout_s)
    path = Path(pptx_path)
    if not path.exists():
        return _failed(call, "PptxArtifactMissing", "PptxGenJS completed but did not create the PPTX file")
    return {
        "pptx_path": str(path),
        "exists": True,
        "file_size": path.stat().st_size,
    }


def _read_pptx_text(call: ToolCall) -> ToolResult | dict[str, Any]:
    from backend.tools import pptx_skill

    pptx_path = str(call.input.get("pptx_path") or "")
    if not Path(pptx_path).exists():
        return _failed(call, "FileNotFoundError", "PPTX file does not exist")
    text = pptx_skill.read_pptx(pptx_path)
    return {"text": text, "text_length": len(text)}


def _render_preview(call: ToolCall) -> ToolResult | dict[str, Any]:
    from backend.tools import pptx_skill

    pptx_path = str(call.input.get("pptx_path") or "")
    output_dir = str(call.input.get("output_dir") or "")
    if not Path(pptx_path).exists():
        return _failed(call, "FileNotFoundError", "PPTX file does not exist")
    images = pptx_skill.pptx_to_images(pptx_path, output_dir)
    if not images:
        return _failed(call, "PreviewRenderError", "Preview rendering produced no images")
    return {"preview_images": list(images), "preview_count": len(images)}


def _failed(call: ToolCall, error_type: str, message: str) -> ToolResult:
    safe_message = sanitize_error_message(message)
    return ToolResult(
        run_id=call.run_id,
        call_id=call.call_id,
        tool_name=call.tool_name,
        status="failed",
        error=ToolError(
            error_type=error_type,
            message=safe_message,
            error_signature=build_error_signature(
                tool_name=call.tool_name,
                error_type=error_type,
                message=safe_message,
            ),
            retryable=False,
            raw_excerpt=safe_message,
        ),
    )


def _object_schema(properties: dict[str, str], *, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {key: {"type": value} for key, value in properties.items()},
        "required": list(required or []),
    }
