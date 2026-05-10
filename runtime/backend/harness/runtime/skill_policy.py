from __future__ import annotations

import json
from dataclasses import dataclass

from backend.harness.runtime.skill_loader import SkillLoader


@dataclass(frozen=True)
class SkillPolicyEntry:
    phase: str
    trigger_stage: str
    catalog_triggers: tuple[str, ...]
    audience_scopes: tuple[str, ...] = ()
    course_type_scopes: tuple[str, ...] = ()
    provider_scopes: tuple[str, ...] = ()
    language_scopes: tuple[str, ...] = ()
    max_items: int = 2
    heading: str = ""


class SkillPolicyStore:
    def __init__(self, loader: SkillLoader | None = None) -> None:
        self.loader = loader or SkillLoader()
        self._policy_map = self._load_policy_map()

    def get(self, *, phase: str, trigger_stage: str) -> SkillPolicyEntry:
        phase_map = self._policy_map.get(phase, {})
        raw = phase_map.get(trigger_stage, {})
        catalog_triggers = raw.get("catalog_triggers") if isinstance(raw, dict) else None
        if not isinstance(catalog_triggers, list) or not catalog_triggers:
            catalog_triggers = [trigger_stage]
        audience_scopes = raw.get("audience_scopes") if isinstance(raw, dict) else None
        course_type_scopes = raw.get("course_type_scopes") if isinstance(raw, dict) else None
        provider_scopes = raw.get("provider_scopes") if isinstance(raw, dict) else None
        language_scopes = raw.get("language_scopes") if isinstance(raw, dict) else None
        max_items = raw.get("max_items", 2) if isinstance(raw, dict) else 2
        heading = raw.get("heading", "") if isinstance(raw, dict) else ""
        return SkillPolicyEntry(
            phase=phase,
            trigger_stage=trigger_stage,
            catalog_triggers=tuple(str(item).strip() for item in catalog_triggers if str(item).strip()),
            audience_scopes=tuple(str(item).strip() for item in audience_scopes or [] if str(item).strip()),
            course_type_scopes=tuple(str(item).strip() for item in course_type_scopes or [] if str(item).strip()),
            provider_scopes=tuple(str(item).strip() for item in provider_scopes or [] if str(item).strip()),
            language_scopes=tuple(str(item).strip() for item in language_scopes or [] if str(item).strip()),
            max_items=max(int(max_items or 2), 1),
            heading=str(heading or "").strip(),
        )

    def export(self) -> dict[str, dict[str, dict[str, object]]]:
        return self._policy_map

    def _load_policy_map(self) -> dict[str, dict[str, dict[str, object]]]:
        try:
            raw = self.loader.read_reference("shared-core", "skill_policy.json")
        except FileNotFoundError:
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}

        normalized: dict[str, dict[str, dict[str, object]]] = {}
        for phase, value in data.items():
            if not isinstance(value, dict):
                continue
            phase_map: dict[str, dict[str, object]] = {}
            for trigger_stage, payload in value.items():
                if not isinstance(payload, dict):
                    continue
                phase_map[str(trigger_stage)] = {
                    "catalog_triggers": [
                        str(item).strip()
                        for item in payload.get("catalog_triggers", [])
                        if str(item).strip()
                    ],
                    "audience_scopes": [
                        str(item).strip()
                        for item in payload.get("audience_scopes", [])
                        if str(item).strip()
                    ],
                    "course_type_scopes": [
                        str(item).strip()
                        for item in payload.get("course_type_scopes", [])
                        if str(item).strip()
                    ],
                    "provider_scopes": [
                        str(item).strip()
                        for item in payload.get("provider_scopes", [])
                        if str(item).strip()
                    ],
                    "language_scopes": [
                        str(item).strip()
                        for item in payload.get("language_scopes", [])
                        if str(item).strip()
                    ],
                    "max_items": int(payload.get("max_items", 2) or 2),
                    "heading": str(payload.get("heading", "")).strip(),
                }
            normalized[str(phase)] = phase_map
        return normalized
