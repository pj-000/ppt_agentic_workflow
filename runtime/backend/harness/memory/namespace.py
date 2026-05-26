from __future__ import annotations

import re


PLANNER_OUTLINE = "planner:outline"
PLANNER_THEME = "planner:theme"
PLANNER_SLIDE_CODE = "planner:slide_code"
RESEARCH_SYNTHESIS = "research:synthesis"
ASSET_IMAGE_ACQUISITION = "asset:image_acquisition"
EVALUATOR_VISUAL_RUBRIC = "evaluator:visual_rubric"
EVALUATOR_CONTENT_RUBRIC = "evaluator:content_rubric"
REPAIR_VISUAL = "repair:visual"
REPAIR_CONTENT = "repair:content"
REPAIR_TOOL = "repair:tool"
ORCHESTRATOR_EPISODE = "orchestrator:episode"
ORCHESTRATOR_PLAN_POLICY = "orchestrator:plan_policy"
SEMANTIC_PPT_DESIGN = "semantic:ppt_design"
SEMANTIC_COURSEWARE = "semantic:courseware"
SEMANTIC_DOMAIN_KNOWLEDGE = "semantic:domain_knowledge"

_NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9_:/-]+$")
_SECRET_PATTERN = re.compile(r"(?i)(api[_-]?key|secret|password|token|authorization|sk-[A-Za-z0-9_-]{8,})")


def validate_namespace(namespace: str) -> str:
    value = str(namespace or "").strip()
    if not value:
        raise ValueError("memory namespace must not be empty")
    if not _NAMESPACE_PATTERN.fullmatch(value):
        raise ValueError(f"unsafe memory namespace: {value}")
    if value.startswith("/") or value.endswith("/") or "//" in value or ".." in value or "\\" in value:
        raise ValueError("memory namespace must not look like a filesystem path")
    if _SECRET_PATTERN.search(value):
        raise ValueError("memory namespace must not contain secret-like text")
    return value


def namespace_to_filename(namespace: str) -> str:
    safe = validate_namespace(namespace)
    return safe.replace(":", "__").replace("/", "__")
