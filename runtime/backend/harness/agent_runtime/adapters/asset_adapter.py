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


class AssetRuntimeAdapter:
    spec = AgentSpec(
        name="asset",
        role=AgentRole.ASSET,
        capabilities=[AgentCapability.FETCH_ASSETS, AgentCapability.FETCH_SLIDE_ASSET],
        description="Adapter for AssetAgent capabilities.",
    )

    def __init__(self, impl: Any):
        self.impl = impl

    async def run(self, request: AgentRequest, context: AgentContext) -> AgentResult:
        try:
            if request.capability == AgentCapability.FETCH_ASSETS:
                return await self._fetch_assets(request)
            if request.capability == AgentCapability.FETCH_SLIDE_ASSET:
                return _skipped(request, self.spec.name, "FETCH_SLIDE_ASSET is not wired to a public adapter method yet")
            return _skipped(request, self.spec.name, f"Unsupported capability: {request.capability.value}")
        except Exception as exc:
            return _failed(request, self.spec.name, exc)

    async def _fetch_assets(self, request: AgentRequest) -> AgentResult:
        payload = request.payload
        slides = list(payload.get("slides") or [])
        paths = await _invoke(
            self.impl.fetch_all,
            slides,
            str(payload.get("job_id") or request.run_id),
            concurrency=int(payload.get("concurrency") or 3),
        )
        image_paths = list(paths or [])
        return AgentResult(
            run_id=request.run_id,
            task_id=request.task_id,
            agent_name=self.spec.name,
            capability=request.capability,
            status="success",
            payload={"image_paths": to_jsonable(image_paths)},
            metrics={
                "slide_count": len(slides),
                "asset_count": sum(1 for item in image_paths if item),
            },
        )


async def _invoke(method: Any, *args: Any, **kwargs: Any) -> Any:
    if inspect.iscoroutinefunction(method):
        return await method(*args, **kwargs)
    result = await asyncio.to_thread(method, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


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
