from __future__ import annotations

import inspect
import os
from typing import Any, Awaitable, Callable

from backend.tools.usage_recorder import (
    estimate_tokens_from_messages,
    estimate_tokens_from_text,
    record_usage,
)


def model_supports_reasoning(model: str | None) -> bool:
    name = (model or "").strip().lower()
    return name.startswith("minimax")


def build_chat_completion_kwargs(model: str | None) -> dict:
    name = (model or "").strip().lower()
    if not model_supports_reasoning(model):
        return {}
    if "/" in name:
        return {
            "extra_body": {
                "reasoning": {
                    "enabled": True,
                    "exclude": False,
                }
            }
        }
    return {
        "extra_body": {
            "reasoning_split": True,
        }
    }


def stream_chat_completion_text(
    client: Any,
    *,
    model: str | None,
    messages: list[dict[str, Any]],
    on_reasoning_chunk: Callable[[str], None] | None = None,
    **kwargs: Any,
) -> tuple[str, str]:
    usage_requested = False
    stream_kwargs = dict(kwargs)
    if _should_request_stream_usage() and "stream_options" not in stream_kwargs:
        stream_kwargs["stream_options"] = {"include_usage": True}
        usage_requested = True
    try:
        stream = client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            **stream_kwargs,
        )
    except Exception:
        if not usage_requested:
            raise
        stream_kwargs.pop("stream_options", None)
        usage_requested = False
        stream = client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            **stream_kwargs,
        )

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    content_buffer = ""
    reasoning_buffer = ""
    usage = None

    for chunk in stream:
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage is not None:
            usage = chunk_usage
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        delta = getattr(choices[0], "delta", None)
        if delta is None:
            continue

        reasoning_chunk = extract_reasoning_delta_text(delta)
        if reasoning_chunk:
            if _uses_cumulative_stream_deltas(model):
                new_reasoning = _subtract_prefix(reasoning_chunk, reasoning_buffer)
                reasoning_buffer = reasoning_chunk
                reasoning_chunk = new_reasoning
            reasoning_parts.append(reasoning_chunk)
            if on_reasoning_chunk:
                on_reasoning_chunk(reasoning_chunk)

        content_chunk = extract_content_delta_text(delta)
        if content_chunk:
            if _uses_cumulative_stream_deltas(model):
                new_content = _subtract_prefix(content_chunk, content_buffer)
                content_buffer = content_chunk
                content_chunk = new_content
            content_parts.append(content_chunk)

    content_text = "".join(content_parts)
    reasoning_text = "".join(reasoning_parts).strip()
    record_usage(
        component="ppt_runtime_llm",
        operation=_caller_operation(),
        model=model,
        usage=usage,
        estimated_input_tokens=estimate_tokens_from_messages(messages),
        estimated_output_tokens=estimate_tokens_from_text(content_text + "\n" + reasoning_text),
        metadata={"stream": True, "usage_requested": usage_requested},
    )
    return content_text, reasoning_text


async def async_stream_chat_completion_text(
    client: Any,
    *,
    model: str | None,
    messages: list[dict[str, Any]],
    on_reasoning_chunk: Callable[[str], Awaitable[None] | None] | Callable[[str], None] | None = None,
    **kwargs: Any,
) -> tuple[str, str]:
    usage_requested = False
    stream_kwargs = dict(kwargs)
    if _should_request_stream_usage() and "stream_options" not in stream_kwargs:
        stream_kwargs["stream_options"] = {"include_usage": True}
        usage_requested = True
    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            **stream_kwargs,
        )
    except Exception:
        if not usage_requested:
            raise
        stream_kwargs.pop("stream_options", None)
        usage_requested = False
        stream = await client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            **stream_kwargs,
        )

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    content_buffer = ""
    reasoning_buffer = ""
    usage = None

    async for chunk in stream:
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage is not None:
            usage = chunk_usage
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        delta = getattr(choices[0], "delta", None)
        if delta is None:
            continue

        reasoning_chunk = extract_reasoning_delta_text(delta)
        if reasoning_chunk:
            if _uses_cumulative_stream_deltas(model):
                new_reasoning = _subtract_prefix(reasoning_chunk, reasoning_buffer)
                reasoning_buffer = reasoning_chunk
                reasoning_chunk = new_reasoning
            reasoning_parts.append(reasoning_chunk)
            if on_reasoning_chunk:
                result = on_reasoning_chunk(reasoning_chunk)
                if hasattr(result, "__await__"):
                    await result

        content_chunk = extract_content_delta_text(delta)
        if content_chunk:
            if _uses_cumulative_stream_deltas(model):
                new_content = _subtract_prefix(content_chunk, content_buffer)
                content_buffer = content_chunk
                content_chunk = new_content
            content_parts.append(content_chunk)

    content_text = "".join(content_parts)
    reasoning_text = "".join(reasoning_parts).strip()
    record_usage(
        component="ppt_runtime_llm",
        operation=_caller_operation(),
        model=model,
        usage=usage,
        estimated_input_tokens=estimate_tokens_from_messages(messages),
        estimated_output_tokens=estimate_tokens_from_text(content_text + "\n" + reasoning_text),
        metadata={"stream": True, "usage_requested": usage_requested},
    )
    return content_text, reasoning_text


