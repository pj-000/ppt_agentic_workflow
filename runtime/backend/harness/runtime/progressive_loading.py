from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.harness.runtime.context import SkillContext


@dataclass(frozen=True)
class PromptSection:
    source_type: str
    identifier: str
    content: str
    title: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not str(self.content or "").strip()

    def render(self) -> str:
        if self.is_empty():
            return ""
        if self.title.strip():
            return f"{self.title.strip()}\n{self.content.strip()}".strip()
        return str(self.content).strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "identifier": self.identifier,
            "title": self.title,
            "content_present": bool(str(self.content or "").strip()),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class LoadedSkillRecord:
    source: str
    item_id: str
    title: str
    summary: str
    trigger_stage: str
    error_signature: str = ""
    benchmark_id: str = ""
    memory_id: str = ""
    scopes: dict[str, str] = field(default_factory=dict)
    conditions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "item_id": self.item_id,
            "title": self.title,
            "summary": self.summary,
            "trigger_stage": self.trigger_stage,
            "error_signature": self.error_signature,
            "benchmark_id": self.benchmark_id,
            "memory_id": self.memory_id,
            "scopes": dict(self.scopes),
            "conditions": list(self.conditions),
        }


@dataclass(frozen=True)
class PromptBundle:
    text: str = ""
    sections: tuple[PromptSection, ...] = ()
    loaded_records: tuple[LoadedSkillRecord, ...] = ()
    runtime_memory_ids: tuple[str, ...] = ()

    def render(self) -> str:
        if self.sections:
            rendered = merge_prompt_sections(*self.sections)
            if rendered.strip():
                return rendered
        return self.text.strip()

    def to_trace_payload(
        self,
        *,
        mode: str,
        context: SkillContext,
        attempt: int | None = None,
        error_signature: str = "",
    ) -> dict[str, Any]:
        return {
            "mode": mode,
            "context": context.to_dict(),
            "attempt": attempt,
            "error_signature": error_signature,
            "text_present": bool(self.render().strip()),
            "runtime_memory_ids": list(self.runtime_memory_ids),
            "sections": [item.to_dict() for item in self.sections],
            "records": [item.to_dict() for item in self.loaded_records],
        }


def render_prompt_part(part: object) -> str:
    if part is None:
        return ""
    if isinstance(part, PromptSection):
        return part.render()
    if isinstance(part, PromptBundle):
        return part.render()
    return str(part).strip()


def merge_prompt_sections(*parts: object) -> str:
    rendered = [render_prompt_part(part) for part in parts]
    return "\n\n".join(item for item in rendered if item.strip())
