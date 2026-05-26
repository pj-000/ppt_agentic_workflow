from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from backend.harness.observability import (  # noqa: E402
    ObservabilityTraceAdapter,
    TraceEvent,
    TraceStore,
    new_event_id,
    redact_trace_payload,
    summarize_trace_events,
    utc_now_iso,
)
from backend.harness.quality import write_quality_report_safely  # noqa: E402
from backend.harness.tooling import ToolCall, ToolExecutor, ToolRegistry, ToolSpec  # noqa: E402


def _event(
    event_type: str,
    *,
    run_id: str = "run_obs",
    phase: str | None = None,
    tool_name: str | None = None,
    status: str | None = None,
    error_signature: str | None = None,
    artifact_refs: dict[str, str] | None = None,
    payload: dict | None = None,
    metrics: dict | None = None,
) -> TraceEvent:
    return TraceEvent(
        run_id=run_id,
        event_id=new_event_id(),
        event_type=event_type,  # type: ignore[arg-type]
        timestamp=utc_now_iso(),
        phase=phase,
        tool_name=tool_name,
        status=status,
        error_signature=error_signature,
        artifact_refs=artifact_refs or {},
        payload=payload or {},
        metrics=metrics or {},
    )


def test_trace_event_serializes_payload_metrics_and_artifacts() -> None:
    event = _event(
        "tool.finished",
        tool_name="ppt.render_preview",
        status="failed",
        payload={"call_id": "call_1"},
        metrics={"latency_ms": 12},
        artifact_refs={"preview_dir": "/tmp/preview"},
    )

    loaded = TraceEvent.model_validate_json(event.model_dump_json())

    assert loaded.payload["call_id"] == "call_1"
    assert loaded.metrics["latency_ms"] == 12
    assert loaded.artifact_refs["preview_dir"] == "/tmp/preview"


