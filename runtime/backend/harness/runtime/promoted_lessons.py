from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import config


@dataclass(frozen=True)
class PromotedRepairLesson:
    title: str
    phase: str
    trigger_stage: str
    error_signature: str
    layout_scope: str = "*"
    visual_mode_scope: str = "*"
    success_count: int = 0
    failure_count: int = 0
    confidence: float = 0.0
    benchmark_id: str = ""
    memory_id: str = ""
    repair_instruction: str = ""
    conditions: list[str] = field(default_factory=list)
    audience_scope: str = "*"
    course_type_scope: str = "*"
    provider_scope: str = "*"
    language_scope: str = "*"
    source_path: Path | None = None


class PromotedLessonStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or (config.DOCS_DIR / "harness" / "promotions")

    def load_all(self) -> list[PromotedRepairLesson]:
        if not self.root.exists():
            return []

        lessons: list[PromotedRepairLesson] = []
        for path in sorted(self.root.glob("*.md")):
            lesson = self._parse_candidate(path)
            if lesson is not None:
                lessons.append(lesson)
        return lessons

    def match_lessons(
        self,
        *,
        phase: str,
        trigger_stage: str,
        layout_scope: str | None = None,
        visual_mode_scope: str | None = None,
        audience_scope: str | None = None,
        course_type_scope: str | None = None,
        provider_scope: str | None = None,
        language_scope: str | None = None,
        error_signature: str | None = None,
        max_items: int = 3,
    ) -> list[PromotedRepairLesson]:
        matched: list[PromotedRepairLesson] = []
        for lesson in self.load_all():
            if lesson.phase != phase:
                continue
            if lesson.trigger_stage not in {trigger_stage, "*"}:
                continue
            if error_signature and lesson.error_signature not in {error_signature, "*"}:
                continue
            if layout_scope and lesson.layout_scope not in {"*", layout_scope}:
                continue
            if visual_mode_scope and lesson.visual_mode_scope not in {"*", visual_mode_scope}:
                continue
            if audience_scope and lesson.audience_scope not in {"*", audience_scope}:
                continue
            if course_type_scope and lesson.course_type_scope not in {"*", course_type_scope}:
                continue
            if provider_scope and lesson.provider_scope not in {"*", provider_scope}:
                continue
            if language_scope and lesson.language_scope not in {"*", language_scope}:
                continue
            matched.append(lesson)

        matched.sort(
            key=lambda item: (
                item.confidence,
                item.success_count - item.failure_count,
                item.benchmark_id,
                item.title,
            ),
            reverse=True,
        )
        return matched[:max_items]

    def _parse_candidate(self, path: Path) -> PromotedRepairLesson | None:
        text = path.read_text(encoding="utf-8")
        stripped = text.strip()
        if not stripped:
            return None

        lines = stripped.splitlines()
        title = lines[0].removeprefix("#").strip() if lines else path.stem
        metadata = self._parse_metadata(lines)
        repair_instruction = self._parse_section(text, "Repair Instruction")
        conditions = self._parse_bullets(self._parse_section(text, "Conditions"))

        if not repair_instruction:
            return None

        return PromotedRepairLesson(
            title=title,
            phase=metadata.get("phase", "").strip(),
            trigger_stage=metadata.get("trigger_stage", "*").strip() or "*",
            error_signature=metadata.get("error_signature", path.stem).strip(),
            layout_scope=metadata.get("layout_scope", "*").strip() or "*",
            visual_mode_scope=metadata.get("visual_mode_scope", "*").strip() or "*",
            success_count=self._parse_int(metadata.get("success_count")),
            failure_count=self._parse_int(metadata.get("failure_count")),
            confidence=self._parse_float(metadata.get("confidence")),
            benchmark_id=metadata.get("benchmark_id", "").strip(),
            memory_id=metadata.get("memory_id", "").strip(),
            repair_instruction=repair_instruction,
            conditions=conditions,
            audience_scope=metadata.get("audience_scope", "*").strip() or "*",
            course_type_scope=metadata.get("course_type_scope", "*").strip() or "*",
            provider_scope=metadata.get("provider_scope", "*").strip() or "*",
            language_scope=metadata.get("language_scope", "*").strip() or "*",
            source_path=path,
        )

    @staticmethod
    def _parse_metadata(lines: list[str]) -> dict[str, str]:
        metadata: dict[str, str] = {}
        for line in lines[1:]:
            stripped = line.strip()
            if stripped.startswith("## "):
                break
            if not stripped.startswith("- ") or ":" not in stripped:
                continue
            key, value = stripped[2:].split(":", 1)
            metadata[key.strip()] = value.strip()
        return metadata

    @staticmethod
    def _parse_section(text: str, heading: str) -> str:
        marker = f"## {heading}"
        start = text.find(marker)
        if start < 0:
            return ""
        start += len(marker)
        remainder = text[start:].lstrip()
        next_heading = remainder.find("\n## ")
        if next_heading >= 0:
            remainder = remainder[:next_heading]
        return remainder.strip()

    @staticmethod
    def _parse_bullets(text: str) -> list[str]:
        items: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                items.append(stripped[2:].strip())
        return items

    @staticmethod
    def _parse_int(raw: str | None) -> int:
        try:
            return int(float(raw or 0))
        except Exception:
            return 0

    @staticmethod
    def _parse_float(raw: str | None) -> float:
        try:
            return float(raw or 0.0)
        except Exception:
            return 0.0
