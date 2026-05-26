from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from backend.harness.memory.jsonl_store import JsonlMemoryStore
from backend.harness.memory.models import MemoryHit, MemoryQuery, MemoryRecord, MemoryType, MemoryWriteResult
from backend.harness.memory.retriever import retrieve_memory_records
from backend.harness.memory.safety import clamp_confidence, sanitize_memory_mapping, sanitize_memory_text

logger = logging.getLogger(__name__)


class AgentMemory:
    def __init__(self, store: JsonlMemoryStore, trace: Any | None = None):
        self.store = store
        self.trace = trace

    def query(self, query: MemoryQuery) -> list[MemoryHit]:
        records = self.store.list_records(namespace=query.namespace, memory_type=query.memory_type)
        hits = retrieve_memory_records(records, query)
        safe_query_text = sanitize_memory_text(query.query, limit=200)
        safe_context = sanitize_memory_mapping(query.context)
        self._record(
            "memory.queried",
            {
                "namespace": query.namespace,
                "memory_type": query.memory_type.value if query.memory_type else "",
                "query": safe_query_text,
                "context": safe_context,
                "top_k": query.top_k,
                "hit_count": len(hits),
                "memory_ids": [hit.record.memory_id for hit in hits],
            },
        )
        if hits:
            self._record(
                "memory.hit",
                {
                    "namespace": query.namespace,
                    "memory_type": query.memory_type.value if query.memory_type else "",
                    "hit_count": len(hits),
                    "memory_ids": [hit.record.memory_id for hit in hits],
                    "score": hits[0].score,
                    "reason": sanitize_memory_text(hits[0].reason, limit=200),
                },
            )
        return hits

    def write(self, record: MemoryRecord) -> MemoryWriteResult:
        result = self.store.write(record)
        self._record(
            "memory.written",
            {
                "namespace": record.namespace,
                "memory_type": record.memory_type.value,
                "memory_id": result.memory_id,
                "created": result.created,
                "updated": result.updated,
                "skipped": result.skipped,
                "reason": result.reason,
            },
        )
        return result

    def get(self, memory_id: str) -> MemoryRecord | None:
        return self.store.get(memory_id)

    def list_records(
        self,
        *,
        namespace: str | None = None,
        memory_type: MemoryType | None = None,
    ) -> list[MemoryRecord]:
        return self.store.list_records(namespace=namespace, memory_type=memory_type)

    def update_outcome(
        self,
        *,
        memory_id: str,
        success: bool,
        metrics: dict[str, Any] | None = None,
    ) -> MemoryWriteResult:
        record = self.get(memory_id)
        if record is None:
            return MemoryWriteResult(memory_id=memory_id, created=False, skipped=True, reason="memory not found")
        outcome = dict(record.outcome)
        if metrics:
            outcome["metrics"] = metrics
        if success:
            success_count = record.success_count + 1
            failure_count = record.failure_count
            confidence = clamp_confidence(record.confidence + 0.05)
        else:
            success_count = record.success_count
            failure_count = record.failure_count + 1
            confidence = clamp_confidence(record.confidence - 0.08)
        updated = record.model_copy(
            update={
                "success_count": success_count,
                "failure_count": failure_count,
                "confidence": confidence,
                "outcome": outcome,
                "updated_at": utc_now_iso(),
            }
        )
        return self.write(updated)

    def _record(self, stage: str, payload: dict[str, Any]) -> None:
        if not self.trace:
            return
        record = getattr(self.trace, "record", None)
        if not callable(record):
            return
        try:
            record(stage=stage, payload=payload)
        except Exception as exc:
            logger.warning("[Memory] Trace recording failed; continuing: %s", exc)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
