from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class SkillPhase(str, Enum):
    SHARED_CORE = "shared-core"
    DOCUMENT_UNDERSTANDING = "document-understanding"
    OUTLINE_PLANNING = "outline-planning"
    RESEARCH_SYNTHESIS = "research-synthesis"
    VISUAL_PRODUCTION = "visual-production"
    EVALUATION_AND_REPAIR = "evaluation-and-repair"


@dataclass(frozen=True)
class SkillReference:
    name: str
    path: Path


class SkillAssetKind(str, Enum):
    SKILL_DOC = "skill_doc"
    TEMPLATE = "template"
    REFERENCE = "reference"


@dataclass(frozen=True)
class SkillAssetSpec:
    name: str
    kind: SkillAssetKind
    path: Path
    prompt_mode: str = "static"
    purpose: str = ""
    injection_points: tuple[str, ...] = ()
    variables: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillSpec:
    name: str
    description: str
    phase: SkillPhase
    order: int
    root: Path
    skill_md: Path
    references: tuple[SkillReference, ...] = ()
    assets: tuple[SkillAssetSpec, ...] = ()
    metadata_path: Path | None = None


@dataclass
class RunContext:
    phase: SkillPhase
    topic: str = ""
    language: str = "中文"
    metadata: dict[str, Any] = field(default_factory=dict)
