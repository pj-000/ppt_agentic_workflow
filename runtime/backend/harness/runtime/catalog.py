from __future__ import annotations

import functools
import json
from typing import Any

from backend.harness.runtime.learned_skills import LearnedSkillStore
from backend.harness.runtime.skill_policy import SkillPolicyStore
from backend.harness.runtime.skill_loader import SkillLoader

_LOADER = SkillLoader()
_POLICY_STORE = SkillPolicyStore(_LOADER)


@functools.lru_cache(maxsize=64)
def _load_json(skill_name: str, filename: str) -> Any:
    return json.loads(_LOADER.read_reference(skill_name, filename))


def get_supported_styles() -> tuple[str, ...]:
    return tuple(_load_json("outline-planning", "supported_styles.json"))


def get_supported_audiences() -> tuple[str, ...]:
    return tuple(_load_json("outline-planning", "supported_audiences.json"))


def get_audience_aliases() -> dict[str, str]:
    return dict(_load_json("outline-planning", "audience_aliases.json"))


def get_audience_profiles() -> dict[str, str]:
    return dict(_load_json("outline-planning", "audience_profiles.json"))


def get_shape_value_map() -> dict[str, str]:
    return dict(_load_json("visual-production", "shape_value_map.json"))


def get_js_diagram_hints() -> tuple[str, ...]:
    return tuple(_load_json("outline-planning", "js_diagram_hints.json"))


def get_generated_image_hints() -> tuple[str, ...]:
    return tuple(_load_json("outline-planning", "generated_image_hints.json"))


def get_evaluation_metric_aliases() -> dict[str, str]:
    return dict(_load_json("evaluation-and-repair", "evaluation_metric_aliases.json"))


def get_default_principle_descriptions() -> dict[str, str]:
    return dict(_load_json("evaluation-and-repair", "default_principle_descriptions.json"))


def get_learned_skill_registry() -> list[dict[str, object]]:
    return LearnedSkillStore().list_phase_summaries()


def get_skill_asset_registry() -> list[dict[str, object]]:
    return _LOADER.asset_registry()


def get_skill_policy_map() -> dict[str, dict[str, dict[str, object]]]:
    return _POLICY_STORE.export()
