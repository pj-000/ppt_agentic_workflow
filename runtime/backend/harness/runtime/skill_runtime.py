from __future__ import annotations

from pathlib import Path

from backend.harness.runtime.context import SkillContext
from backend.harness.runtime.learned_skills import LearnedSkillStore
from backend.harness.runtime.progressive_loading import (
    LoadedSkillRecord,
    PromptBundle,
    PromptSection,
)
from backend.harness.runtime.promoted_lessons import PromotedLessonStore, PromotedRepairLesson
from backend.harness.runtime.runtime_memory import RepairMemoryRecord, RuntimeMemoryStore
from backend.harness.runtime.skill_policy import SkillPolicyStore
from backend.harness.runtime.skill_loader import SkillLoader


class SkillRuntime:
    PREVENTION_MAX_ITEMS = 2
    REPAIR_MAX_ITEMS = 1
    MAX_INSTRUCTION_CHARS = 72
    MAX_CONDITION_CHARS = 48

    def __init__(
        self,
        loader: SkillLoader | None = None,
        memory_store: RuntimeMemoryStore | None = None,
        promoted_store: PromotedLessonStore | None = None,
        learned_store: LearnedSkillStore | None = None,
        policy_store: SkillPolicyStore | None = None,
    ) -> None:
        self.loader = loader or SkillLoader()
        self.memory_store = memory_store or RuntimeMemoryStore()
        self.promoted_store = promoted_store or PromotedLessonStore()
        self.learned_store = learned_store or LearnedSkillStore(promoted_store=self.promoted_store)
        self.policy_store = policy_store or SkillPolicyStore(self.loader)
        self._ephemeral_memories: dict[str, list[RepairMemoryRecord]] = {}

    def shared_heading(self, template_name: str, fallback: str) -> str:
        try:
            return self.load_template("shared-core", template_name).strip() or fallback
        except FileNotFoundError:
            return fallback

    def shared_text(self, template_name: str, fallback: str = "") -> str:
        try:
            return self.load_template("shared-core", template_name).strip() or fallback
        except FileNotFoundError:
            return fallback

    def load_reference(self, skill_name: str, reference_name: str) -> str:
        return self.loader.read_reference(skill_name, reference_name)

    def load_template(self, skill_name: str, template_name: str) -> str:
        spec = self.loader.get_skill(skill_name)
        path = spec.root / "templates" / template_name
        if path.exists():
            return path.read_text(encoding="utf-8")
        return self.loader.read_reference(skill_name, template_name)

    def render_template(
        self,
        skill_name: str,
        template_name: str,
        variables: dict[str, str | int | float],
    ) -> str:
        rendered = self.load_template(skill_name, template_name)
        for key, value in variables.items():
            rendered = rendered.replace(f"{{{key}}}", str(value))
        return rendered

    def render_runtime_memory_section(
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
        heading: str,
        max_items: int | None = None,
    ) -> str:
        memories = self.match_runtime_memories(
            phase=phase,
            trigger_stage=trigger_stage,
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
            audience_scope=audience_scope,
            course_type_scope=course_type_scope,
            provider_scope=provider_scope,
            language_scope=language_scope,
            error_signature=error_signature,
            max_items=max_items or self._default_limit(error_signature=error_signature),
        )
        return self._render_runtime_records(memories, heading=heading)

    def render_promoted_lessons_section(
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
        heading: str,
        max_items: int | None = None,
    ) -> str:
        lessons = self.match_promoted_lessons(
            phase=phase,
            trigger_stage=trigger_stage,
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
            audience_scope=audience_scope,
            course_type_scope=course_type_scope,
            provider_scope=provider_scope,
            language_scope=language_scope,
            error_signature=error_signature,
            max_items=max_items or self._default_limit(error_signature=error_signature),
        )
        if not lessons:
            return ""

        lines = [heading]
        for item in lessons:
            suffix_parts = []
            if item.benchmark_id:
                suffix_parts.append(f"benchmark={item.benchmark_id}")
            suffix_parts.extend(
                self._scope_parts(
                    item.layout_scope,
                    item.visual_mode_scope,
                    item.audience_scope,
                    item.course_type_scope,
                    item.provider_scope,
                    item.language_scope,
                )
            )
            condition_hint = self._condition_hint(item.conditions)
            if condition_hint:
                suffix_parts.append(condition_hint)
            suffix_text = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
            lines.append(f"- {self._compact_text(item.repair_instruction)}{suffix_text}")
        return "\n".join(lines)

    def render_learned_skill_catalog_section(
        self,
        *,
        phase: str,
        trigger_stage: str | None = None,
        heading: str,
        max_items: int = 4,
        audience_scope: str | None = None,
        course_type_scope: str | None = None,
        provider_scope: str | None = None,
        language_scope: str | None = None,
    ) -> str:
        policy = self.policy_store.get(
            phase=phase,
            trigger_stage=trigger_stage or "*",
        )
        resolved_heading = heading or policy.heading or self.shared_heading(
            "learned_catalog_heading.txt",
            "## 长期技能目录",
        )
        resolved_max_items = min(max_items, policy.max_items) if max_items else policy.max_items
        catalog: list[dict[str, object]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for policy_trigger in policy.catalog_triggers:
            for item in self.learned_store.phase_catalog(
                phase=phase,
                trigger_stage=policy_trigger,
                audience_scope=audience_scope,
                course_type_scope=course_type_scope,
                provider_scope=provider_scope,
                language_scope=language_scope,
                max_items=max(resolved_max_items * 2, resolved_max_items),
            ):
                key = (
                    str(item.get("trigger_stage", "")),
                    str(item.get("error_signature", "")),
                    str(item.get("layout_scope", "")),
                    str(item.get("visual_mode_scope", "")),
                    str(item.get("audience_scope", "")),
                    str(item.get("course_type_scope", "")),
                    str(item.get("provider_scope", "")),
                    str(item.get("language_scope", "")),
                )
                if key in seen:
                    continue
                seen.add(key)
                catalog.append(item)
                if len(catalog) >= resolved_max_items:
                    break
            if len(catalog) >= resolved_max_items:
                break
        if not catalog:
            return ""

        lines = [resolved_heading]
        for item in catalog:
            suffix_parts = []
            benchmark_id = str(item.get("benchmark_id", "")).strip()
            if benchmark_id:
                suffix_parts.append(f"benchmark={benchmark_id}")
            suffix_parts.extend(
                self._scope_parts(
                    str(item.get("layout_scope", "*") or "*"),
                    str(item.get("visual_mode_scope", "*") or "*"),
                    str(item.get("audience_scope", "*") or "*"),
                    str(item.get("course_type_scope", "*") or "*"),
                    str(item.get("provider_scope", "*") or "*"),
                    str(item.get("language_scope", "*") or "*"),
                )
            )
            condition_hint = self._condition_hint(
                [str(x) for x in item.get("conditions", []) if str(x).strip()]
            )
            if condition_hint:
                suffix_parts.append(condition_hint)
            suffix_text = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
            lines.append(
                f"- {item.get('error_signature', 'unknown')}: "
                f"{self._compact_text(str(item.get('repair_instruction', '')))}{suffix_text}"
            )
        return "\n".join(lines)

    def match_promoted_lessons(
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
        learned = self.learned_store.match_lessons(
            phase=phase,
            trigger_stage=trigger_stage,
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
            audience_scope=audience_scope,
            course_type_scope=course_type_scope,
            provider_scope=provider_scope,
            language_scope=language_scope,
            error_signature=error_signature,
            max_items=max_items,
        )
        if learned:
            return learned

        return self.promoted_store.match_lessons(
            phase=phase,
            trigger_stage=trigger_stage,
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
            audience_scope=audience_scope,
            course_type_scope=course_type_scope,
            provider_scope=provider_scope,
            language_scope=language_scope,
            error_signature=error_signature,
            max_items=max_items,
        )

    def match_runtime_memories(
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
    ) -> list[RepairMemoryRecord]:
        persisted = self.memory_store.match_records(
            phase,
            trigger_stage=trigger_stage,
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
            audience_scope=audience_scope,
            course_type_scope=course_type_scope,
            provider_scope=provider_scope,
            language_scope=language_scope,
            error_signature=error_signature,
            max_items=max_items,
        )
        ephemeral = []
        for item in self._ephemeral_memories.get(phase, []):
            if item.trigger_stage not in {trigger_stage, "*"}:
                continue
            if error_signature and item.error_signature not in {error_signature, "*"}:
                continue
            if layout_scope and item.layout_scope not in {"*", layout_scope}:
                continue
            if visual_mode_scope and item.visual_mode_scope not in {"*", visual_mode_scope}:
                continue
            if audience_scope and item.audience_scope not in {"*", audience_scope}:
                continue
            if course_type_scope and item.course_type_scope not in {"*", course_type_scope}:
                continue
            if provider_scope and item.provider_scope not in {"*", provider_scope}:
                continue
            if language_scope and item.language_scope not in {"*", language_scope}:
                continue
            ephemeral.append(item)

        combined: list[RepairMemoryRecord] = []
        seen: set[str] = set()
        for record in [*ephemeral, *persisted]:
            if record.memory_id in seen:
                continue
            seen.add(record.memory_id)
            combined.append(record)
        combined.sort(
            key=lambda item: (
                item.confidence,
                item.success_count - item.failure_count,
                item.last_used_at,
            ),
            reverse=True,
        )
        return combined[:max_items]

    def remember_runtime_success(
        self,
        *,
        phase: str,
        trigger_stage: str,
        error_signature: str,
        error_excerpt: str,
        repair_instruction: str,
        source_run_id: str,
        layout_scope: str = "*",
        visual_mode_scope: str = "*",
        audience_scope: str = "*",
        course_type_scope: str = "*",
        provider_scope: str = "*",
        language_scope: str = "*",
        conditions: list[str] | None = None,
        before_pattern: str = "",
        after_pattern: str = "",
    ) -> RepairMemoryRecord:
        record = self.memory_store.remember_success(
            phase=phase,
            trigger_stage=trigger_stage,
            error_signature=error_signature,
            error_excerpt=error_excerpt,
            repair_instruction=repair_instruction,
            source_run_id=source_run_id,
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
            audience_scope=audience_scope,
            course_type_scope=course_type_scope,
            provider_scope=provider_scope,
            language_scope=language_scope,
            conditions=conditions,
            before_pattern=before_pattern,
            after_pattern=after_pattern,
        )
        phase_records = self._ephemeral_memories.setdefault(phase, [])
        phase_records[:] = [item for item in phase_records if item.memory_id != record.memory_id]
        phase_records.insert(0, record)
        return record

    def remember_runtime_failure(self, *, phase: str, memory_id: str) -> RepairMemoryRecord | None:
        record = self.memory_store.remember_failure(phase=phase, memory_id=memory_id)
        if not record:
            return None
        phase_records = self._ephemeral_memories.setdefault(phase, [])
        phase_records[:] = [item for item in phase_records if item.memory_id != memory_id]
        phase_records.insert(0, record)
        return record

    def runtime_memory_path(self, phase: str) -> Path:
        return self.memory_store._phase_file(phase)

    def runtime_skill_path(self, phase: str) -> Path:
        return self.runtime_memory_path(phase)

    def build_prevention_bundle(
        self,
        *,
        context: SkillContext,
        heading: str = "",
        max_items: int | None = None,
    ) -> PromptBundle:
        learned_section = self._build_learned_bundle(
            context=context,
            heading=heading,
            max_items=max_items or self.PREVENTION_MAX_ITEMS,
        )
        promoted_lessons = self.match_promoted_lessons(
            phase=context.phase,
            trigger_stage=context.trigger_stage,
            layout_scope=context.layout_scope,
            visual_mode_scope=context.visual_mode_scope,
            audience_scope=context.audience,
            course_type_scope=context.course_type,
            provider_scope=context.provider,
            language_scope=context.language,
            max_items=max_items or self.PREVENTION_MAX_ITEMS,
        )
        runtime_memories = self.match_runtime_memories(
            phase=context.phase,
            trigger_stage=context.trigger_stage,
            layout_scope=context.layout_scope,
            visual_mode_scope=context.visual_mode_scope,
            audience_scope=context.audience,
            course_type_scope=context.course_type,
            provider_scope=context.provider,
            language_scope=context.language,
            max_items=max_items or self.PREVENTION_MAX_ITEMS,
        )
        sections = list(learned_section.sections)
        records = list(learned_section.loaded_records)
        if promoted_lessons:
            sections.append(
                PromptSection(
                    source_type="dynamic_memory",
                    identifier=f"{context.phase}:{context.trigger_stage}:promoted",
                    title="",
                    content=self._render_promoted_records(
                        promoted_lessons,
                        heading=self.shared_heading(
                            "promoted_lessons_heading.txt",
                            "## 项目级修复经验（已通过 benchmark gate）",
                        ),
                    ),
                    metadata={
                        "mode": "prevention",
                        "section_kind": "promoted_lessons",
                        "phase": context.phase,
                        "trigger_stage": context.trigger_stage,
                    },
                )
            )
            records.extend(self._promoted_records_to_trace(promoted_lessons))
        if runtime_memories:
            sections.append(
                PromptSection(
                    source_type="dynamic_memory",
                    identifier=f"{context.phase}:{context.trigger_stage}:runtime_prevention",
                    title="",
                    content=self._render_runtime_records(
                        runtime_memories,
                        heading=self.shared_heading(
                            "runtime_history_prevention_heading.txt",
                            "## 历史修复经验（预防）",
                        ),
                    ),
                    metadata={
                        "mode": "prevention",
                        "section_kind": "runtime_memory",
                        "phase": context.phase,
                        "trigger_stage": context.trigger_stage,
                    },
                )
            )
            records.extend(self._runtime_records_to_trace(runtime_memories))
        return PromptBundle(
            text="\n\n".join(section.render() for section in sections if not section.is_empty()),
            sections=tuple(section for section in sections if not section.is_empty()),
            loaded_records=tuple(records),
            runtime_memory_ids=tuple(item.memory_id for item in runtime_memories),
        )

    def build_repair_bundle(
        self,
        *,
        context: SkillContext,
        error_signature: str,
        max_items: int | None = None,
    ) -> PromptBundle:
        promoted_lessons = self.match_promoted_lessons(
            phase=context.phase,
            trigger_stage=context.trigger_stage,
            layout_scope=context.layout_scope,
            visual_mode_scope=context.visual_mode_scope,
            audience_scope=context.audience,
            course_type_scope=context.course_type,
            provider_scope=context.provider,
            language_scope=context.language,
            error_signature=error_signature,
            max_items=max_items or self.REPAIR_MAX_ITEMS,
        )
        runtime_memories = self.match_runtime_memories(
            phase=context.phase,
            trigger_stage=context.trigger_stage,
            layout_scope=context.layout_scope,
            visual_mode_scope=context.visual_mode_scope,
            audience_scope=context.audience,
            course_type_scope=context.course_type,
            provider_scope=context.provider,
            language_scope=context.language,
            error_signature=error_signature,
            max_items=max_items or self.REPAIR_MAX_ITEMS,
        )
        sections: list[PromptSection] = []
        records: list[LoadedSkillRecord] = []
        if promoted_lessons:
            sections.append(
                PromptSection(
                    source_type="dynamic_memory",
                    identifier=f"{context.phase}:{context.trigger_stage}:{error_signature}:promoted",
                    title="",
                    content=self._render_promoted_records(
                        promoted_lessons,
                        heading=self.shared_heading(
                            "promoted_lessons_heading.txt",
                            "## 项目级修复经验（已通过 benchmark gate）",
                        ),
                    ),
                    metadata={
                        "mode": "repair",
                        "section_kind": "promoted_lessons",
                        "phase": context.phase,
                        "trigger_stage": context.trigger_stage,
                        "error_signature": error_signature,
                    },
                )
            )
            records.extend(self._promoted_records_to_trace(promoted_lessons))
        if runtime_memories:
            sections.append(
                PromptSection(
                    source_type="dynamic_memory",
                    identifier=f"{context.phase}:{context.trigger_stage}:{error_signature}:runtime_repair",
                    title="",
                    content=self._render_runtime_records(
                        runtime_memories,
                        heading=self.shared_heading(
                            "runtime_history_repair_heading.txt",
                            "## 历史修复经验（强匹配）",
                        ),
                    ),
                    metadata={
                        "mode": "repair",
                        "section_kind": "runtime_memory",
                        "phase": context.phase,
                        "trigger_stage": context.trigger_stage,
                        "error_signature": error_signature,
                    },
                )
            )
            records.extend(self._runtime_records_to_trace(runtime_memories))
        return PromptBundle(
            text="\n\n".join(section.render() for section in sections if not section.is_empty()),
            sections=tuple(section for section in sections if not section.is_empty()),
            loaded_records=tuple(records),
            runtime_memory_ids=tuple(item.memory_id for item in runtime_memories),
        )

    def _build_learned_bundle(
        self,
        *,
        context: SkillContext,
        heading: str,
        max_items: int,
    ) -> PromptBundle:
        policy = self.policy_store.get(phase=context.phase, trigger_stage=context.trigger_stage)
        resolved_heading = heading or policy.heading or self.shared_heading(
            "learned_catalog_heading.txt",
            "## 长期技能目录",
        )
        resolved_max_items = min(max_items, policy.max_items) if max_items else policy.max_items
        catalog: list[dict[str, object]] = []
        seen: set[tuple[str, ...]] = set()
        for policy_trigger in policy.catalog_triggers:
            for item in self.learned_store.phase_catalog(
                phase=context.phase,
                trigger_stage=policy_trigger,
                audience_scope=context.audience,
                course_type_scope=context.course_type,
                provider_scope=context.provider,
                language_scope=context.language,
                max_items=max(resolved_max_items * 2, resolved_max_items),
            ):
                key = (
                    str(item.get("trigger_stage", "")),
                    str(item.get("error_signature", "")),
                    str(item.get("layout_scope", "")),
                    str(item.get("visual_mode_scope", "")),
                    str(item.get("audience_scope", "")),
                    str(item.get("course_type_scope", "")),
                    str(item.get("provider_scope", "")),
                    str(item.get("language_scope", "")),
                )
                if key in seen:
                    continue
                seen.add(key)
                catalog.append(item)
                if len(catalog) >= resolved_max_items:
                    break
            if len(catalog) >= resolved_max_items:
                break
        if not catalog:
            return PromptBundle()

        lines = [resolved_heading]
        trace_records: list[LoadedSkillRecord] = []
        for item in catalog:
            suffix_parts = []
            benchmark_id = str(item.get("benchmark_id", "")).strip()
            if benchmark_id:
                suffix_parts.append(f"benchmark={benchmark_id}")
            suffix_parts.extend(
                self._scope_parts(
                    str(item.get("layout_scope", "*") or "*"),
                    str(item.get("visual_mode_scope", "*") or "*"),
                    str(item.get("audience_scope", "*") or "*"),
                    str(item.get("course_type_scope", "*") or "*"),
                    str(item.get("provider_scope", "*") or "*"),
                    str(item.get("language_scope", "*") or "*"),
                )
            )
            condition_hint = self._condition_hint([str(x) for x in item.get("conditions", []) if str(x).strip()])
            if condition_hint:
                suffix_parts.append(condition_hint)
            suffix_text = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
            summary = self._compact_text(str(item.get("repair_instruction", "")))
            error_sig = str(item.get("error_signature", "unknown"))
            lines.append(f"- {error_sig}: {summary}{suffix_text}")
            trace_records.append(
                LoadedSkillRecord(
                    source="learned",
                    item_id=str(item.get("memory_id", "") or error_sig),
                    title=error_sig,
                    summary=summary,
                    trigger_stage=str(item.get("trigger_stage", "")),
                    error_signature=error_sig,
                    benchmark_id=benchmark_id,
                    memory_id=str(item.get("memory_id", "")).strip(),
                    scopes={
                        "layout_scope": str(item.get("layout_scope", "*") or "*"),
                        "visual_mode_scope": str(item.get("visual_mode_scope", "*") or "*"),
                        "audience_scope": str(item.get("audience_scope", "*") or "*"),
                        "course_type_scope": str(item.get("course_type_scope", "*") or "*"),
                        "provider_scope": str(item.get("provider_scope", "*") or "*"),
                        "language_scope": str(item.get("language_scope", "*") or "*"),
                    },
                    conditions=[str(x) for x in item.get("conditions", []) if str(x).strip()],
                )
            )
        return PromptBundle(
            text="\n".join(lines),
            sections=(
                PromptSection(
                    source_type="dynamic_memory",
                    identifier=f"{context.phase}:{context.trigger_stage}:learned_catalog",
                    title="",
                    content="\n".join(lines),
                    metadata={
                        "mode": "prevention",
                        "section_kind": "learned_catalog",
                        "phase": context.phase,
                        "trigger_stage": context.trigger_stage,
                    },
                ),
            ),
            loaded_records=tuple(trace_records),
        )

    def _render_runtime_records(
        self,
        records: list[RepairMemoryRecord],
        *,
        heading: str,
    ) -> str:
        if not records:
            return ""

        lines = [heading]
        for item in records:
            suffix_parts = self._scope_parts(
                item.layout_scope,
                item.visual_mode_scope,
                item.audience_scope,
                item.course_type_scope,
                item.provider_scope,
                item.language_scope,
            )
            condition_hint = self._condition_hint(item.conditions)
            if condition_hint:
                suffix_parts.append(condition_hint)
            suffix_text = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
            lines.append(f"- {self._compact_text(item.repair_instruction)}{suffix_text}")
        return "\n".join(lines)

    def _render_promoted_records(
        self,
        records: list[PromotedRepairLesson],
        *,
        heading: str,
    ) -> str:
        if not records:
            return ""
        lines = [heading]
        for item in records:
            suffix_parts = []
            if item.benchmark_id:
                suffix_parts.append(f"benchmark={item.benchmark_id}")
            suffix_parts.extend(
                self._scope_parts(
                    item.layout_scope,
                    item.visual_mode_scope,
                    item.audience_scope,
                    item.course_type_scope,
                    item.provider_scope,
                    item.language_scope,
                )
            )
            condition_hint = self._condition_hint(item.conditions)
            if condition_hint:
                suffix_parts.append(condition_hint)
            suffix_text = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
            lines.append(f"- {self._compact_text(item.repair_instruction)}{suffix_text}")
        return "\n".join(lines)

    def _runtime_records_to_trace(
        self,
        records: list[RepairMemoryRecord],
    ) -> list[LoadedSkillRecord]:
        payload: list[LoadedSkillRecord] = []
        for item in records:
            payload.append(
                LoadedSkillRecord(
                    source="runtime",
                    item_id=item.memory_id,
                    title=item.error_signature,
                    summary=self._compact_text(item.repair_instruction),
                    trigger_stage=item.trigger_stage,
                    error_signature=item.error_signature,
                    memory_id=item.memory_id,
                    scopes={
                        "layout_scope": item.layout_scope,
                        "visual_mode_scope": item.visual_mode_scope,
                        "audience_scope": item.audience_scope,
                        "course_type_scope": item.course_type_scope,
                        "provider_scope": item.provider_scope,
                        "language_scope": item.language_scope,
                    },
                    conditions=list(item.conditions),
                )
            )
        return payload

    def _promoted_records_to_trace(
        self,
        records: list[PromotedRepairLesson],
    ) -> list[LoadedSkillRecord]:
        payload: list[LoadedSkillRecord] = []
        for item in records:
            payload.append(
                LoadedSkillRecord(
                    source="promoted",
                    item_id=item.memory_id or item.error_signature,
                    title=item.error_signature,
                    summary=self._compact_text(item.repair_instruction),
                    trigger_stage=item.trigger_stage,
                    error_signature=item.error_signature,
                    benchmark_id=item.benchmark_id,
                    memory_id=item.memory_id,
                    scopes={
                        "layout_scope": item.layout_scope,
                        "visual_mode_scope": item.visual_mode_scope,
                        "audience_scope": item.audience_scope,
                        "course_type_scope": item.course_type_scope,
                        "provider_scope": item.provider_scope,
                        "language_scope": item.language_scope,
                    },
                    conditions=list(item.conditions),
                )
            )
        return payload

    def _default_limit(self, *, error_signature: str | None) -> int:
        return self.REPAIR_MAX_ITEMS if error_signature else self.PREVENTION_MAX_ITEMS

    @staticmethod
    def _scope_parts(
        layout_scope: str,
        visual_mode_scope: str,
        audience_scope: str = "*",
        course_type_scope: str = "*",
        provider_scope: str = "*",
        language_scope: str = "*",
    ) -> list[str]:
        parts: list[str] = []
        if layout_scope != "*":
            parts.append(f"layout={layout_scope}")
        if visual_mode_scope != "*":
            parts.append(f"visual_mode={visual_mode_scope}")
        if audience_scope != "*":
            parts.append(f"audience={audience_scope}")
        if course_type_scope != "*":
            parts.append(f"course_type={course_type_scope}")
        if provider_scope != "*":
            parts.append(f"provider={provider_scope}")
        if language_scope != "*":
            parts.append(f"language={language_scope}")
        return parts

    @classmethod
    def _condition_hint(cls, conditions: list[str]) -> str:
        if not conditions:
            return ""
        first = str(conditions[0]).strip()
        if not first:
            return ""
        return f"when={cls._compact_text(first, max_chars=cls.MAX_CONDITION_CHARS)}"

    @classmethod
    def _compact_text(cls, text: str, *, max_chars: int | None = None) -> str:
        limit = max_chars or cls.MAX_INSTRUCTION_CHARS
        cleaned = " ".join(str(text or "").split())
        for sep in ("，", ",", "；", ";", "。"):
            parts = [part.strip() for part in cleaned.split(sep) if part.strip()]
            for part in parts:
                if len(part) >= 16:
                    cleaned = part
                    break
            if cleaned != " ".join(str(text or "").split()):
                break
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: max(limit - 1, 1)].rstrip() + "…"
