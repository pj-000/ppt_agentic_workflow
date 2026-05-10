from __future__ import annotations

from dataclasses import dataclass


_WILDCARD = "*"


def _normalize(value: str | None) -> str:
    text = str(value or "").strip()
    return text or _WILDCARD


@dataclass(frozen=True)
class SkillContext:
    phase: str
    trigger_stage: str
    layout_scope: str = _WILDCARD
    visual_mode_scope: str = _WILDCARD
    audience: str = _WILDCARD
    course_type: str = _WILDCARD
    provider: str = _WILDCARD
    language: str = _WILDCARD

    def __post_init__(self) -> None:
        object.__setattr__(self, "phase", _normalize(self.phase))
        object.__setattr__(self, "trigger_stage", _normalize(self.trigger_stage))
        object.__setattr__(self, "layout_scope", _normalize(self.layout_scope))
        object.__setattr__(self, "visual_mode_scope", _normalize(self.visual_mode_scope))
        object.__setattr__(self, "audience", _normalize(self.audience))
        object.__setattr__(self, "course_type", _normalize(self.course_type))
        object.__setattr__(self, "provider", _normalize(self.provider))
        object.__setattr__(self, "language", _normalize(self.language))

    def to_dict(self) -> dict[str, str]:
        return {
            "phase": self.phase,
            "trigger_stage": self.trigger_stage,
            "layout_scope": self.layout_scope,
            "visual_mode_scope": self.visual_mode_scope,
            "audience": self.audience,
            "course_type": self.course_type,
            "provider": self.provider,
            "model_id": self.provider,
            "language": self.language,
        }

    @property
    def model_id(self) -> str:
        return self.provider

    def scope_parts(self) -> list[str]:
        parts: list[str] = []
        if self.layout_scope != _WILDCARD:
            parts.append(f"layout={self.layout_scope}")
        if self.visual_mode_scope != _WILDCARD:
            parts.append(f"visual_mode={self.visual_mode_scope}")
        if self.audience != _WILDCARD:
            parts.append(f"audience={self.audience}")
        if self.course_type != _WILDCARD:
            parts.append(f"course_type={self.course_type}")
        if self.provider != _WILDCARD:
            parts.append(f"model={self.provider}")
        if self.language != _WILDCARD:
            parts.append(f"language={self.language}")
        return parts
