from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from backend.harness.tooling.errors import DuplicateToolError, ToolNotFoundError
from backend.harness.tooling.schema import ToolCall, ToolResult, ToolSpec

ToolHandler = Callable[[ToolCall], ToolResult | dict[str, Any] | Any]


@dataclass(frozen=True)
class RegisteredTool:
    spec: ToolSpec
    handler: ToolHandler


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if self.has(spec.name):
            raise DuplicateToolError(f"Tool already registered: {spec.name}")
        self._tools[spec.name] = RegisteredTool(spec=spec, handler=handler)

    def get(self, name: str) -> RegisteredTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(f"Tool is not registered: {name}") from exc

    def list_specs(self) -> list[ToolSpec]:
        return [registered.spec for _, registered in sorted(self._tools.items())]

    def has(self, name: str) -> bool:
        return name in self._tools


def create_default_tool_registry() -> ToolRegistry:
    from backend.harness.tooling.builtin_document_tools import register_document_tools
    from backend.harness.tooling.builtin_eval_tools import register_eval_tools
    from backend.harness.tooling.builtin_ppt_tools import register_ppt_tools
    from backend.harness.tooling.builtin_search_tools import register_search_tools

    registry = ToolRegistry()
    register_ppt_tools(registry)
    register_search_tools(registry)
    register_document_tools(registry)
    register_eval_tools(registry)
    return registry