def _should_request_stream_usage() -> bool:
    return bool(os.getenv("DIRECTIONAI_BOOK_TOKEN_USAGE_LOG") or os.getenv("DIRECTIONAI_TOKEN_USAGE_LOG")) and (
        os.getenv("DIRECTIONAI_TOKEN_USAGE_STREAM_OPTIONS", "1").strip().lower()
        in {"1", "true", "yes", "on"}
    )


def _caller_operation() -> str:
    try:
        frame = inspect.stack()[2]
        return f"{frame.function}"
    except Exception:
        return "stream_chat_completion"


def extract_reasoning_text(response: object) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""

    message = getattr(choices[0], "message", None)
    if message is None:
        return ""

    parts: list[str] = []

    reasoning = getattr(message, "reasoning", None)
    if isinstance(reasoning, str) and reasoning.strip():
        parts.append(reasoning.strip())
    elif reasoning is not None:
        summary = _extract_text_from_obj(reasoning)
        if summary:
            parts.append(summary)

    reasoning_details = getattr(message, "reasoning_details", None)
    if isinstance(reasoning_details, list):
        for item in reasoning_details:
            text = _extract_reasoning_detail(item)
            if text:
                parts.append(text)

    deduped: list[str] = []
    seen: set[str] = set()
    for part in parts:
        cleaned = part.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            deduped.append(cleaned)

    return "\n".join(deduped)


def extract_reasoning_delta_text(delta: object) -> str:
    parts: list[str] = []

    for key in ("reasoning_content", "reasoning"):
        value = _read_value(delta, key)
        text = _extract_text_from_obj(value)
        if text:
            parts.append(text)

    reasoning_details = _read_value(delta, "reasoning_details")
    if isinstance(reasoning_details, list):
        for item in reasoning_details:
            text = _extract_reasoning_detail(item)
            if text:
                parts.append(text)

    deduped: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            deduped.append(part)
    return "".join(deduped)


def extract_content_delta_text(delta: object) -> str:
    content = _read_value(delta, "content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            text = _extract_text_from_obj(item)
            if text:
                parts.append(text)
        return "".join(parts)
    return _extract_text_from_obj(content)


def _extract_reasoning_detail(item: object) -> str:
    item_type = _read_value(item, "type")
    if item_type == "reasoning.summary":
        summary = _read_value(item, "summary")
        if isinstance(summary, list):
            return "\n".join(str(entry).strip() for entry in summary if str(entry).strip())
        if isinstance(summary, str):
            return summary.strip()

    text = _read_value(item, "text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    summary = _read_value(item, "summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()

    return _extract_text_from_obj(item)


def _extract_text_from_obj(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, dict):
        for key in ("text", "summary", "content"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
            if isinstance(nested, list):
                return "\n".join(str(item).strip() for item in nested if str(item).strip())
    return ""


def _uses_cumulative_stream_deltas(model: str | None) -> bool:
    name = (model or "").strip().lower()
    return name.startswith("minimax") and "/" not in name


def _subtract_prefix(current: str, previous: str) -> str:
    if previous and current.startswith(previous):
        return current[len(previous):]
    return current


def _read_value(obj: object, key: str):
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)
