from backend.harness.agent_runtime.adapters import (
    AssetRuntimeAdapter,
    EvaluatorRuntimeAdapter,
    PlannerRuntimeAdapter,
    ResearchRuntimeAdapter,
)
from backend.harness.agent_runtime.errors import (
    AgentCapabilityError,
    AgentExecutionError,
    AgentNotFoundError,
    AgentRuntimeError,
    build_agent_error_signature,
)
from backend.harness.agent_runtime.executor import AgentExecutor
from backend.harness.agent_runtime.registry import AgentRegistry, RegisteredAgent
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


def create_agent_registry_from_instances(
    *,
    planner=None,
    researcher=None,
    asset_agent=None,
    evaluator=None,
) -> AgentRegistry:
    registry = AgentRegistry()
    if planner is not None:
        registry.register(PlannerRuntimeAdapter(planner))
    if researcher is not None:
        registry.register(ResearchRuntimeAdapter(researcher))
    if asset_agent is not None:
        registry.register(AssetRuntimeAdapter(asset_agent))
    if evaluator is not None:
        registry.register(EvaluatorRuntimeAdapter(evaluator))
    return registry


__all__ = [
    "AgentCapability",
    "AgentCapabilityError",
    "AgentContext",
    "AgentError",
    "AgentExecutionError",
    "AgentExecutor",
    "AgentNotFoundError",
    "AgentRegistry",
    "AgentRequest",
    "AgentResult",
    "AgentRole",
    "AgentRuntimeError",
    "AgentSpec",
    "AssetRuntimeAdapter",
    "EvaluatorRuntimeAdapter",
    "PlannerRuntimeAdapter",
    "RegisteredAgent",
    "ResearchRuntimeAdapter",
    "build_agent_error_signature",
    "create_agent_registry_from_instances",
    "to_jsonable",
]