def test_trace_store_appends_loads_and_writes_summary(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    store.append(_event("run.started", run_id="run_store", phase="init"))
    store.append(_event("phase.finished", run_id="run_store", phase="outline", status="success"))
    store.append(
        _event(
            "tool.finished",
            run_id="run_store",
            tool_name="ppt.render_preview",
            status="failed",
            error_signature="ppt.render_preview:PreviewGenerationFailed:no_images",
            artifact_refs={"preview_dir": "preview"},
        )
    )
    store.append(_event("run.finished", run_id="run_store", status="success"))

    loaded = store.load("run_store")
    summary = store.write_summary("run_store")

    assert len(loaded) == 4
    assert (tmp_path / "runs" / "run_store" / "trace.jsonl").exists()
    assert (tmp_path / "runs" / "run_store" / "trace_summary.json").exists()
    assert (tmp_path / "runs" / "run_store" / "trace_summary.md").exists()
    assert summary["failed_tool_count"] == 1
    assert summary["status"] == "warning"


def test_trace_store_load_returns_empty_when_missing(tmp_path: Path) -> None:
    assert TraceStore(tmp_path).load("missing_run") == []


def test_summarize_trace_events_counts_tools_errors_artifacts_and_quality_paths() -> None:
    events = [
        _event("run.started", phase="init"),
        _event("phase.started", phase="outline"),
        _event("phase.finished", phase="outline", status="success"),
        _event("tool.finished", tool_name="search.web_text", status="skipped"),
        _event(
            "tool.finished",
            tool_name="ppt.render_preview",
            status="timeout",
            error_signature="ppt.render_preview:TimeoutError:libreoffice_timeout",
        ),
        _event(
            "quality.reported",
            payload={"json_path": "quality_report.json", "markdown_path": "quality_report.md"},
            artifact_refs={"quality_report_json": "quality_report.json", "pptx_path": "deck.pptx"},
        ),
    ]

    summary = summarize_trace_events(events)

    assert summary["total_events"] == 6
    assert summary["phase_count"] == 2
    assert summary["tool_call_count"] == 2
    assert summary["failed_tool_count"] == 0
    assert summary["skipped_tool_count"] == 1
    assert summary["timeout_tool_count"] == 1
    assert summary["error_signatures"] == ["ppt.render_preview:TimeoutError:libreoffice_timeout"]
    assert summary["artifact_refs"]["pptx_path"] == "deck.pptx"
    assert "quality_report.json" in summary["quality_report_paths"]


def test_redact_trace_payload_filters_sensitive_keys_values_and_long_strings() -> None:
    redacted = redact_trace_payload(
        {
            "api_key": "sk-abc123456789",
            "token": "token-value",
            "secret": "secret-value",
            "password": "password-value",
            "authorization": "Bearer sk-live-secret-token",
            "system_prompt": "private",
            "hidden_reasoning": "private",
            "chain_of_thought": "private",
            "raw_model_response": "private",
            "nested": {"path": "/Users/alice/project/private.txt"},
            "long_text": "x" * 600,
            "message": "provider key sk-testsecret123456789 leaked",
        }
    )

    for key in (
        "api_key",
        "token",
        "secret",
        "password",
        "authorization",
        "system_prompt",
        "hidden_reasoning",
        "chain_of_thought",
        "raw_model_response",
    ):
        assert redacted[key] == "[REDACTED]"
    assert "/Users/alice/project" not in redacted["nested"]["path"]
    assert redacted["long_text"].endswith("[TRUNCATED]")
    assert "sk-testsecret123456789" not in redacted["message"]


def test_observability_adapter_records_standard_event_and_legacy_trace(tmp_path: Path) -> None:
    class LegacyTrace:
        def __init__(self) -> None:
            self.entries: list[tuple[str, dict]] = []

        def record(self, *, stage: str, payload: dict) -> None:
            self.entries.append((stage, payload))

    legacy = LegacyTrace()
    store = TraceStore(tmp_path)
    adapter = ObservabilityTraceAdapter("run_adapter", store, legacy_trace=legacy)

    adapter.record(
        "tool.finished",
        {
            "tool_name": "ppt.render_preview",
            "status": "failed",
            "latency_ms": 20,
            "error_signature": "ppt.render_preview:PreviewGenerationFailed:no_images",
            "api_key": "sk-secret123456789",
        },
    )

    events = store.load("run_adapter")
    assert len(events) == 1
    assert events[0].event_type == "tool.finished"
    assert events[0].tool_name == "ppt.render_preview"
    assert events[0].status == "failed"
    assert events[0].metrics["latency_ms"] == 20
    assert events[0].error_signature == "ppt.render_preview:PreviewGenerationFailed:no_images"
    assert events[0].payload["api_key"] == "[REDACTED]"
    assert legacy.entries[0][0] == "tool.finished"


def test_observability_adapter_does_not_throw_when_legacy_or_store_fails(tmp_path: Path) -> None:
    class FailingLegacyTrace:
        def record(self, *, stage: str, payload: dict) -> None:
            raise RuntimeError("legacy failed")

    class FailingStore(TraceStore):
        def append(self, event: TraceEvent) -> None:
            raise RuntimeError("store failed")

    adapter = ObservabilityTraceAdapter("run_failures", FailingStore(tmp_path), legacy_trace=FailingLegacyTrace())

    adapter.record("phase.finished", {"phase": "outline", "status": "success"})


def test_tool_executor_with_observability_adapter_writes_tool_events(tmp_path: Path) -> None:
    def fail(_call: ToolCall) -> dict:
        raise ValueError("provider unavailable")

    registry = ToolRegistry()
    registry.register(ToolSpec(name="fake.fail", description="Fake failure tool"), fail)
    adapter = ObservabilityTraceAdapter("run_tools", TraceStore(tmp_path))

    result = asyncio.run(
        ToolExecutor(registry, trace=adapter).execute(
            ToolCall(run_id="run_tools", call_id="call_1", tool_name="fake.fail", input={})
        )
    )
    events = TraceStore(tmp_path).load("run_tools")

    assert result.status == "failed"
    assert [event.event_type for event in events] == ["tool.started", "tool.finished"]
    finished = events[-1]
    assert finished.tool_name == "fake.fail"
    assert finished.status == "failed"
    assert "latency_ms" in finished.metrics
    assert finished.error_signature == "fake.fail:ValueError:provider_unavailable"


def test_tool_executor_does_not_throw_when_trace_record_fails() -> None:
    class FailingTrace:
        def record(self, *, stage: str, payload: dict) -> None:
            raise RuntimeError("trace unavailable")

    registry = ToolRegistry()
    registry.register(ToolSpec(name="fake.success", description="Fake success tool"), lambda call: {"ok": True})

    result = asyncio.run(
        ToolExecutor(registry, trace=FailingTrace()).execute(
            ToolCall(run_id="run_trace_fail", call_id="call_1", tool_name="fake.success", input={})
        )
    )

    assert result.status == "success"
    assert result.output == {"ok": True}


def test_quality_report_safely_records_quality_report_event(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    adapter = ObservabilityTraceAdapter("run_quality", store)

    paths = write_quality_report_safely(
        output_root=tmp_path,
        run_id="run_quality",
        topic="trace quality",
        pptx_path=None,
        preview_images=None,
        extracted_text=None,
        visual_eval_results=None,
        content_issues=None,
        repair_events=None,
        trace=adapter,
    )

    events = store.load("run_quality")
    quality_events = [event for event in events if event.event_type == "quality.reported"]

    assert Path(paths["json_path"]).exists()
    assert quality_events
    assert quality_events[0].payload["json_path"].endswith("runs/run_quality/quality_report.json")
    assert quality_events[0].artifact_refs["quality_report_json"].endswith("runs/run_quality/quality_report.json")
