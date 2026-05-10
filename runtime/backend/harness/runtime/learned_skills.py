from __future__ import annotations

import json
from pathlib import Path

import config
from backend.harness.runtime.promoted_lessons import PromotedLessonStore, PromotedRepairLesson


class LearnedSkillStore:
    def __init__(
        self,
        root: Path | None = None,
        promoted_store: PromotedLessonStore | None = None,
    ) -> None:
        self.root = root or (config.DOCS_DIR / "harness" / "learned_skills")
        self.promoted_store = promoted_store or PromotedLessonStore()

    def sync_from_promotions(self) -> list[Path]:
        lessons = self.promoted_store.load_all()
        grouped: dict[str, list[PromotedRepairLesson]] = {}
        for lesson in lessons:
            grouped.setdefault(lesson.phase, []).append(lesson)

        written: list[Path] = []
        existing = set(self.root.glob("*.json")) if self.root.exists() else set()
        existing_md = set(self.root.glob("*.md")) if self.root.exists() else set()
        retained: set[Path] = set()

        for phase, items in grouped.items():
            path = self._phase_file(phase, suffix=".json")
            md_path = self._phase_file(phase, suffix=".md")
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "phase": phase,
                "lesson_count": len(items),
                "lessons": [self._serialize(item) for item in items],
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            md_path.write_text(self._render_phase_markdown(phase, items), encoding="utf-8")
            written.append(path)
            written.append(md_path)
            retained.add(path)
            retained.add(md_path)

        for stale in existing - retained:
            stale.unlink()
        for stale in existing_md - retained:
            stale.unlink()

        self._write_index(grouped)

        return written

    def load_phase_lessons(self, phase: str) -> list[PromotedRepairLesson]:
        path = self._phase_file(phase, suffix=".json")
        if not path.exists():
            return []

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []

        items = payload.get("lessons") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []

        lessons: list[PromotedRepairLesson] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                lessons.append(
                    PromotedRepairLesson(
                        title=str(item.get("title", "")).strip(),
                        phase=str(item.get("phase", phase)).strip(),
                        trigger_stage=str(item.get("trigger_stage", "*")).strip() or "*",
                        error_signature=str(item.get("error_signature", "")).strip(),
                        layout_scope=str(item.get("layout_scope", "*")).strip() or "*",
                        visual_mode_scope=str(item.get("visual_mode_scope", "*")).strip() or "*",
                        audience_scope=str(item.get("audience_scope", "*")).strip() or "*",
                        course_type_scope=str(item.get("course_type_scope", "*")).strip() or "*",
                        provider_scope=str(item.get("provider_scope", "*")).strip() or "*",
                        language_scope=str(item.get("language_scope", "*")).strip() or "*",
                        success_count=int(item.get("success_count", 0) or 0),
                        failure_count=int(item.get("failure_count", 0) or 0),
                        confidence=float(item.get("confidence", 0.0) or 0.0),
                        benchmark_id=str(item.get("benchmark_id", "")).strip(),
                        memory_id=str(item.get("memory_id", "")).strip(),
                        repair_instruction=str(item.get("repair_instruction", "")).strip(),
                        conditions=[str(x) for x in item.get("conditions", []) if str(x).strip()],
                        source_path=Path(item["source_path"]) if item.get("source_path") else None,
                    )
                )
            except Exception:
                continue
        return lessons

    def load_index(self) -> dict[str, object]:
        path = self.root / "index.json"
        if not path.exists():
            return {"phase_count": 0, "phases": []}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"phase_count": 0, "phases": []}
        if not isinstance(payload, dict):
            return {"phase_count": 0, "phases": []}
        phases = payload.get("phases")
        if not isinstance(phases, list):
            phases = []
        return {
            "phase_count": int(payload.get("phase_count", len(phases)) or 0),
            "phases": [item for item in phases if isinstance(item, dict)],
        }

    def list_phase_summaries(self) -> list[dict[str, object]]:
        payload = self.load_index()
        summaries: list[dict[str, object]] = []
        for item in payload.get("phases", []):
            phase = str(item.get("phase", "")).strip()
            if not phase:
                continue
            summaries.append(
                {
                    "phase": phase,
                    "lesson_count": int(item.get("lesson_count", 0) or 0),
                    "json_path": str(item.get("json_path", "")).strip(),
                    "markdown_path": str(item.get("markdown_path", "")).strip(),
                }
            )
        return summaries

    def phase_catalog(
        self,
        *,
        phase: str,
        trigger_stage: str | None = None,
        audience_scope: str | None = None,
        course_type_scope: str | None = None,
        provider_scope: str | None = None,
        language_scope: str | None = None,
        max_items: int = 6,
    ) -> list[dict[str, object]]:
        items = self.load_phase_lessons(phase)
        filtered: list[PromotedRepairLesson] = []
        for item in items:
            if trigger_stage and item.trigger_stage not in {trigger_stage, "*"}:
                continue
            if audience_scope and item.audience_scope not in {"*", audience_scope}:
                continue
            if course_type_scope and item.course_type_scope not in {"*", course_type_scope}:
                continue
            if provider_scope and item.provider_scope not in {"*", provider_scope}:
                continue
            if language_scope and item.language_scope not in {"*", language_scope}:
                continue
            filtered.append(item)
        filtered.sort(
            key=lambda lesson: (
                lesson.confidence,
                lesson.success_count - lesson.failure_count,
                lesson.benchmark_id,
                lesson.error_signature,
            ),
            reverse=True,
        )
        catalog: list[dict[str, object]] = []
        for item in filtered[:max_items]:
            catalog.append(
                {
                    "phase": item.phase,
                    "trigger_stage": item.trigger_stage,
                    "error_signature": item.error_signature,
                    "layout_scope": item.layout_scope,
                    "visual_mode_scope": item.visual_mode_scope,
                    "audience_scope": item.audience_scope,
                    "course_type_scope": item.course_type_scope,
                    "provider_scope": item.provider_scope,
                    "language_scope": item.language_scope,
                    "confidence": item.confidence,
                    "benchmark_id": item.benchmark_id,
                    "memory_id": item.memory_id,
                    "repair_instruction": item.repair_instruction,
                    "conditions": list(item.conditions),
                }
            )
        return catalog

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
        for lesson in self.load_phase_lessons(phase):
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

    def _phase_file(self, phase: str, *, suffix: str = ".json") -> Path:
        return self.root / f"{phase}{suffix}"

    def _write_index(self, grouped: dict[str, list[PromotedRepairLesson]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "phase_count": len(grouped),
            "phases": [
                {
                    "phase": phase,
                    "lesson_count": len(items),
                    "json_path": str(self._phase_file(phase, suffix=".json")),
                    "markdown_path": str(self._phase_file(phase, suffix=".md")),
                }
                for phase, items in sorted(grouped.items())
            ],
        }
        (self.root / "index.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _render_phase_markdown(phase: str, items: list[PromotedRepairLesson]) -> str:
        lines = [
            f"# Learned Skills: {phase}",
            "",
            f"- phase: {phase}",
            f"- lesson_count: {len(items)}",
            "",
        ]
        grouped_by_stage: dict[str, list[PromotedRepairLesson]] = {}
        for item in items:
            grouped_by_stage.setdefault(item.trigger_stage, []).append(item)

        for trigger_stage, stage_items in sorted(grouped_by_stage.items()):
            lines.append(f"## {trigger_stage}")
            lines.append("")
            for item in sorted(
                stage_items,
                key=lambda lesson: (
                    lesson.confidence,
                    lesson.success_count - lesson.failure_count,
                    lesson.title,
                ),
                reverse=True,
            ):
                lines.append(f"### {item.error_signature}")
                lines.append("")
                lines.append(f"- layout_scope: {item.layout_scope}")
                lines.append(f"- visual_mode_scope: {item.visual_mode_scope}")
                lines.append(f"- audience_scope: {item.audience_scope}")
                lines.append(f"- course_type_scope: {item.course_type_scope}")
                lines.append(f"- provider_scope: {item.provider_scope}")
                lines.append(f"- language_scope: {item.language_scope}")
                lines.append(f"- success_count: {item.success_count}")
                lines.append(f"- failure_count: {item.failure_count}")
                lines.append(f"- confidence: {item.confidence}")
                lines.append(f"- benchmark_id: {item.benchmark_id or '(none)'}")
                lines.append(f"- memory_id: {item.memory_id or '(none)'}")
                lines.append("")
                lines.append("Repair Instruction:")
                lines.append(item.repair_instruction)
                lines.append("")
                lines.append("Conditions:")
                if item.conditions:
                    lines.extend(f"- {condition}" for condition in item.conditions)
                else:
                    lines.append("- (none)")
                lines.append("")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _serialize(item: PromotedRepairLesson) -> dict[str, object]:
        return {
            "title": item.title,
            "phase": item.phase,
            "trigger_stage": item.trigger_stage,
            "error_signature": item.error_signature,
            "layout_scope": item.layout_scope,
            "visual_mode_scope": item.visual_mode_scope,
            "audience_scope": item.audience_scope,
            "course_type_scope": item.course_type_scope,
            "provider_scope": item.provider_scope,
            "language_scope": item.language_scope,
            "success_count": item.success_count,
            "failure_count": item.failure_count,
            "confidence": item.confidence,
            "benchmark_id": item.benchmark_id,
            "memory_id": item.memory_id,
            "repair_instruction": item.repair_instruction,
            "conditions": list(item.conditions),
            "source_path": str(item.source_path) if item.source_path else "",
        }
