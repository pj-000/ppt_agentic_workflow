from __future__ import annotations

from typing import Any

from backend.harness.tooling.error_signature import build_error_signature
from backend.harness.tooling.schema import ToolResult


def tool_result_to_quality_error(result: ToolResult) -> dict[str, Any] | None:
    if result.status == "success":
        return None

    message = ""
    error_type = "ToolSkipped" if result.status == "skipped" else "ToolError"
    error_signature = build_error_signature(
        tool_name=result.tool_name,
        error_type=error_type,
        message=str(result.output.get("reason") or result.status),
    )
    if result.error is not None:
        error_type = result.error.error_type
        error_signature = result.error.error_signature
        message = result.error.message
    else:
        message = str(result.output.get("reason") or result.status)

    return {
        "tool": result.tool_name,
        "stage": result.tool_name,
        "status": result.status,
        "error_type": error_type,
        "error_signature": error_signature,
        "message": message,
        "latency_ms": result.latency_ms,
    }
