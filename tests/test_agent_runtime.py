from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from backend.harness.agent_runtime import (  # noqa: E402
    AgentCapability,
    AgentContext,
    AgentError,
    AgentExecutor,
    AgentNotFoundError,
    AgentRegistry,
    AgentRequest,
    AgentResult,
    AgentRole,
    AgentRuntimeError,
    AgentSpec,
    AssetRuntimeAdapter,
    EvaluatorRuntimeAdapter,
    PlannerRuntimeAdapter,
    ResearchRuntimeAdapter,
    create_agent_registry_from_instances,
    to_jsonable,
)
from backend.harness.observability import ObservabilityTraceAdapter, TraceStore  # noqa: E402


def _context() -> AgentContext:
    return AgentContext(run_id="run_agent", language="zh-CN", model_provider="fake")


def _request(capability: AgentCapability, payload: dict[str, Any] | None = None) -> AgentRequest:
    return AgentRequest(
        run_id="run_agent",
        task_id=f"task_{capability.value}",
        capability=capability,
        payload=payload or {},
    )


class FakeRuntime:
    spec = AgentSpec(
        name="fake",
        role=AgentRole.UNKNOWN,
        capabilities=[AgentCapability.UNKNOWN],
        description="Fake runtime",
    )

    async def run(self, request: AgentRequest, context: AgentContext) -> AgentResult:
        return AgentResult(
            run_id=request.run_id,
            task_id=request.task_id,
            agent_name=self.spec.name,
            capability=request.capability,
            status="success",
            payload={"ok": True, "language": context.language},
        )


def test_agent_schema_models_serialize() -> None:
    spec = AgentSpec(
        name="planner",
        role=AgentRole.PLANNER,
        capabilities=[AgentCapability.PLAN_OUTLINE],
        metadata={"owner": "harness"},
    )
    context = AgentContext(run_id="run", budget={"tokens": 100})
    request = AgentRequest(
        run_id="run",
        task_id="task",
        capability=AgentCapability.PLAN_OUTLINE,
        payload={"topic": "AI"},
        input_artifacts={"doc": "input.md"},
    )
    result = AgentResult(
        run_id="run",
        task_id="task",
        agent_name="planner",
        capability=AgentCapability.PLAN_OUTLINE,
        status="failed",
        metrics={"latency_ms": 1},
        output_artifacts={"outline": "outline.json"},
        errors=[
            AgentError(
                error_type="ValueError",
                message="bad outline",
                error_signature="planner:plan_outline:ValueError:invalid_outline_json",
            )
        ],
    )

    assert AgentSpec.model_validate_json(spec.model_dump_json()).role == AgentRole.PLANNER
    assert AgentContext.model_validate_json(context.model_dump_json()).budget["tokens"] == 100
    assert AgentRequest.model_validate_json(request.model_dump_json()).payload["topic"] == "AI"
    loaded = AgentResult.model_validate_json(result.model_dump_json())
    assert loaded.errors[0].error_type == "ValueError"
    assert loaded.output_artifacts["outline"] == "outline.json"


def test_to_jsonable_supports_common_structures(tmp_path: Path) -> None:
    class Color(Enum):
        BLUE = "blue"

    class Model(BaseModel):
        value: int

    @dataclass
    class Item:
        path: Path
        payload: bytes

    value = {
        "model": Model(value=3),
        "item": Item(path=tmp_path / "file.txt", payload=b"abc"),
        "enum": Color.BLUE,
        "nested": [{"values": {1, 2}}],
    }

    result = to_jsonable(value)

    assert result["model"] == {"value": 3}
    assert result["item"]["path"].endswith("file.txt")
    assert result["item"]["payload"] == "<bytes:3>"
    assert result["enum"] == "blue"
    assert sorted(result["nested"][0]["values"]) == [1, 2]


def test_agent_registry_registers_lists_and_rejects_errors() -> None:
    registry = AgentRegistry()
    runtime = FakeRuntime()

    registry.register(runtime)

    assert registry.has("fake")
    assert registry.get("fake").runtime is runtime
    assert [spec.name for spec in registry.list_specs()] == ["fake"]
    with pytest.raises(AgentRuntimeError):
        registry.register(runtime)
    with pytest.raises(AgentNotFoundError):
        registry.get("missing")


def test_agent_executor_executes_fake_success_runtime() -> None:
    registry = AgentRegistry()
    registry.register(FakeRuntime())

    result = asyncio.run(
        AgentExecutor(registry).execute(
            agent_name="fake",
            request=_request(AgentCapability.UNKNOWN),
            context=_context(),
        )
    )

    assert result.status == "success"
    assert result.payload["ok"] is True
    assert result.metrics["latency_ms"] >= 0


