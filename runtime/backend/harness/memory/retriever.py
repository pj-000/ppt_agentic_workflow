from __future__ import annotations

import re
from typing import Any

from backend.harness.memory.models import MemoryHit, MemoryLifecycleState, MemoryQuery, MemoryRecord


def score_memory_record(
    record: MemoryRecord,
    *,
    query: str,
    context: dict[str, Any] | None = None,
) -> float:
    if record.lifecycle_state == MemoryLifecycleState.RETIRED:
        return 0.0
    query_tokens = _tokens(query)
    context_tokens = _tokens(" ".join(str(value) for value in (context or {}).values()))
    record_text = " ".join(
        [
            record.key,
            record.content,
            " ".join(record.tags),
            " ".join(str(value) for value in record.context.values()),
        ]
    )
    record_tokens = set(_tokens(record_text))
    if not query_tokens and not context_tokens:
        lexical = 0.1
    else:
        combined = query_tokens | context_tokens
        lexical = len(combined & record_tokens) / max(len(combined), 1)

    score = lexical
    if query and query.lower() in record.key.lower():
        score += 0.35
    if query and query.lower() in record.content.lower():
        score += 0.25
    score += record.confidence * 0.25
    score += min(record.success_count, 10) * 0.03
    score -= min(record.failure_count, 10) * 0.05
    if record.lifecycle_state == MemoryLifecycleState.STALE:
        score *= 0.75
    return round(max(score, 0.0), 4)


def retrieve_memory_records(
    records: list[MemoryRecord],
    query: MemoryQuery,
) -> list[MemoryHit]:
    hits: list[MemoryHit] = []
    for record in records:
        if record.lifecycle_state == MemoryLifecycleState.RETIRED:
            continue
        if query.memory_type is not None and record.memory_type != query.memory_type:
            continue
        score = score_memory_record(record, query=query.query, context=query.context)
        if score < query.min_score:
            continue
        hits.append(MemoryHit(record=record, score=score, reason=_reason(record, query, score)))
    hits.sort(key=lambda hit: (hit.score, hit.record.success_count, hit.record.updated_at), reverse=True)
    return hits[: max(query.top_k, 0)]


def _tokens(value: str) -> set[str]:
    return {item for item in re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", str(value).lower()) if item}


def _reason(record: MemoryRecord, query: MemoryQuery, score: float) -> str:
    parts = [f"score={score:.4f}", f"confidence={record.confidence:.2f}"]
    if query.query and query.query.lower() in record.key.lower():
        parts.append("key_match")
    if query.query and query.query.lower() in record.content.lower():
        parts.append("content_match")
    return "; ".join(parts)
