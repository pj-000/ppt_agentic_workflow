from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from backend.harness.memory import (  # noqa: E402
    AgentMemory,
    JsonlMemoryStore,
    MemoryHit,
    MemoryLifecycleState,
    MemoryPromotionPolicy,
    MemoryPromotionState,
    MemoryQuery,
    MemoryRecord,
    MemoryType,
    ProceduralRepairMemoryAdapter,
    build_episode_memory_from_run_artifacts,
    create_default_agent_memory,
    create_semantic_memory,
    memory_hit_to_trace_payload,
    should_promote_memory,
    write_episode_memory_for_run,
)
from backend.harness.memory.namespace import REPAIR_VISUAL, SEMANTIC_PPT_DESIGN, validate_namespace  # noqa: E402
from backend.harness.memory.retriever import retrieve_memory_records, score_memory_record  # noqa: E402
from backend.harness.observability import ObservabilityTraceAdapter, TraceStore  # noqa: E402


def _record(
    memory_id: str = "mem_1",
    *,
    namespace: str = REPAIR_VISUAL,
    memory_type: MemoryType = MemoryType.PROCEDURAL,
    key: str = "ppt.render_preview:DependencyMissing:soffice_not_found",
    content: str = "Install preview dependency or skip preview repair safely.",
    tags: list[str] | None = None,
    confidence: float = 0.7,
    success_count: int = 1,
    failure_count: int = 0,
    lifecycle_state: MemoryLifecycleState = MemoryLifecycleState.ACTIVE,
    promotion_state: MemoryPromotionState = MemoryPromotionState.NONE,
    context: dict[str, Any] | None = None,
    outcome: dict[str, Any] | None = None,
) -> MemoryRecord:
    return MemoryRecord(
        memory_id=memory_id,
        namespace=namespace,
        memory_type=memory_type,
        key=key,
        content=content,
        context=context or {"trigger_stage": "visual_qa"},
        outcome=outcome or {"status": "success"},
        tags=tags or ["repair", "visual", "preview"],
        confidence=confidence,
        success_count=success_count,
        failure_count=failure_count,
        source_run_id="run_1",
        created_at="2026-05-26T00:00:00+00:00",
        updated_at="2026-05-26T00:00:00+00:00",
        lifecycle_state=lifecycle_state,
        promotion_state=promotion_state,
    )


