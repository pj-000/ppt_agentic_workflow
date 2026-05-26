from __future__ import annotations

import inspect
import logging
import time
from typing import Any

from backend.harness.agent_runtime.errors import (
    AgentCapabilityError,
    build_agent_error_signature,
    sanitize_agent_error_message,
)
from backend.harness.agent_runtime.registry import AgentRegistry
from backend.harness.agent_runtime.schema import AgentContext, AgentError, AgentRequest, AgentResult, AgentSpec
from backend.harness.agent_runtime.serialization import to_jsonable

logger = logging.getLogger(__name__)


class AgentExecutor:
    def __init__(self, registry: AgentRegistry, trace: Any | None = None):
        self.registry = registry
        self.trace = trace

    async def execute(
        self,
        *,
        agent_name: str,
        request: AgentRequest,
        context: AgentContext,
    ) -> AgentResult:
        registered = self.registry.get(agent_name)
        spec = registered.spec
        started = time.perf_counter()
        self._record(
            "agent.started",
            {
                "agent_name": spec.name,
                "capability": request.capability.value,
                "task_id": request.task_id,
                "status": "started",
            },
        )

        if request.capability not in spec.capabilities:
            result = _error_result(
                agent_name=spec.name,
                request=request,
                exc=AgentCapabilityError(f"Unsupported capability: {request.capability.value}"),
                status="skipped",
                latency_ms=_latency_ms(started),
                retryable=False,
            )
            result = _normalize_agent_result(
                result,
                spec=spec,
                request=request,
                latency_ms=_latency_ms(started),
            )
            self._record_finished(result)
            return result

        try:
            raw = registered.runtime.run(request, context)
            result = await raw if inspect.isawaitable(raw) else raw
            if not isinstance(result, AgentResult):
                result = AgentResult(
                    run_id=request.run_id,
                    task_id=request.task_id,
                    agent_name=spec.name,
                    capability=request.capability,
                    status="success",
                    payload={"value": to_jsonable(result)},
                )
            result = _normalize_agent_result(
                result,
                spec=spec,
                request=request,
                latency_ms=_latency_ms(started),
            )
        except Exception as exc:
            result = _error_result(
                agent_name=spec.name,
                request=request,
                exc=exc,
                status="failed",
                latency_ms=_latency_ms(started),
                retryable=_is_retryable(exc),
            )
            result = _normalize_agent_result(
                result,
                spec=spec,
                request=request,
                latency_ms=_latency_ms(started),
            )

        self._record_finished(result)
        return result

    def _record_finished(self, result: AgentResult) -> None:
        first_error = result.errors[0] if result.errors else None
        self._record(
            "agent.finished",
            {
                "agent_name": result.agent_name,
                "capability": result.capability.value,
                "task_id": result.task_id,
                "status": result.status,
                "latency_ms": result.metrics.get("latency_ms", 0),
                "error_signature": first_error.error_signature if first_error else "",
                "metrics": result.metrics,
                "artifact_refs": result.output_artifacts,
            },
        )

    def _record(self, stage: str, payload: dict[str, Any]) -> None:
        if not self.trace:
            return
        record = getattr(self.trace, "record", None)
        if not callable(record):
            return
        try:
            record(stage=stage, payload=to_jsonable(payload))
        except Exception as exc:
            logger.warning("[AgentRuntime] Trace recording failed; continuing: %s", exc)


def _error_result(
    *,
    agent_name: str,
    request: AgentRequest,
    exc: Exception,
    status: str,
    latency_ms: int,
    retryable: bool,
) -> AgentResult:
    error_type = type(exc).__name__
    message = sanitize_agent_error_message(str(exc) or error_type)
    return AgentResult(
        run_id=request.run_id,
        task_id=request.task_id,
        agent_name=agent_name,
        capability=request.capability,
        status=status,  # type: ignore[arg-type]
        metrics={"latency_ms": latency_ms},
        errors=[
            AgentError(
                error_type=error_type,
                message=message,
                error_signature=build_agent_error_signature(
                    agent_name=agent_name,
                    capability=request.capability.value,
                    error_type=error_type,
                    message=message,
                ),
                retryable=retryable,
                raw_excerpt=message[:200],
            )
        ],
    )


def _normalize_agent_result(
    result: AgentResult,
    *,
    spec: AgentSpec,
    request: AgentRequest,
    latency_ms: int,
) -> AgentResult:
    metrics = to_jsonable(result.metrics)
    if not isinstance(metrics, dict):
        metrics = {}
    metrics["latency_ms"] = latency_ms
    return result.model_copy(
        update={
            "run_id": request.run_id,
            "task_id": request.task_id,
            "agent_name": spec.name,
            "capability": request.capability,
            "payload": to_jsonable(result.payload),
            "output_artifacts": {str(key): str(value) for key, value in result.output_artifacts.items()},
            "metrics": metrics,
            "errors": _sanitize_result_errors(
                result,
                agent_name=spec.name,
                capability=request.capability.value,
            ),
            "memory_writes": [str(to_jsonable(item)) for item in result.memory_writes],
        }
    )


def _sanitize_result_errors(
    result: AgentResult,
    *,
    agent_name: str,
    capability: str,
) -> list[AgentError]:
    sanitized: list[AgentError] = []
    for error in result.errors:
        error_type = sanitize_agent_error_message(error.error_type or "Error", limit=80)
        message = sanitize_agent_error_message(error.message)
        raw_excerpt = (
            sanitize_agent_error_message(error.raw_excerpt, limit=200)
            if error.raw_excerpt is not None
            else None
        )
        sanitized.append(
            AgentError(
                error_type=error_type,
                message=message,
                error_signature=build_agent_error_signature(
                    agent_name=agent_name,
                    capability=capability,
                    error_type=error_type,
                    message=message,
                ),
                retryable=bool(error.retryable),
                raw_excerpt=raw_excerpt,
            )
        )
    return sanitized


def _latency_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _is_retryable(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError | ConnectionError | OSError)
