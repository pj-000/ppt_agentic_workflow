from __future__ import annotations

import hashlib
from typing import Any

from backend.harness.memory.models import (
    MemoryHit,
    MemoryLifecycleState,
    MemoryPromotionState,
    MemoryRecord,
    MemoryType,
)
from backend.harness.memory.namespace import REPAIR_CONTENT, REPAIR_TOOL, REPAIR_VISUAL
from backend.harness.memory.safety import sanitize_memory_mapping, sanitize_memory_text
from backend.harness.memory.store import utc_now_iso


class ProceduralRepairMemoryAdapter:
    def __init__(self, runtime_memory_store: Any):
        self.runtime_memory_store = runtime_memory_store

    def query_repair_memories(
        self,
        *,
        phase: str,
        trigger_stage: str | None = None,
        error_signature: str | None = None,
        layout_scope: str | None = None,
        visual_mode_scope: str | None = None,
        audience_scope: str | None = None,
        course_type_scope: str | None = None,
        provider_scope: str | None = None,
        language_scope: str | None = None,
        top_k: int = 3,
    ) -> list[MemoryHit]:
        records = self.runtime_memory_store.match_records(
            phase,
            trigger_stage=trigger_stage,
            error_signature=error_signature,
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
            audience_scope=audience_scope,
            course_type_scope=course_type_scope,
            provider_scope=provider_scope,
            language_scope=language_scope,
            max_items=top_k,
        )
        hits: list[MemoryHit] = []
        for record in records:
            memory = self.to_memory_record(record)
            hits.append(MemoryHit(record=memory, score=float(memory.confidence), reason="runtime repair memory match"))
        return hits

    def remember_repair_success(self, **kwargs: Any) -> MemoryRecord:
        record = self.runtime_memory_store.remember_success(**kwargs)
        return self.to_memory_record(record)

    def to_memory_record(self, repair_record: Any) -> MemoryRecord:
        now = utc_now_iso()
        phase = str(_get(repair_record, "phase", ""))
        trigger_stage = str(_get(repair_record, "trigger_stage", ""))
        error_signature = str(_get(repair_record, "error_signature", "") or "unknown_error")
        namespace = _repair_namespace(phase, trigger_stage, error_signature)
        context = {
            "phase": phase,
            "trigger_stage": trigger_stage,
            "error_excerpt": _get(repair_record, "error_excerpt", ""),
            "layout_scope": _get(repair_record, "layout_scope", "*"),
            "visual_mode_scope": _get(repair_record, "visual_mode_scope", "*"),
            "audience_scope": _get(repair_record, "audience_scope", "*"),
            "course_type_scope": _get(repair_record, "course_type_scope", "*"),
            "provider_scope": _get(repair_record, "provider_scope", "*"),
            "language_scope": _get(repair_record, "language_scope", "*"),
            "conditions": list(_get(repair_record, "conditions", []) or []),
            "before_pattern": _get(repair_record, "before_pattern", ""),
            "after_pattern": _get(repair_record, "after_pattern", ""),
        }
        outcome = {
            "success_count": int(_get(repair_record, "success_count", 0) or 0),
            "failure_count": int(_get(repair_record, "failure_count", 0) or 0),
            "failure_streak": int(_get(repair_record, "failure_streak", 0) or 0),
            "confidence": float(_get(repair_record, "confidence", 0.5) or 0.5),
            "promotion_state": _get(repair_record, "promotion_state", "none"),
            "lifecycle_state": _get(repair_record, "lifecycle_state", "active"),
        }
        content = sanitize_memory_text(_get(repair_record, "repair_instruction", ""))
        return MemoryRecord(
            memory_id=str(_get(repair_record, "memory_id", "") or _stable_memory_id(namespace, error_signature, content)),
            namespace=namespace,
            memory_type=MemoryType.PROCEDURAL,
            key=error_signature,
            content=content,
            context=sanitize_memory_mapping(context),
            outcome=sanitize_memory_mapping(outcome),
            tags=["repair", "procedural", error_signature],
            confidence=_clamped_confidence(_get(repair_record, "confidence", 0.5)),
            success_count=int(_get(repair_record, "success_count", 0) or 0),
            failure_count=int(_get(repair_record, "failure_count", 0) or 0),
            source_run_id=str(_get(repair_record, "source_run_id", "")),
            created_at=str(_get(repair_record, "created_at", "") or now),
            updated_at=str(_get(repair_record, "last_used_at", "") or now),
            lifecycle_state=_lifecycle_state(_get(repair_record, "lifecycle_state", "active")),
            promotion_state=_promotion_state(_get(repair_record, "promotion_state", "none")),
        )


def _repair_namespace(phase: str, trigger_stage: str, error_signature: str) -> str:
    text = " ".join((phase, trigger_stage, error_signature)).lower()
    if any(marker in text for marker in ("tool", "js", "pptx", "node", "libreoffice", "search")):
        return REPAIR_TOOL
    if "content" in text or "coherence" in text:
        return REPAIR_CONTENT
    return REPAIR_VISUAL


def _get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _lifecycle_state(value: Any) -> MemoryLifecycleState:
    if hasattr(value, "value"):
        value = value.value
    try:
        return MemoryLifecycleState(str(value))
    except ValueError:
        return MemoryLifecycleState.ACTIVE


def _promotion_state(value: Any) -> MemoryPromotionState:
    if hasattr(value, "value"):
        value = value.value
    mapping = {
        "none": MemoryPromotionState.NONE,
        "pending_benchmark": MemoryPromotionState.PENDING_BENCHMARK,
        "candidate": MemoryPromotionState.CANDIDATE,
        "candidate_generated": MemoryPromotionState.CANDIDATE,
        "promoted": MemoryPromotionState.PROMOTED,
        "rejected": MemoryPromotionState.REJECTED,
    }
    return mapping.get(str(value), MemoryPromotionState.NONE)


def _clamped_confidence(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0.5
    return round(min(max(parsed, 0.0), 1.0), 4)


def _stable_memory_id(*parts: str) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"repair_{digest}"
