from __future__ import annotations

import hashlib
from typing import Any

from backend.harness.memory.models import MemoryRecord, MemoryType
from backend.harness.memory.namespace import validate_namespace
from backend.harness.memory.safety import clamp_confidence, sanitize_memory_mapping, sanitize_memory_list, sanitize_memory_text
from backend.harness.memory.store import utc_now_iso


def create_semantic_memory(
    *,
    namespace: str,
    key: str,
    content: str,
    tags: list[str] | None = None,
    context: dict[str, Any] | None = None,
    source_run_id: str = "",
    confidence: float = 0.5,
) -> MemoryRecord:
    safe_namespace = validate_namespace(namespace)
    safe_content = sanitize_memory_text(content)
    now = utc_now_iso()
    return MemoryRecord(
        memory_id=_stable_memory_id("semantic", safe_namespace, key, safe_content[:200]),
        namespace=safe_namespace,
        memory_type=MemoryType.SEMANTIC,
        key=sanitize_memory_text(key, limit=200),
        content=safe_content,
        context=sanitize_memory_mapping(context or {}),
        tags=sanitize_memory_list(tags or []),
        confidence=clamp_confidence(confidence),
        source_run_id=source_run_id,
        created_at=now,
        updated_at=now,
    )


def _stable_memory_id(*parts: str) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{parts[0]}_{digest}"
