from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal

from pydantic import BaseModel, Field

import config
from backend.harness.runtime.benchmark_gate import BenchmarkGateStore, BenchmarkVerdict
from backend.harness.runtime.learned_skills import LearnedSkillStore
from backend.harness.runtime.promoted_lessons import PromotedLessonStore


PromotionState = Literal[
    "none",
    "pending_benchmark",
    "candidate_generated",
    "promoted",
    "rejected",
]

LifecycleState = Literal["active", "stale", "retired"]


class RepairMemoryRecord(BaseModel):
    memory_id: str
    phase: str
    trigger_stage: str
    error_signature: str
    error_excerpt: str = ""
    layout_scope: str = "*"
    visual_mode_scope: str = "*"
    audience_scope: str = "*"
    course_type_scope: str = "*"
    provider_scope: str = "*"
    language_scope: str = "*"
    conditions: list[str] = Field(default_factory=list)
    repair_instruction: str
    before_pattern: str = ""
    after_pattern: str = ""
    source_run_id: str = ""
    success_count: int = 1
    failure_count: int = 0
    failure_streak: int = 0
    confidence: float = 0.5
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_used_at: str
    last_success_at: str = ""
    promotion_state: PromotionState = "none"
    lifecycle_state: LifecycleState = "active"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_memory_id(
    phase: str,
    trigger_stage: str,
    error_signature: str,
    layout_scope: str,
    visual_mode_scope: str,
    audience_scope: str,
    course_type_scope: str,
    provider_scope: str,
    language_scope: str,
    repair_instruction: str,
) -> str:
    digest = hashlib.sha1(
        "|".join(
            (
                phase,
                trigger_stage,
                error_signature,
                layout_scope,
                visual_mode_scope,
                audience_scope,
                course_type_scope,
                provider_scope,
                language_scope,
                repair_instruction,
            )
        ).encode("utf-8")
    ).hexdigest()
    return digest[:12]


