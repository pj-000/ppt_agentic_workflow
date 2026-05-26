from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.harness.agent_runtime.errors import AgentNotFoundError, AgentRuntimeError
from backend.harness.agent_runtime.schema import AgentSpec


@dataclass(frozen=True)
class RegisteredAgent:
    spec: AgentSpec
    runtime: Any


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, RegisteredAgent] = {}

    def register(self, agent_runtime: Any) -> None:
        spec = getattr(agent_runtime, "spec", None)
        run = getattr(agent_runtime, "run", None)
        if not isinstance(spec, AgentSpec):
            raise AgentRuntimeError("Agent runtime must expose an AgentSpec as .spec")
        if not callable(run):
            raise AgentRuntimeError("Agent runtime must expose async run(request, context)")
        if self.has(spec.name):
            raise AgentRuntimeError(f"Agent already registered: {spec.name}")
        self._agents[spec.name] = RegisteredAgent(spec=spec, runtime=agent_runtime)

    def get(self, name: str) -> RegisteredAgent:
        try:
            return self._agents[name]
        except KeyError as exc:
            raise AgentNotFoundError(f"Agent is not registered: {name}") from exc

    def has(self, name: str) -> bool:
        return name in self._agents

    def list_specs(self) -> list[AgentSpec]:
        return [registered.spec for _, registered in sorted(self._agents.items())]
