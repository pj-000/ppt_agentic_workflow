from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any

from backend.harness.tooling.error_signature import build_error_signature, sanitize_error_message
from backend.harness.tooling.errors import ToolInputValidationError
from backend.harness.tooling.registry import ToolRegistry
from backend.harness.tooling.schema import ToolCall, ToolError, ToolResult, ToolSpec


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, trace: Any | None = None):
        self.registry = registry
        self.trace = trace

    async def execute(self, call: ToolCall) -> ToolResult:
        started = time.perf_counter()
        try:
            registered = self.registry.get(call.tool_name)
            spec = registered.spec
            self._validate_input(call, spec)
        except Exception as exc:
            latency_ms = _latency_ms(started)
            return self._error_result(call, exc, latency_ms=latency_ms, status="failed")

        last_result: ToolResult | None = None
        for attempt in range(spec.retry + 1):
            self._record(
                "tool.started",
                {
                    "tool_name": call.tool_name,
                    "call_id": call.call_id,
                    "caller": call.caller,
                    "purpose": call.purpose,
                    "attempt": attempt + 1,
                },
            )
            attempt_started = time.perf_counter()
            try:
                raw = await asyncio.wait_for(
                    self._invoke_handler(registered.handler, call),
                    timeout=max(1, int(spec.timeout_s or 1)),
                )
                result = self._coerce_result(call, raw)
                if result.latency_ms <= 0:
                    result.latency_ms = _latency_ms(attempt_started)
            except TimeoutError as exc:
                result = self._error_result(call, exc, latency_ms=_latency_ms(attempt_started), status="timeout")
            except asyncio.TimeoutError as exc:
                result = self._error_result(call, exc, latency_ms=_latency_ms(attempt_started), status="timeout")
            except Exception as exc:
                result = self._error_result(call, exc, latency_ms=_latency_ms(attempt_started), status="failed")

            last_result = result
            retryable = result.status == "timeout" or bool(result.error and result.error.retryable)
            if result.status == "success" or result.status == "skipped" or attempt >= spec.retry or not retryable:
                self._record_finished(result, attempt=attempt + 1)
                return result
            self._record_finished(result, attempt=attempt + 1)

        return last_result or self._error_result(
            call,
            RuntimeError("Tool execution did not produce a result"),
            latency_ms=_latency_ms(started),
            status="failed",
        )

    async def _invoke_handler(self, handler, call: ToolCall) -> Any:
        if inspect.iscoroutinefunction(handler):
            return await handler(call)
        result = await asyncio.to_thread(handler, call)
        if inspect.isawaitable(result):
            return await result
        return result

    def _coerce_result(self, call: ToolCall, raw: Any) -> ToolResult:
        if isinstance(raw, ToolResult):
            return raw
        if isinstance(raw, dict):
            return ToolResult(
                run_id=call.run_id,
                call_id=call.call_id,
                tool_name=call.tool_name,
                status="success",
                output=raw,
            )
        return ToolResult(
            run_id=call.run_id,
            call_id=call.call_id,
            tool_name=call.tool_name,
            status="success",
            output={"value": raw},
        )

    def _validate_input(self, call: ToolCall, spec: ToolSpec) -> None:
        if not isinstance(call.input, dict):
            raise ToolInputValidationError("Tool input must be a dictionary")
        schema = spec.input_schema or {}
        required = schema.get("required") or []
        for key in required:
            if key not in call.input:
                raise ToolInputValidationError(f"Missing required input field: {key}")
        properties = schema.get("properties") or {}
        for key, property_schema in properties.items():
            if key not in call.input:
                continue
            expected = property_schema.get("type")
            if expected and not _matches_json_type(call.input[key], expected):
                raise ToolInputValidationError(f"Invalid input field type: {key} expected {expected}")

    def _error_result(
        self,
        call: ToolCall,
        exc: Exception,
        *,
        latency_ms: int,
        status: str,
    ) -> ToolResult:
        error_type = type(exc).__name__
        message = sanitize_error_message(str(exc) or error_type)
        return ToolResult(
            run_id=call.run_id,
            call_id=call.call_id,
            tool_name=call.tool_name,
            status=status,  # type: ignore[arg-type]
            error=ToolError(
                error_type=error_type,
                message=message,
                error_signature=build_error_signature(
                    tool_name=call.tool_name,
                    error_type=error_type,
                    message=message,
                ),
                retryable=_is_retryable(exc) or status == "timeout",
                raw_excerpt=message[:200],
            ),
            latency_ms=latency_ms,
        )

    def _record_finished(self, result: ToolResult, *, attempt: int) -> None:
        self._record(
            "tool.finished",
            {
                "tool_name": result.tool_name,
                "call_id": result.call_id,
                "status": result.status,
                "latency_ms": result.latency_ms,
                "attempt": attempt,
                "error_signature": result.error.error_signature if result.error else "",
            },
        )

    def _record(self, stage: str, payload: dict[str, Any]) -> None:
        if not self.trace:
            return
        record = getattr(self.trace, "record", None)
        if callable(record):
            record(stage=stage, payload=payload)


def _latency_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _matches_json_type(value: Any, expected: str | list[str]) -> bool:
    expected_values = expected if isinstance(expected, list) else [expected]
    for item in expected_values:
        if item == "string" and isinstance(value, str):
            return True
        if item == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if item == "number" and isinstance(value, int | float) and not isinstance(value, bool):
            return True
        if item == "boolean" and isinstance(value, bool):
            return True
        if item == "array" and isinstance(value, list):
            return True
        if item == "object" and isinstance(value, dict):
            return True
        if item == "null" and value is None:
            return True
    return False


def _is_retryable(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError | ConnectionError | OSError)