class RuntimeMemoryStore:
    STALE_AFTER_DAYS = 14
    RETIRE_AFTER_DAYS = 45
    RETIRE_FAILURE_STREAK = 4

    def __init__(
        self,
        root: Path | None = None,
        promotions_root: Path | None = None,
        learned_root: Path | None = None,
        benchmark_store: BenchmarkGateStore | None = None,
    ) -> None:
        if root is not None:
            resolved_root = root
        elif config.RUNTIME_MEMORY_DIR.exists() or not config.LEGACY_RUNTIME_SKILLS_DIR.exists():
            resolved_root = config.RUNTIME_MEMORY_DIR
        else:
            resolved_root = config.LEGACY_RUNTIME_SKILLS_DIR
        self.root = resolved_root
        self.promotions_root = promotions_root or (config.DOCS_DIR / "harness" / "promotions")
        self.benchmark_store = benchmark_store or BenchmarkGateStore()
        self.learned_root = learned_root or (
            self.promotions_root.parent / "learned_skills"
            if promotions_root is not None
            else (config.DOCS_DIR / "harness" / "learned_skills")
        )
        self._promoted_store = PromotedLessonStore(root=self.promotions_root)
        self._learned_store = LearnedSkillStore(
            root=self.learned_root,
            promoted_store=self._promoted_store,
        )

    def _phase_dir(self, phase: str) -> Path:
        path = self.root / phase
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _phase_file(self, phase: str) -> Path:
        return self._phase_dir(phase) / "repair_memory.jsonl"

    def load_phase_records(self, phase: str) -> list[RepairMemoryRecord]:
        path = self._phase_file(phase)
        if not path.exists():
            return []
        records: list[RepairMemoryRecord] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                records.append(RepairMemoryRecord.model_validate_json(line))
            except Exception:
                continue
        return records

    def save_phase_records(self, phase: str, records: Iterable[RepairMemoryRecord]) -> None:
        path = self._phase_file(phase)
        payload = "\n".join(record.model_dump_json() for record in records)
        path.write_text(payload + ("\n" if payload else ""), encoding="utf-8")

    def match_records(
        self,
        phase: str,
        *,
        trigger_stage: str | None = None,
        error_signature: str | None = None,
        layout_scope: str | None = None,
        visual_mode_scope: str | None = None,
        audience_scope: str | None = None,
        course_type_scope: str | None = None,
        provider_scope: str | None = None,
        language_scope: str | None = None,
        max_items: int = 3,
        min_confidence: float = 0.55,
    ) -> list[RepairMemoryRecord]:
        candidates = self.load_phase_records(phase)
        matched: list[RepairMemoryRecord] = []
        for record in candidates:
            governed = self._govern_record(record)
            if governed.lifecycle_state == "retired":
                continue
            if trigger_stage and record.trigger_stage not in {trigger_stage, "*"}:
                continue
            if error_signature and record.error_signature not in {error_signature, "*"}:
                continue
            if layout_scope and record.layout_scope not in {"*", layout_scope}:
                continue
            if visual_mode_scope and record.visual_mode_scope not in {"*", visual_mode_scope}:
                continue
            if audience_scope and record.audience_scope not in {"*", audience_scope}:
                continue
            if course_type_scope and record.course_type_scope not in {"*", course_type_scope}:
                continue
            if provider_scope and record.provider_scope not in {"*", provider_scope}:
                continue
            if language_scope and record.language_scope not in {"*", language_scope}:
                continue
            effective_confidence = self._effective_confidence(governed)
            if effective_confidence < min_confidence:
                continue
            matched.append(
                governed.model_copy(update={"confidence": round(effective_confidence, 2)})
            )
        matched.sort(
            key=lambda item: (
                item.confidence,
                item.success_count - item.failure_count,
                item.last_used_at,
            ),
            reverse=True,
        )
        return matched[:max_items]

    def remember_success(
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
        conditions = conditions or []
        memory_id = make_memory_id(
            phase,
            trigger_stage,
            error_signature,
            layout_scope,
            visual_mode_scope,
            audience_scope,
            course_type_scope,
            provider_scope,
            language_scope,
            repair_instruction,
        )
        records = self.load_phase_records(phase)
        now = utc_now_iso()

        for index, record in enumerate(records):
            if record.memory_id != memory_id:
                continue
            updated = record.model_copy(
                update={
                    "error_excerpt": error_excerpt or record.error_excerpt,
                    "conditions": conditions or record.conditions,
                    "before_pattern": before_pattern or record.before_pattern,
                    "after_pattern": after_pattern or record.after_pattern,
                    "source_run_id": source_run_id or record.source_run_id,
                    "success_count": record.success_count + 1,
                    "failure_streak": 0,
                    "last_used_at": now,
                    "last_success_at": now,
                    "confidence": self._estimate_confidence(
                        record.success_count + 1,
                        record.failure_count,
                    ),
                }
            )
            records[index] = self._sync_promotion_state(self._govern_record(updated))
            self.save_phase_records(phase, records)
            return records[index]

        created = RepairMemoryRecord(
            memory_id=memory_id,
            phase=phase,
            trigger_stage=trigger_stage,
            error_signature=error_signature,
            error_excerpt=error_excerpt[:500],
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
            audience_scope=audience_scope,
            course_type_scope=course_type_scope,
            provider_scope=provider_scope,
            language_scope=language_scope,
            conditions=conditions,
            repair_instruction=repair_instruction,
            before_pattern=before_pattern[:400],
            after_pattern=after_pattern[:400],
            source_run_id=source_run_id,
            success_count=1,
            failure_count=0,
            failure_streak=0,
            confidence=self._estimate_confidence(1, 0),
            created_at=now,
            last_used_at=now,
            last_success_at=now,
            promotion_state="none",
            lifecycle_state="active",
        )
        created = self._sync_promotion_state(self._govern_record(created))
        records.append(created)
        self.save_phase_records(phase, records)
        return created

    def remember_failure(
        self,
        *,
        phase: str,
        memory_id: str,
    ) -> RepairMemoryRecord | None:
        records = self.load_phase_records(phase)
        for index, record in enumerate(records):
            if record.memory_id != memory_id:
                continue
            updated = record.model_copy(
                update={
                    "failure_count": record.failure_count + 1,
                    "failure_streak": record.failure_streak + 1,
                    "last_used_at": utc_now_iso(),
                    "confidence": self._estimate_confidence(
                        record.success_count,
                        record.failure_count + 1,
                    ),
                }
            )
            governed = self._govern_record(updated)
            if updated.failure_streak >= self.RETIRE_FAILURE_STREAK:
                governed = governed.model_copy(update={"lifecycle_state": "retired"})
            records[index] = self._sync_promotion_state(governed)
            self.save_phase_records(phase, records)
            return records[index]
        return None

    def record_benchmark_result(
        self,
        *,
        phase: str,
        error_signature: str,
        benchmark_id: str,
        passed: bool,
        memory_id: str = "",
        regression_detected: bool = False,
        average_visual_delta: float = 0.0,
        notes: str = "",
        layout_scope: str = "*",
        visual_mode_scope: str = "*",
    ) -> BenchmarkVerdict:
        verdict = self.benchmark_store.record_verdict(
            phase=phase,
            error_signature=error_signature,
            benchmark_id=benchmark_id,
            passed=passed,
            memory_id=memory_id,
            regression_detected=regression_detected,
            average_visual_delta=average_visual_delta,
            notes=notes,
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
        )
        self.refresh_promotions(
            phase=phase,
            error_signature=error_signature,
            memory_id=memory_id or None,
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
        )
        return verdict

    def refresh_promotions(
        self,
        *,
        phase: str,
        error_signature: str | None = None,
        memory_id: str | None = None,
        layout_scope: str | None = None,
        visual_mode_scope: str | None = None,
    ) -> list[RepairMemoryRecord]:
        records = self.load_phase_records(phase)
        refreshed: list[RepairMemoryRecord] = []
        changed = False
        for record in records:
            if error_signature and record.error_signature != error_signature:
                refreshed.append(record)
                continue
            if memory_id and record.memory_id != memory_id:
                refreshed.append(record)
                continue
            if layout_scope and record.layout_scope not in {"*", layout_scope}:
                refreshed.append(record)
                continue
            if visual_mode_scope and record.visual_mode_scope not in {"*", visual_mode_scope}:
                refreshed.append(record)
                continue
            updated = self._sync_promotion_state(self._govern_record(record))
            refreshed.append(updated)
            changed = changed or (updated != record)
        if changed:
            self.save_phase_records(phase, refreshed)
        self._learned_store.sync_from_promotions()
        return refreshed

    def govern_phase_records(self, phase: str) -> list[RepairMemoryRecord]:
        records = self.load_phase_records(phase)
        governed = [self._sync_promotion_state(self._govern_record(record)) for record in records]
        if governed != records:
            self.save_phase_records(phase, governed)
        return governed

    @staticmethod
    def _estimate_confidence(success_count: int, failure_count: int) -> float:
        total = max(success_count + failure_count, 1)
        success_rate = success_count / total
        confidence = 0.45 + (success_rate * 0.4) + min(success_count, 6) * 0.03
        return round(min(confidence, 0.98), 2)

    @classmethod
    def _effective_confidence(cls, record: RepairMemoryRecord) -> float:
        stale_penalty = 0.12 if record.lifecycle_state == "stale" else 0.0
        failure_penalty = min(record.failure_streak, 3) * 0.07
        return max(0.0, record.confidence - stale_penalty - failure_penalty)

    @classmethod
    def _days_since(cls, raw: str) -> float:
        if not raw:
            return 0.0
        try:
            dt = datetime.fromisoformat(raw)
        except Exception:
            return 0.0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max((datetime.now(timezone.utc) - dt).total_seconds() / 86400.0, 0.0)

    @classmethod
    def _govern_record(cls, record: RepairMemoryRecord) -> RepairMemoryRecord:
        reference_time = record.last_success_at or record.last_used_at or record.created_at
        age_days = cls._days_since(reference_time)
        lifecycle_state: LifecycleState = "active"
        if record.failure_streak >= cls.RETIRE_FAILURE_STREAK or age_days >= cls.RETIRE_AFTER_DAYS:
            lifecycle_state = "retired"
        elif age_days >= cls.STALE_AFTER_DAYS:
            lifecycle_state = "stale"
        return record.model_copy(update={"lifecycle_state": lifecycle_state})

    def _candidate_path(self, record: RepairMemoryRecord) -> Path:
        return self.promotions_root / f"{record.phase}-{record.error_signature}-{record.memory_id}.md"

    def _sync_promotion_state(self, record: RepairMemoryRecord) -> RepairMemoryRecord:
        if record.promotion_state == "promoted":
            return record
        next_state = self._resolve_promotion_state(record)
        candidate_path = self._candidate_path(record)
        if next_state == "candidate_generated":
            self._write_candidate(record)
        elif candidate_path.exists():
            candidate_path.unlink()
        return record.model_copy(update={"promotion_state": next_state})

    def _resolve_promotion_state(self, record: RepairMemoryRecord) -> PromotionState:
        if record.lifecycle_state == "retired":
            return "rejected"
        total = max(record.success_count + record.failure_count, 1)
        success_rate = record.success_count / total
        if record.success_count < 3 or success_rate < 0.8:
            return "none"

        verdict = self._effective_verdict(record)
        if verdict is None:
            return "pending_benchmark"
        if not verdict.passed or verdict.regression_detected:
            return "rejected"
        return "candidate_generated"

    def _effective_verdict(self, record: RepairMemoryRecord) -> BenchmarkVerdict | None:
        exact_verdict = self.benchmark_store.latest_verdict(
            phase=record.phase,
            error_signature=record.error_signature,
            memory_id=record.memory_id,
            layout_scope=record.layout_scope,
            visual_mode_scope=record.visual_mode_scope,
        )
        if exact_verdict is not None:
            return exact_verdict

        bucket_verdict = self.benchmark_store.latest_verdict(
            phase=record.phase,
            error_signature=record.error_signature,
            layout_scope=record.layout_scope,
            visual_mode_scope=record.visual_mode_scope,
        )
        if bucket_verdict is None:
            return None

        sibling_count = sum(
            1
            for item in self.load_phase_records(record.phase)
            if item.error_signature == record.error_signature
            and item.layout_scope in {"*", record.layout_scope}
            and item.visual_mode_scope in {"*", record.visual_mode_scope}
            and item.audience_scope in {"*", record.audience_scope}
            and item.course_type_scope in {"*", record.course_type_scope}
            and item.provider_scope in {"*", record.provider_scope}
            and item.language_scope in {"*", record.language_scope}
        )
        if sibling_count <= 1:
            return bucket_verdict
        return None

    def _write_candidate(self, record: RepairMemoryRecord) -> None:
        self.promotions_root.mkdir(parents=True, exist_ok=True)
        path = self._candidate_path(record)
        verdict = self._effective_verdict(record)
        content = [
            f"# Promotion Candidate: {record.error_signature}",
            "",
            f"- phase: {record.phase}",
            f"- trigger_stage: {record.trigger_stage}",
            f"- error_signature: {record.error_signature}",
            f"- layout_scope: {record.layout_scope}",
            f"- visual_mode_scope: {record.visual_mode_scope}",
            f"- audience_scope: {record.audience_scope}",
            f"- course_type_scope: {record.course_type_scope}",
            f"- provider_scope: {record.provider_scope}",
            f"- language_scope: {record.language_scope}",
            f"- success_count: {record.success_count}",
            f"- failure_count: {record.failure_count}",
            f"- confidence: {record.confidence}",
            f"- source_run_id: {record.source_run_id}",
            f"- benchmark_id: {verdict.benchmark_id if verdict else '(missing)'}",
            f"- memory_id: {record.memory_id}",
            f"- benchmark_passed: {verdict.passed if verdict else False}",
            f"- regression_detected: {verdict.regression_detected if verdict else True}",
            f"- average_visual_delta: {verdict.average_visual_delta if verdict else 0.0}",
            "",
            "## Repair Instruction",
            record.repair_instruction,
            "",
            "## Conditions",
            *([f"- {item}" for item in record.conditions] or ["- (none)"]),
            "",
            "## Notes",
            f"- Benchmark gate passed via `{verdict.benchmark_id}`." if verdict else "- Benchmark gate information missing.",
        ]
        path.write_text("\n".join(content) + "\n", encoding="utf-8")
