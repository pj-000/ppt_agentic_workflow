from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from backend.harness.observability.event import TraceEvent
from backend.harness.observability.exporter import write_trace_summary_markdown
from backend.harness.observability.metrics import summarize_trace_events

logger = logging.getLogger(__name__)


class TraceStore:
    def __init__(self, output_root: str | Path):
        self.output_root = Path(output_root)

    def append(self, event: TraceEvent) -> None:
        try:
            path = self._trace_path(event.run_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            serialized = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(serialized + "\n")
        except Exception as exc:
            logger.warning("[Observability] Failed to append trace event; continuing: %s", exc)

    def load(self, run_id: str) -> list[TraceEvent]:
        path = self._trace_path(run_id)
        if not path.exists():
            return []

        events: list[TraceEvent] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    events.append(TraceEvent.model_validate_json(line))
                except Exception as exc:
                    logger.warning("[Observability] Skipping invalid trace line: %s", exc)
        except Exception as exc:
            logger.warning("[Observability] Failed to load trace events; returning empty list: %s", exc)
            return []
        return events

    def write_summary(self, run_id: str) -> dict[str, Any]:
        try:
            summary = summarize_trace_events(self.load(run_id))
            summary["run_id"] = run_id
            run_dir = self._run_dir(run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "trace_summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            write_trace_summary_markdown(summary, run_dir / "trace_summary.md")
            return summary
        except Exception as exc:
            logger.warning("[Observability] Failed to write trace summary; continuing: %s", exc)
            return {
                "run_id": run_id,
                "total_events": 0,
                "status": "unknown",
                "phase_count": 0,
                "tool_call_count": 0,
                "tool_attempt_count": 0,
                "failed_tool_count": 0,
                "skipped_tool_count": 0,
                "timeout_tool_count": 0,
                "error_signatures": [],
                "artifact_refs": {},
                "quality_report_paths": [],
            }

    def _run_dir(self, run_id: str) -> Path:
        return self.output_root / "runs" / _safe_run_id(run_id)

    def _trace_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "trace.jsonl"


def _safe_run_id(run_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in run_id)
    return safe or "run"
