from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from backend.harness.runtime.trace import HarnessTrace


PhaseStatus = Literal["pending", "running", "completed", "failed", "skipped"]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PhaseExecution:
    phase: str
    status: PhaseStatus = "pending"
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "details": dict(self.details),
        }


class HarnessRunState:
    def __init__(self, phases: list[str], trace: HarnessTrace | None = None) -> None:
        self.trace = trace
        self._ordered = list(phases)
        self._phases = {phase: PhaseExecution(phase=phase) for phase in phases}

    def start(self, phase: str, *, details: dict[str, Any] | None = None) -> None:
        item = self._require_phase(phase)
        item.status = "running"
        item.started_at = item.started_at or _utc_now_iso()
        if details:
            item.details.update(details)
        self._record(item)

    def complete(self, phase: str, *, details: dict[str, Any] | None = None) -> None:
        item = self._require_phase(phase)
        item.status = "completed"
        item.started_at = item.started_at or _utc_now_iso()
        item.finished_at = _utc_now_iso()
        item.error = ""
        if details:
            item.details.update(details)
        self._record(item)

    def skip(self, phase: str, *, reason: str, details: dict[str, Any] | None = None) -> None:
        item = self._require_phase(phase)
        item.status = "skipped"
        item.started_at = item.started_at or _utc_now_iso()
        item.finished_at = _utc_now_iso()
        item.error = reason
        if details:
            item.details.update(details)
        self._record(item)

    def fail(self, phase: str, *, error: str, details: dict[str, Any] | None = None) -> None:
        item = self._require_phase(phase)
        item.status = "failed"
        item.started_at = item.started_at or _utc_now_iso()
        item.finished_at = _utc_now_iso()
        item.error = error
        if details:
            item.details.update(details)
        self._record(item)

    def export(self) -> dict[str, Any]:
        return {
            "phase_count": len(self._ordered),
            "phases": [self._phases[phase].to_dict() for phase in self._ordered],
        }

    def _require_phase(self, phase: str) -> PhaseExecution:
        if phase not in self._phases:
            raise KeyError(f"unknown phase: {phase}")
        return self._phases[phase]

    def _record(self, item: PhaseExecution) -> None:
        if not self.trace:
            return
        self.trace.record(
            stage="phase_state",
            payload={
                "phase": item.phase,
                "status": item.status,
                "started_at": item.started_at,
                "finished_at": item.finished_at,
                "error": item.error,
                "details": dict(item.details),
            },
        )