def test_agent_executor_converts_runtime_exception_to_failed_result() -> None:
    class FailingRuntime(FakeRuntime):
        async def run(self, request: AgentRequest, context: AgentContext) -> AgentResult:
            raise ValueError("bad input api_key=sk-secret123456789 from /private/tmp/file.py")

    registry = AgentRegistry()
    registry.register(FailingRuntime())

    result = asyncio.run(
        AgentExecutor(registry).execute(
            agent_name="fake",
            request=_request(AgentCapability.UNKNOWN),
            context=_context(),
        )
    )

    assert result.status == "failed"
    assert result.errors[0].error_type == "ValueError"
    assert "sk-secret123456789" not in result.errors[0].message
    assert "/private/tmp" not in result.errors[0].error_signature


def test_agent_executor_returns_structured_skipped_for_unsupported_capability() -> None:
    registry = AgentRegistry()
    registry.register(FakeRuntime())

    result = asyncio.run(
        AgentExecutor(registry).execute(
            agent_name="fake",
            request=_request(AgentCapability.PLAN_OUTLINE),
            context=_context(),
        )
    )

    assert result.status == "skipped"
    assert result.errors[0].error_type == "AgentCapabilityError"
    assert result.errors[0].error_signature is not None


def test_agent_executor_writes_observability_trace(tmp_path: Path) -> None:
    registry = AgentRegistry()
    registry.register(FakeRuntime())
    store = TraceStore(tmp_path)
    trace = ObservabilityTraceAdapter("run_agent", store)

    result = asyncio.run(
        AgentExecutor(registry, trace=trace).execute(
            agent_name="fake",
            request=_request(AgentCapability.UNKNOWN),
            context=_context(),
        )
    )
    events = store.load("run_agent")

    assert result.status == "success"
    assert [event.event_type for event in events] == ["agent.started", "agent.finished"]
    assert events[-1].agent_name == "fake"
    assert events[-1].status == "success"
    assert "latency_ms" in events[-1].metrics


def test_agent_executor_ignores_trace_failures() -> None:
    class FailingTrace:
        def record(self, *, stage: str, payload: dict) -> None:
            raise RuntimeError("trace down")

    registry = AgentRegistry()
    registry.register(FakeRuntime())

    result = asyncio.run(
        AgentExecutor(registry, trace=FailingTrace()).execute(
            agent_name="fake",
            request=_request(AgentCapability.UNKNOWN),
            context=_context(),
        )
    )

    assert result.status == "success"


@dataclass
class FakeOutline:
    title: str
    slides: list[dict[str, Any]]


class FakePlanner:
    def plan_outline(self, topic: str, **kwargs: Any) -> FakeOutline:
        return FakeOutline(title=topic, slides=[{"topic": "intro"}, {"topic": "concepts"}])

    def decide_visual_theme(self, outline: Any, **kwargs: Any) -> dict[str, Any]:
        return {"primary_color": "123456", "outline_title": outline["title"] if isinstance(outline, dict) else "ok"}

    def plan_slide(self, slide: Any, theme: dict, research: Any, image_path: str | None, **kwargs: Any) -> str:
        return "slide.addText('hello');"


def test_planner_adapter_plan_outline() -> None:
    result = asyncio.run(
        PlannerRuntimeAdapter(FakePlanner()).run(
            _request(AgentCapability.PLAN_OUTLINE, {"topic": "AI", "min_slides": 2, "max_slides": 3}),
            _context(),
        )
    )

    assert result.status == "success"
    assert result.payload["outline"]["title"] == "AI"
    assert result.metrics["slide_count"] == 2


def test_planner_adapter_decide_visual_theme() -> None:
    result = asyncio.run(
        PlannerRuntimeAdapter(FakePlanner()).run(
            _request(AgentCapability.DECIDE_VISUAL_THEME, {"outline": {"title": "AI"}}),
            _context(),
        )
    )

    assert result.status == "success"
    assert result.payload["theme"]["primary_color"] == "123456"


def test_planner_adapter_generate_slide_code() -> None:
    result = asyncio.run(
        PlannerRuntimeAdapter(FakePlanner()).run(
            _request(
                AgentCapability.GENERATE_SLIDE_CODE,
                {"slide": {"topic": "intro"}, "theme": {}, "research": {}, "image_path": None},
            ),
            _context(),
        )
    )

    assert result.status == "success"
    assert result.payload["slide_code"] == "slide.addText('hello');"
    assert result.metrics["code_length"] == len("slide.addText('hello');")


def test_planner_adapter_exception_returns_failed() -> None:
    class BadPlanner(FakePlanner):
        def plan_outline(self, topic: str, **kwargs: Any) -> FakeOutline:
            raise ValueError("invalid outline json")

    result = asyncio.run(
        PlannerRuntimeAdapter(BadPlanner()).run(_request(AgentCapability.PLAN_OUTLINE, {"topic": "AI"}), _context())
    )

    assert result.status == "failed"
    assert result.errors[0].error_type == "ValueError"


