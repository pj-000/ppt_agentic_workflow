from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.harness.memory.episodic_store import build_episode_memory_from_run_artifacts
from backend.harness.memory.jsonl_store import JsonlMemoryStore
from backend.harness.memory.models import MemoryHit, MemoryWriteResult
from backend.harness.memory.safety import sanitize_memory_text
from backend.harness.memory.store import AgentMemory


def create_default_agent_memory(
    *,
    output_root: str | Path,
    trace: Any | None = None,
) -> AgentMemory:
    return AgentMemory(JsonlMemoryStore(Path(output_root) / "memory"), trace=trace)


def write_episode_memory_for_run(
    *,
    run_id: str,
    run_dir: str | Path,
    memory: AgentMemory,
) -> MemoryWriteResult:
    record = build_episode_memory_from_run_artifacts(run_id=run_id, run_dir=run_dir)
    return memory.write(record)


def memory_hit_to_trace_payload(hit: MemoryHit) -> dict[str, Any]:
    record = hit.record
    return {
        "memory_id": record.memory_id,
        "namespace": record.namespace,
        "memory_type": record.memory_type.value,
        "key": sanitize_memory_text(record.key, limit=200),
        "score": hit.score,
        "reason": sanitize_memory_text(hit.reason, limit=200),
        "content_excerpt": sanitize_memory_text(record.content, limit=100),
        "tags": list(record.tags),
    }
