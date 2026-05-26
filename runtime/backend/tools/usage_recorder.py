from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any


def usage_log_path() -> Path | None:
    raw = (
        os.getenv("DIRECTIONAI_BOOK_TOKEN_USAGE_LOG")
        or os.getenv("DIRECTIONAI_TOKEN_USAGE_LOG")
        or ""
    ).strip()
    return Path(raw).expanduser() if raw else None


def record_usage(
    *,
    phase: str | None = None,
    component: str,
    operation: str,
    provider: str | None = None,
    model: str | None = None,
    usage: Any = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    estimated_input_tokens: int | None = None,
    estimated_output_tokens: int | None = None,
    image_count: int = 0,
    generated_image_count: int = 0,
    vl_image_count: int = 0,
    request_count: int = 1,
    is_estimate: bool | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    path = usage_log_path()
    if path is None:
        return
    parsed = _usage_to_tokens(usage)
    prompt_tokens = input_tokens if input_tokens is not None else parsed.get("input_tokens")
    completion_tokens = output_tokens if output_tokens is not None else parsed.get("output_tokens")
    total = total_tokens if total_tokens is not None else parsed.get("total_tokens")
    if total is None and prompt_tokens is not None and completion_tokens is not None:
        total = prompt_tokens + completion_tokens
    if is_estimate is None:
        is_estimate = total is None and (estimated_input_tokens is not None or estimated_output_tokens is not None)
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "run_id": os.getenv("DIRECTIONAI_BOOK_USAGE_RUN_ID", ""),
        "book_id": os.getenv("DIRECTIONAI_BOOK_ID", ""),
        "lesson_id": os.getenv("DIRECTIONAI_BOOK_LESSON_ID", ""),
        "phase": phase or os.getenv("DIRECTIONAI_BOOK_PHASE", "phase3"),
        "component": component,
        "operation": operation,
        "provider": provider or "",
        "model": model or "",
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "total_tokens": total,
        "estimated_input_tokens": estimated_input_tokens,
        "estimated_output_tokens": estimated_output_tokens,
        "estimated_total_tokens": (
            (estimated_input_tokens or 0) + (estimated_output_tokens or 0)
            if estimated_input_tokens is not None or estimated_output_tokens is not None
            else None
        ),
        "is_estimate": bool(is_estimate),
        "image_count": image_count,
        "generated_image_count": generated_image_count,
        "vl_image_count": vl_image_count,
        "request_count": request_count,
        "metadata": metadata or {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def estimate_tokens_from_text(text: str) -> int:
    value = str(text or "")
    if not value:
        return 0
    ascii_count = sum(1 for ch in value if ord(ch) < 128)
    non_ascii_count = len(value) - ascii_count
    return max(1, math.ceil(ascii_count / 4 + non_ascii_count / 1.8))


def estimate_tokens_from_messages(messages: list[dict[str, Any]]) -> int:
    return estimate_tokens_from_text(json.dumps(messages, ensure_ascii=False, default=str))


def _usage_to_tokens(usage: Any) -> dict[str, int | None]:
    if usage is None:
        return {}
    if isinstance(usage, dict):
        getter = usage.get
    else:
        getter = lambda key, default=None: getattr(usage, key, default)
    input_tokens = (
        getter("prompt_tokens")
        or getter("input_tokens")
        or getter("prompt_token_count")
        or getter("input_token_count")
    )
    output_tokens = (
        getter("completion_tokens")
        or getter("output_tokens")
        or getter("completion_token_count")
        or getter("output_token_count")
    )
    total_tokens = getter("total_tokens") or getter("total_token_count")
    return {
        "input_tokens": _coerce_int(input_tokens),
        "output_tokens": _coerce_int(output_tokens),
        "total_tokens": _coerce_int(total_tokens),
    }


def _coerce_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
