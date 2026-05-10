from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

import config


class BenchmarkVerdict(BaseModel):
    gate_id: str
    phase: str
    error_signature: str
    memory_id: str = ""
    layout_scope: str = "*"
    visual_mode_scope: str = "*"
    benchmark_id: str
    passed: bool
    regression_detected: bool = False
    average_visual_delta: float = 0.0
    notes: str = ""
    recorded_at: str


def make_gate_id(
    phase: str,
    error_signature: str,
    memory_id: str,
    layout_scope: str,
    visual_mode_scope: str,
    benchmark_id: str,
) -> str:
    digest = hashlib.sha1(
        "|".join((phase, error_signature, memory_id, layout_scope, visual_mode_scope, benchmark_id)).encode("utf-8")
    ).hexdigest()
    return digest[:12]


class BenchmarkGateStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or config.BENCHMARKS_DIR

    def _gate_file(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root / "runtime_memory_gate.jsonl"

    def load_verdicts(self) -> list[BenchmarkVerdict]:
        path = self._gate_file()
        if not path.exists():
            return []
        verdicts: list[BenchmarkVerdict] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                verdicts.append(BenchmarkVerdict.model_validate_json(line))
            except Exception:
                continue
        return verdicts

    def save_verdicts(self, verdicts: list[BenchmarkVerdict]) -> None:
        path = self._gate_file()
        payload = "\n".join(item.model_dump_json() for item in verdicts)
        path.write_text(payload + ("\n" if payload else ""), encoding="utf-8")

    def record_verdict(
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
        gate_id = make_gate_id(
            phase=phase,
            error_signature=error_signature,
            memory_id=memory_id,
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
            benchmark_id=benchmark_id,
        )
        verdicts = self.load_verdicts()
        now = utc_now_iso()
        created = BenchmarkVerdict(
            gate_id=gate_id,
            phase=phase,
            error_signature=error_signature,
            memory_id=memory_id,
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
            benchmark_id=benchmark_id,
            passed=passed,
            regression_detected=regression_detected,
            average_visual_delta=average_visual_delta,
            notes=notes,
            recorded_at=now,
        )
        updated = False
        for index, item in enumerate(verdicts):
            if item.gate_id != gate_id:
                continue
            verdicts[index] = created
            updated = True
            break
        if not updated:
            verdicts.append(created)
        self.save_verdicts(verdicts)
        return created

    def latest_verdict(
        self,
        *,
        phase: str,
        error_signature: str,
        memory_id: str = "",
        layout_scope: str | None = None,
        visual_mode_scope: str | None = None,
    ) -> BenchmarkVerdict | None:
        matched: list[BenchmarkVerdict] = []
        for item in self.load_verdicts():
            if item.phase != phase or item.error_signature != error_signature:
                continue
            if memory_id and item.memory_id != memory_id:
                continue
            if layout_scope and item.layout_scope not in {"*", layout_scope}:
                continue
            if visual_mode_scope and item.visual_mode_scope not in {"*", visual_mode_scope}:
                continue
            matched.append(item)

        if not matched:
            return None

        matched.sort(
            key=lambda item: (
                item.layout_scope != "*",
                item.visual_mode_scope != "*",
                item.recorded_at,
            ),
            reverse=True,
        )
        return matched[0]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
