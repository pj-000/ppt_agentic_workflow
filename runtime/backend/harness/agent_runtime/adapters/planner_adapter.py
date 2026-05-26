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


class PlannerRuntimeAdapter:
    spec = AgentSpec(
        name="planner",
        role=AgentRole.PLANNER,
        capabilities=[
            AgentCapability.PLAN_OUTLINE,
            AgentCapability.DECIDE_VISUAL_THEME,
            AgentCapability.GENERATE_SLIDE_CODE,
        ],
        description="Adapter for PlannerAgent capabilities.",
    )

    def __init__(self, impl: Any):
        self.impl = impl

    async def run(self, request: AgentRequest, context: AgentContext) -> AgentResult:
        try:
            if request.capability == AgentCapability.PLAN_OUTLINE:
                return await self._plan_outline(request, context)
            if request.capability == AgentCapability.DECIDE_VISUAL_THEME:
                return await self._decide_visual_theme(request, context)
            if request.capability == AgentCapability.GENERATE_SLIDE_CODE:
                return await self._generate_slide_code(request, context)
            return _skipped(request, self.spec.name, f"Unsupported capability: {request.capability.value}")
        except Exception as exc:
            return _failed(request, self.spec.name, exc)

    async def _plan_outline(self, request: AgentRequest, context: AgentContext) -> AgentResult:
        payload = request.payload
        outline = await _invoke(
            self.impl.plan_outline,
            payload.get("topic", ""),
            min_slides=int(payload.get("min_slides") or 6),
            max_slides=int(payload.get("max_slides") or 10),
            style=str(payload.get("style") or ""),
            audience=str(payload.get("audience") or "general"),
            language=str(payload.get("language") or context.language),
            content_requirements=str(payload.get("content_requirements") or ""),
        )
        return _success(
            request,
            self.spec.name,
            {"outline": to_jsonable(outline)},
            {"slide_count": _slide_count(outline)},
        )

    async def _decide_visual_theme(self, request: AgentRequest, context: AgentContext) -> AgentResult:
        payload = request.payload
        theme = await _invoke(
            self.impl.decide_visual_theme,
            payload.get("outline"),
            style=str(payload.get("style") or ""),
            audience=str(payload.get("audience") or "general"),
            language=str(payload.get("language") or context.language),
        )
        return _success(request, self.spec.name, {"theme": to_jsonable(theme)})

    async def _generate_slide_code(self, request: AgentRequest, context: AgentContext) -> AgentResult:
        payload = request.payload
        code = await _invoke(
            self.impl.plan_slide,
            payload.get("slide"),
            payload.get("theme") or {},
            payload.get("research"),
            payload.get("image_path"),
            layout_intent=payload.get("layout_intent"),
            prev_slides_summary=str(payload.get("prev_slides_summary") or ""),
            recent_layout_intents=payload.get("recent_layout_intents"),
            revision_feedback=payload.get("revision_feedback"),
            consistency_brief=str(payload.get("consistency_brief") or ""),
            content_requirements=str(payload.get("content_requirements") or ""),
            audience=str(payload.get("audience") or "general"),
            course_type=str(payload.get("course_type") or "*"),
            language=str(payload.get("language") or context.language),
            trigger_stage=str(payload.get("trigger_stage") or "slide_generation"),
            forced_retry_feedback=payload.get("forced_retry_feedback"),
            forced_error_signature=payload.get("forced_error_signature"),
            forced_error_message=str(payload.get("forced_error_message") or ""),
        )
        code_text = str(code or "")
        return _success(
            request,
            self.spec.name,
            {"slide_code": code_text},
            {"code_length": len(code_text)},
        )


async def _invoke(method: Any, *args: Any, **kwargs: Any) -> Any:
    if inspect.iscoroutinefunction(method):
        return await method(*args, **kwargs)
    result = await asyncio.to_thread(method, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _success(
    request: AgentRequest,
    agent_name: str,
    payload: dict[str, Any],
    metrics: dict[str, Any] | None = None,
) -> AgentResult:
    return AgentResult(
        run_id=request.run_id,
        task_id=request.task_id,
        agent_name=agent_name,
        capability=request.capability,
        status="success",
        payload=to_jsonable(payload),
        metrics=to_jsonable(metrics or {}),
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


def _slide_count(outline: Any) -> int:
    if isinstance(outline, dict):
        slides = outline.get("slides") or []
    else:
        slides = getattr(outline, "slides", []) or []
    try:
        return len(slides)
    except TypeError:
        return 0
