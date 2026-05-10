from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class HarnessTrace:
    run_id: str
    created_at: str = field(default_factory=_utc_now_iso)
    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(self, *, stage: str, payload: dict[str, Any]) -> None:
        self.entries.append(
            {
                "recorded_at": _utc_now_iso(),
                "stage": stage,
                **payload,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "created_at": self.created_at,
            "entry_count": len(self.entries),
            "entries": list(self.entries),
        }