class FakeAsyncResearch:
    async def research_topic(self, topic: str, language: str = "zh-CN") -> dict[str, Any]:
        return {"topic": topic, "language": language}

    async def research_slide(self, slide: Any, **kwargs: Any) -> dict[str, Any]:
        return {"slide": slide, "query": kwargs.get("search_query")}


class FakeSyncResearch:
    def research_topic(self, topic: str, language: str = "zh-CN") -> dict[str, Any]:
        return {"topic": topic, "mode": "sync"}

    def research_slide(self, slide: Any, **kwargs: Any) -> dict[str, Any]:
        return {"slide": slide, "mode": "sync"}


def test_research_adapter_research_topic_async() -> None:
    result = asyncio.run(
        ResearchRuntimeAdapter(FakeAsyncResearch()).run(
            _request(AgentCapability.RESEARCH_TOPIC, {"topic": "AI", "language": "zh-CN"}),
            _context(),
        )
    )

    assert result.status == "success"
    assert result.payload["research"]["topic"] == "AI"


def test_research_adapter_research_slide_async() -> None:
    result = asyncio.run(
        ResearchRuntimeAdapter(FakeAsyncResearch()).run(
            _request(AgentCapability.RESEARCH_SLIDE, {"slide": {"topic": "AI"}, "search_query": "AI trends"}),
            _context(),
        )
    )

    assert result.status == "success"
    assert result.payload["research"]["query"] == "AI trends"


def test_research_adapter_sync_impl_is_supported() -> None:
    result = asyncio.run(
        ResearchRuntimeAdapter(FakeSyncResearch()).run(
            _request(AgentCapability.RESEARCH_TOPIC, {"topic": "AI"}),
            _context(),
        )
    )

    assert result.status == "success"
    assert result.payload["research"]["mode"] == "sync"


class FakeAsset:
    async def fetch_all(self, slides: list[Any], job_id: str, concurrency: int = 3) -> list[str | None]:
        return [f"{job_id}/slide-{index}.png" if index % 2 == 0 else None for index, _ in enumerate(slides)]


def test_asset_adapter_fetch_assets() -> None:
    result = asyncio.run(
        AssetRuntimeAdapter(FakeAsset()).run(
            _request(AgentCapability.FETCH_ASSETS, {"slides": [{}, {}, {}], "job_id": "job", "concurrency": 2}),
            _context(),
        )
    )

    assert result.status == "success"
    assert result.payload["image_paths"] == ["job/slide-0.png", None, "job/slide-2.png"]
    assert result.metrics["slide_count"] == 3
    assert result.metrics["asset_count"] == 2


def test_asset_adapter_fetch_slide_asset_is_skipped() -> None:
    result = asyncio.run(
        AssetRuntimeAdapter(FakeAsset()).run(_request(AgentCapability.FETCH_SLIDE_ASSET, {"slide": {}}), _context())
    )

    assert result.status == "skipped"
    assert result.errors[0].error_type == "UnsupportedCapability"


class FakeEvaluator:
    def evaluate_all(self, image_paths: list[str], outline: Any, slide_indices: list[int] | None = None) -> list[dict]:
        indices = slide_indices or list(range(len(image_paths)))
        return [{"slide_index": index, "overall": 4.0} for index in indices]


def test_evaluator_adapter_evaluate_visual() -> None:
    result = asyncio.run(
        EvaluatorRuntimeAdapter(FakeEvaluator()).run(
            _request(
                AgentCapability.EVALUATE_VISUAL,
                {"image_paths": ["a.png", "b.png"], "outline": {"slides": [1, 2]}, "slide_indices": [1]},
            ),
            _context(),
        )
    )

    assert result.status == "success"
    assert result.payload["eval_results"] == [{"slide_index": 1, "overall": 4.0}]
    assert result.metrics["evaluated_slide_count"] == 1


def test_evaluator_adapter_evaluate_content_is_skipped() -> None:
    result = asyncio.run(
        EvaluatorRuntimeAdapter(FakeEvaluator()).run(_request(AgentCapability.EVALUATE_CONTENT, {"text": "AI"}), _context())
    )

    assert result.status == "skipped"
    assert result.errors[0].error_type == "UnsupportedCapability"


def test_create_agent_registry_from_instances_registers_only_provided_instances() -> None:
    registry = create_agent_registry_from_instances(planner=FakePlanner(), evaluator=FakeEvaluator())
    names = {spec.name for spec in registry.list_specs()}

    assert names == {"planner", "evaluator"}
    assert registry.has("planner")
    assert registry.has("researcher") is False


def test_create_agent_registry_from_instances_empty_registry() -> None:
    registry = create_agent_registry_from_instances()

    assert registry.list_specs() == []