def _write_run_artifacts(run_dir: Path, *, quality: dict[str, Any] | None = None, trace: dict[str, Any] | None = None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    if quality is not None:
        (run_dir / "quality_report.json").write_text(json.dumps(quality, ensure_ascii=False), encoding="utf-8")
    if trace is not None:
        (run_dir / "trace_summary.json").write_text(json.dumps(trace, ensure_ascii=False), encoding="utf-8")


def _quality() -> dict[str, Any]:
    return {
        "run": {
            "topic": "人工智能导论课程大纲",
            "slide_count": 8,
            "pptx_exists": True,
            "preview_success": True,
            "visual_score_avg": 4.1,
            "visual_score_min": 3.8,
            "content_issue_count": 1,
            "repair_attempt_count": 1,
        },
        "summary": {"status": "success", "issue_count": 1},
        "missing_reasons": {},
    }


def _trace() -> dict[str, Any]:
    return {
        "run_id": "run_1",
        "status": "warning",
        "tool_call_count": 4,
        "tool_attempt_count": 5,
        "failed_tool_count": 1,
        "skipped_tool_count": 0,
        "timeout_tool_count": 0,
        "error_signatures": ["ppt.render_preview:PreviewGenerationFailed:no_images"],
        "artifact_refs": {"quality_report_json": "quality_report.json"},
        "quality_report_paths": ["quality_report.json"],
    }


def test_memory_models_serialize_and_validate() -> None:
    record = _record()
    hit = MemoryHit(record=record, score=0.9, reason="key_match")
    query = MemoryQuery(namespace=REPAIR_VISUAL, query="preview", memory_type=MemoryType.PROCEDURAL)

    assert MemoryRecord.model_validate_json(record.model_dump_json()).memory_id == "mem_1"
    assert MemoryHit.model_validate_json(hit.model_dump_json()).score == 0.9
    assert MemoryQuery.model_validate_json(query.model_dump_json()).namespace == REPAIR_VISUAL
    with pytest.raises(ValueError):
        _record(confidence=1.5)
    with pytest.raises(ValueError):
        MemoryQuery(namespace="", query="x")


def test_memory_record_rejects_unsafe_namespace() -> None:
    for namespace in ("", "../secret", "/tmp/path", "bad namespace", "openai_api_key", "token:abc", "sk-secret123456789"):
        with pytest.raises(ValueError):
            _record(namespace=namespace)


def test_memory_query_rejects_unsafe_namespace() -> None:
    for namespace in ("", "../secret", "/tmp/path", "bad namespace", "openai_api_key", "token:abc", "sk-secret123456789"):
        with pytest.raises(ValueError):
            MemoryQuery(namespace=namespace, query="preview")


def test_memory_record_rejects_empty_memory_id() -> None:
    with pytest.raises(ValueError):
        _record(memory_id=" ")


def test_memory_record_rejects_empty_key() -> None:
    with pytest.raises(ValueError):
        _record(key="")


def test_validate_namespace_accepts_and_rejects_unsafe_values() -> None:
    assert validate_namespace("planner:outline") == "planner:outline"
    assert validate_namespace("semantic/courseware") == "semantic/courseware"
    for namespace in ("", "../secret", "/tmp/path", "bad namespace", "openai_api_key", "token:abc", "sk-secret123456789"):
        with pytest.raises(ValueError):
            validate_namespace(namespace)


def test_jsonl_memory_store_write_get_update_and_filters(tmp_path: Path) -> None:
    store = JsonlMemoryStore(tmp_path)
    record = _record()

    first = store.write(record)
    updated = store.write(record.model_copy(update={"content": "Updated repair instruction"}))

    assert first.created is True
    assert updated.updated is True
    assert store.get("mem_1").content == "Updated repair instruction"
    assert len(store.list_records(namespace=REPAIR_VISUAL)) == 1
    assert len(store.list_records(memory_type=MemoryType.PROCEDURAL)) == 1
    assert store.list_records(memory_type=MemoryType.SEMANTIC) == []


def test_jsonl_memory_store_skips_bad_jsonl_lines(tmp_path: Path) -> None:
    store = JsonlMemoryStore(tmp_path)
    record = _record()
    store.write(record)
    path = tmp_path / "procedural" / "repair__visual.jsonl"
    path.write_text("not json\n" + path.read_text(encoding="utf-8"), encoding="utf-8")

    records = store.list_records(namespace=REPAIR_VISUAL, memory_type=MemoryType.PROCEDURAL)

    assert [item.memory_id for item in records] == ["mem_1"]


def test_jsonl_memory_store_sanitizes_source_artifact_paths(tmp_path: Path) -> None:
    store = JsonlMemoryStore(tmp_path)
    record = _record().model_copy(
        update={
            "source_artifacts": {
                "quality_report": "/private/tmp/project/outputs/runs/run_1/quality_report.json",
                "trace_summary": "/home/user/project/outputs/runs/run_1/trace_summary.json",
                "preview": "C:\\Users\\me\\project\\outputs\\runs\\run_1\\preview.png",
            }
        }
    )

    store.write(record)
    loaded = store.get(record.memory_id)
    serialized = json.dumps(loaded.source_artifacts, ensure_ascii=False)

    assert "/private/tmp" not in serialized
    assert "/home/user" not in serialized
    assert "C:\\Users" not in serialized
    assert loaded.source_artifacts["quality_report"] == "runs/run_1/quality_report.json"
    assert loaded.source_artifacts["trace_summary"] == "runs/run_1/trace_summary.json"
    assert loaded.source_artifacts["preview"] == "runs/run_1/preview.png"


def test_jsonl_memory_store_update_rejects_namespace_or_type_change(tmp_path: Path) -> None:
    store = JsonlMemoryStore(tmp_path)
    original = _record(memory_id="same_id")
    changed = _record(
        memory_id="same_id",
        namespace=SEMANTIC_PPT_DESIGN,
        memory_type=MemoryType.SEMANTIC,
        key="semantic_rule",
        content="A semantic rule",
    )

    store.write(original)
    result = store.update(changed)

    assert result.skipped is True
    assert result.reason == "memory namespace or type change is not supported"
    assert [record.namespace for record in store.list_records()] == [REPAIR_VISUAL]
    assert store.list_records(namespace=SEMANTIC_PPT_DESIGN, memory_type=MemoryType.SEMANTIC) == []


def test_retriever_scores_filters_and_ranks_records() -> None:
    strong = _record("strong", content="preview dependency soffice missing", confidence=0.9, success_count=5)
    weak = _record("weak", content="preview dependency soffice missing", confidence=0.2, failure_count=4)
    retired = _record("retired", content="preview dependency soffice missing", lifecycle_state=MemoryLifecycleState.RETIRED)
    query = MemoryQuery(namespace=REPAIR_VISUAL, query="soffice preview", memory_type=MemoryType.PROCEDURAL, top_k=2)

    hits = retrieve_memory_records([weak, retired, strong], query)

    assert [hit.record.memory_id for hit in hits] == ["strong", "weak"]
    assert all(hit.record.lifecycle_state != MemoryLifecycleState.RETIRED for hit in hits)
    assert score_memory_record(strong, query="soffice") > score_memory_record(weak, query="soffice")
    assert retrieve_memory_records([strong], query.model_copy(update={"min_score": 99})) == []


def test_retriever_filters_records_by_query_namespace() -> None:
    visual = _record("visual", namespace=REPAIR_VISUAL, content="preview repair")
    semantic = _record(
        "semantic",
        namespace=SEMANTIC_PPT_DESIGN,
        memory_type=MemoryType.SEMANTIC,
        key="preview repair",
        content="preview repair design rule",
    )
    query = MemoryQuery(namespace=REPAIR_VISUAL, query="preview repair")

    hits = retrieve_memory_records([semantic, visual], query)

    assert [hit.record.memory_id for hit in hits] == ["visual"]


def test_agent_memory_write_query_trace_and_update_outcome(tmp_path: Path) -> None:
    trace_store = TraceStore(tmp_path / "outputs")
    trace = ObservabilityTraceAdapter(run_id="run_memory", trace_store=trace_store)
    memory = AgentMemory(JsonlMemoryStore(tmp_path / "memory"), trace=trace)
    record = _record()

    write_result = memory.write(record)
    hits = memory.query(MemoryQuery(namespace=REPAIR_VISUAL, query="soffice preview", top_k=3))
    success = memory.update_outcome(memory_id=record.memory_id, success=True, metrics={"score": 1})
    failed = memory.update_outcome(memory_id=record.memory_id, success=False)
    events = trace_store.load("run_memory")

    assert write_result.created is True
    assert hits and hits[0].record.memory_id == record.memory_id
    assert success.updated is True
    assert failed.updated is True
    updated = memory.get(record.memory_id)
    assert updated.success_count == 2
    assert updated.failure_count == 1
    assert updated.confidence > 0
    assert {"memory.written", "memory.queried", "memory.hit"}.issubset({event.event_type for event in events})


def test_agent_memory_query_trace_sanitizes_sensitive_query(tmp_path: Path) -> None:
    class FakeTrace:
        def __init__(self) -> None:
            self.records: list[tuple[str, dict[str, Any]]] = []

        def record(self, stage: str, payload: dict[str, Any]) -> None:
            self.records.append((stage, payload))

    trace = FakeTrace()
    memory = AgentMemory(JsonlMemoryStore(tmp_path / "memory"), trace=trace)

    memory.query(
        MemoryQuery(
            namespace=REPAIR_VISUAL,
            query="api_key=sk-secret123456789 system_prompt=private",
            context={"authorization": "Bearer sk-secret123456789", "safe": "preview"},
        )
    )
    payload = json.dumps(trace.records, ensure_ascii=False)

    assert "sk-secret123456789" not in payload
    assert "system_prompt=private" not in payload
    assert "authorization" in payload


def test_agent_memory_trace_failure_is_best_effort(tmp_path: Path) -> None:
    class BrokenTrace:
        def record(self, stage: str, payload: dict[str, Any]) -> None:
            raise RuntimeError("trace down")

    memory = AgentMemory(JsonlMemoryStore(tmp_path / "memory"), trace=BrokenTrace())

    memory.write(_record())
    hits = memory.query(MemoryQuery(namespace=REPAIR_VISUAL, query="preview"))

    assert hits


def test_procedural_repair_memory_adapter_queries_and_converts() -> None:
    @dataclass
    class FakeRepairRecord:
        memory_id: str = "repair_1"
        phase: str = "visual_qa"
        trigger_stage: str = "visual_qa"
        error_signature: str = "visual.low_score:contrast"
        repair_instruction: str = "Increase contrast and simplify layout."
        layout_scope: str = "two_column"
        visual_mode_scope: str = "courseware"
        audience_scope: str = "undergraduate"
        course_type_scope: str = "lecture"
        provider_scope: str = "fake"
        language_scope: str = "zh-CN"
        success_count: int = 3
        failure_count: int = 1
        failure_streak: int = 0
        confidence: float = 0.82
        conditions: tuple[str, ...] = ("low_contrast",)
        before_pattern: str = "low contrast"
        after_pattern: str = "clear contrast"

    class FakeRuntimeMemoryStore:
        def match_records(self, phase: str, **kwargs: Any) -> list[FakeRepairRecord]:
            self.phase = phase
            self.kwargs = kwargs
            return [FakeRepairRecord()]

        def remember_success(self, **kwargs: Any) -> FakeRepairRecord:
            return FakeRepairRecord(memory_id="repair_2")

    legacy = FakeRuntimeMemoryStore()
    adapter = ProceduralRepairMemoryAdapter(legacy)

    hits = adapter.query_repair_memories(phase="visual_qa", error_signature="visual.low_score:contrast", top_k=2)
    remembered = adapter.remember_repair_success(phase="visual_qa")

    assert legacy.kwargs["max_items"] == 2
    assert hits[0].record.memory_type == MemoryType.PROCEDURAL
    assert hits[0].record.namespace == REPAIR_VISUAL
    assert hits[0].record.key == "visual.low_score:contrast"
    assert hits[0].record.content == "Increase contrast and simplify layout."
    assert hits[0].record.context["layout_scope"] == "two_column"
    assert hits[0].record.outcome["success_count"] == 3
    assert remembered.memory_id == "repair_2"


def test_episode_memory_from_run_artifacts_extracts_quality_and_trace(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run_1"
    _write_run_artifacts(run_dir, quality=_quality(), trace=_trace())

    record = build_episode_memory_from_run_artifacts(run_id="run_1", run_dir=run_dir)

    assert record.memory_type == MemoryType.EPISODIC
    assert record.namespace == "orchestrator:episode"
    assert record.context["trace"]["tool_call_count"] == 4
    assert record.context["trace"]["error_signatures"] == ["ppt.render_preview:PreviewGenerationFailed:no_images"]
    assert record.outcome["status"] == "warning"
    assert record.outcome["visual_score_avg"] == 4.1
    assert "tool_failure" in record.tags
    assert len(record.content) <= 1000 + len("... [TRUNCATED]")
    assert "quality_report" in record.source_artifacts


def test_episode_memory_missing_artifacts_does_not_raise(tmp_path: Path) -> None:
    record = build_episode_memory_from_run_artifacts(run_id="missing", run_dir=tmp_path / "missing")

    assert record.memory_type == MemoryType.EPISODIC
    assert "missing quality_report.json" in record.context["missing"]
    assert "missing trace_summary.json" in record.context["missing"]
    assert record.outcome["status"] == "unknown"


def test_semantic_memory_helper_creates_record_without_llm() -> None:
    record = create_semantic_memory(
        namespace=SEMANTIC_PPT_DESIGN,
        key="contrast_rule",
        content="Courseware slides should keep chart labels readable.",
        tags=["rubric", "design"],
        context={"source": "manual"},
        confidence=0.9,
    )

    assert record.memory_type == MemoryType.SEMANTIC
    assert record.tags == ["rubric", "design"]
    assert record.context["source"] == "manual"
    assert record.confidence == 0.9


def test_memory_promotion_policy_reasons_and_success() -> None:
    policy = MemoryPromotionPolicy(min_success_count=3, max_failure_count=1, min_confidence=0.75)

    assert should_promote_memory(_record(success_count=2, confidence=0.9), policy)[0] is False
    assert should_promote_memory(_record(success_count=3, confidence=0.5), policy)[0] is False
    assert should_promote_memory(_record(success_count=3, failure_count=2, confidence=0.9), policy)[0] is False
    assert should_promote_memory(_record(success_count=3, confidence=0.9), policy.model_copy(update={"require_benchmark_pass": True}))[0] is False
    assert should_promote_memory(_record(success_count=3, confidence=0.9), policy)[0] is True


def test_memory_promotion_policy_rejects_stale_memory() -> None:
    promoted, reasons = should_promote_memory(
        _record(success_count=3, confidence=0.9, lifecycle_state=MemoryLifecycleState.STALE),
        MemoryPromotionPolicy(),
    )

    assert promoted is False
    assert "memory is stale" in reasons


def test_memory_promotion_policy_rejects_already_promoted_memory() -> None:
    promoted, reasons = should_promote_memory(
        _record(success_count=3, confidence=0.9, promotion_state=MemoryPromotionState.PROMOTED),
        MemoryPromotionPolicy(),
    )

    assert promoted is False
    assert "memory already promoted" in reasons


def test_integration_helpers_create_memory_write_episode_and_trace_payload(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    run_dir = output_root / "runs" / "run_1"
    _write_run_artifacts(run_dir, quality=_quality(), trace=_trace())
    memory = create_default_agent_memory(output_root=output_root)

    result = write_episode_memory_for_run(run_id="run_1", run_dir=run_dir, memory=memory)
    record = memory.get(result.memory_id)
    payload = memory_hit_to_trace_payload(MemoryHit(record=record, score=0.7, reason="episode"))

    assert result.created is True
    assert record is not None
    assert payload["memory_id"] == record.memory_id
    assert "content" not in payload
    assert len(payload["content_excerpt"]) <= 115


def test_memory_hit_to_trace_payload_sanitizes_key() -> None:
    record = _record(key="api_key=sk-secret123456789")
    payload = memory_hit_to_trace_payload(MemoryHit(record=record, score=0.5, reason="contains sk-secret123456789"))
    serialized = json.dumps(payload, ensure_ascii=False)

    assert "sk-secret123456789" not in serialized
    assert "api_key=" not in payload["key"]
