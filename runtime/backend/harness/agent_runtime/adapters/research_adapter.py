from __future__ import annotations

import asyncio
import inspect
from typing import Any

from backend.harness.agent_runtime.errors import build_agent_error_signature, sanitize_agent_error_message
from backend.harness.agent_runtime.schema import (
    AgentCapability,
    AgentContext,
    AgentError,
    AgentRequest,
    AgentResult,
    AgentRole,
    AgentSpec,
)
from backend.harness.agent_runtime.serialization import to_jsonable


class ResearchRuntimeAdapter:
    spec = AgentSpec(
        name="researcher",
        role=AgentRole.RESEARCHER,
        capabilities=[AgentCapability.RESEARCH_TOPIC, AgentCapability.RESEARCH_SLIDE],
        description="Adapter for ResearchAgent capabilities.",
    )

    def __init__(self, impl: Any):
        self.impl = impl

    async def run(self, request: AgentRequest, context: AgentContext) -> AgentResult:
        try:
            if request.capability == AgentCapability.RESEARCH_TOPIC:
                return await self._research_topic(request, context)
            if request.capability == AgentCapability.RESEARCH_SLIDE:
                return await self._research_slide(request, context)
            return _skipped(request, self.spec.name, f"Unsupported capability: {request.capability.value}")
        except Exception as exc:
            return _failed(request, self.spec.name, exc)

    async def _research_topic(self, request: AgentRequest, context: AgentContext) -> AgentResult:
        payload = request.payload
        result = await _invoke(
            self.impl.research_topic,
            str(payload.get("topic") or ""),
            language=str(payload.get("language") or context.language),
        )
        return _success(request, self.spec.name, {"research": to_jsonable(result)})

    async def _research_slide(self, request: AgentRequest, context: AgentContext) -> AgentResult:
        payload = request.payload
        result = await _invoke(
            self.impl.research_slide,
            payload.get("slide"),
            language=str(payload.get("language") or context.language),
            search_result_budget=payload.get("search_result_budget"),
            search_query=payload.get("search_query"),
            search_budget_reason=payload.get("search_budget_reason"),
        )
        return _success(request, self.spec.name, {"research": to_jsonable(result)})


async def _invoke(method: Any, *args: Any, **kwargs: Any) -> Any:
    if inspect.iscoroutinefunction(method):
        return await method(*args, **kwargs)
    result = await asyncio.to_thread(method, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _success(request: AgentRequest, agent_name: str, payload: dict[str, Any]) -> AgentResult:
    return AgentResult(
        run_id=request.run_id,
        task_id=request.task_id,
        agent_name=agent_name,
        capability=request.capability,
        status="success",
        payload=to_jsonable(payload),
    )


def _failed(request: AgentRequest, agent_name: str, exc: Exception) -> AgentResult:
    error_type = type(exc).__name__
    message = sanitize_agent_error_message(str(exc) or error_type)
    return AgentResult(
        run_id=request.run_id,
        task_id=request.task_id,
        agent_name=agent_name,
        capability=request.capability,
        status="failed",
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
                raw_excerpt=message[:200],
            )
        ],
    )


def _skipped(request: AgentRequest, agent_name: str, message: str) -> AgentResult:
    safe_message = sanitize_agent_error_message(message)
    return AgentResult(
        run_id=request.run_id,
        task_id=request.task_id,
        agent_name=agent_name,
        capability=request.capability,
        status="skipped",
        errors=[
            AgentError(
                error_type="UnsupportedCapability",
                message=safe_message,
                error_signature=build_agent_error_signature(
                    agent_name=agent_name,
                    capability=request.capability.value,
                    error_type="UnsupportedCapability",
                    message=safe_message,
                ),
            )
        ],
    )
