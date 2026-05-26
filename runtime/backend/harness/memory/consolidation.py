from __future__ import annotations

from pydantic import BaseModel

from backend.harness.memory.models import MemoryLifecycleState, MemoryPromotionState, MemoryRecord


class MemoryPromotionPolicy(BaseModel):
    min_success_count: int = 3
    max_failure_count: int = 1
    min_confidence: float = 0.75
    require_benchmark_pass: bool = False


def should_promote_memory(
    record: MemoryRecord,
    policy: MemoryPromotionPolicy,
    *,
    benchmark_passed: bool | None = None,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if record.lifecycle_state == MemoryLifecycleState.RETIRED:
        reasons.append("memory is retired")
    if record.promotion_state == MemoryPromotionState.REJECTED:
        reasons.append("memory promotion was rejected")
    if record.success_count < policy.min_success_count:
        reasons.append(f"success_count below {policy.min_success_count}")
    if record.failure_count > policy.max_failure_count:
        reasons.append(f"failure_count above {policy.max_failure_count}")
    if record.confidence < policy.min_confidence:
        reasons.append(f"confidence below {policy.min_confidence}")
    if policy.require_benchmark_pass and benchmark_passed is not True:
        reasons.append("benchmark pass required")
    if reasons:
        return False, reasons
    return True, ["eligible for promotion"]
