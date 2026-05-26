from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from backend.harness.tooling import (  # noqa: E402
    ToolCall,
    ToolError,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_error_signature,
    create_default_tool_registry,
    tool_result_to_quality_error,
)
from backend.harness.tooling.errors import DuplicateToolError, ToolNotFoundError  # noqa: E402


def _call(tool_name: str, input_payload: dict | None = None) -> ToolCall:
    return ToolCall(
        run_id="run_tool_test",
        call_id=f"call_{tool_name.replace('.', '_')}",
        tool_name=tool_name,
        caller="test",
        input=input_payload or {},
        purpose="unit test",
    )


def test_tool_registry_registers_and_lists_specs() -> None:
    registry = ToolRegistry()
    spec = ToolSpec(name="fake.success", description="Fake success tool")

    registry.register(spec, lambda call: {"ok": True})

    assert registry.has("fake.success")
    assert registry.get("fake.success").spec.name == "fake.success"
    assert [item.name for item in registry.list_specs()] == ["fake.success"]


def test_tool_registry_rejects_duplicate_and_missing_tools() -> None:
    registry = ToolRegistry()
    spec = ToolSpec(name="fake.duplicate", description="Fake duplicate tool")
    registry.register(spec, lambda call: {"ok": True})

    with pytest.raises(DuplicateToolError):
        registry.register(spec, lambda call: {"ok": False})

    with pytest.raises(ToolNotFoundError):
        registry.get("fake.missing")


def test_default_registry_contains_first_batch_tools() -> None:
    registry = create_default_tool_registry()
    names = {spec.name for spec in registry.list_specs()}

    assert {
        "ppt.check_js_syntax",
        "ppt.run_pptxgenjs",
        "ppt.read_pptx_text",
        "ppt.render_preview",
        "search.web_text",
        "search.image",
        "document.extract_text",
        "document.summarize",
        "eval.visual_slides",
        "eval.content_text",
    }.issubset(names)


def test_tool_executor_wraps_fake_success_dict() -> None:
    registry = ToolRegistry()
    registry.register(ToolSpec(name="fake.success", description="Fake success tool"), lambda call: {"ok": True})

    result = asyncio.run(ToolExecutor(registry).execute(_call("fake.success")))

    assert result.status == "success"
    assert result.output == {"ok": True}
    assert result.latency_ms >= 0


def test_tool_executor_returns_failed_result_instead_of_throwing() -> None:
    def fail(_call: ToolCall) -> dict:
        raise ValueError("bad input from /private/tmp/source.txt with api_key=secret-value")

    registry = ToolRegistry()
    registry.register(ToolSpec(name="fake.fail", description="Fake failing tool"), fail)

    result = asyncio.run(ToolExecutor(registry).execute(_call("fake.fail")))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_type == "ValueError"
    assert "api_key=secret-value" not in result.error.message
    assert "/private/tmp" not in result.error.error_signature


def test_tool_executor_retries_flaky_tool() -> None:
    attempts = {"count": 0}

    def flaky(_call: ToolCall) -> dict:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ConnectionError("provider unavailable")
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(ToolSpec(name="fake.flaky", description="Fake flaky tool", retry=1), flaky)

    result = asyncio.run(ToolExecutor(registry).execute(_call("fake.flaky")))

    assert result.status == "success"
    assert attempts["count"] == 2


def test_error_signature_is_stable_short_and_sanitized() -> None:
    message = (
        "SyntaxError: missing ; in /Users/test/project/private/source.js "
        "api_key=sk-super-secret-token " + "x" * 500
    )

    first = build_error_signature(tool_name="ppt.run_pptxgenjs", error_type="SyntaxError", message=message)
    second = build_error_signature(tool_name="ppt.run_pptxgenjs", error_type="SyntaxError", message=message)

    assert first == second
    assert len(first) <= 120
    assert "/Users/test" not in first
    assert "sk-super-secret-token" not in first
    assert first == "ppt.run_pptxgenjs:SyntaxError:missing_semicolon"


def test_tool_result_to_quality_error_converts_failed_result() -> None:
    result = ToolResult(
        run_id="run",
        call_id="call",
        tool_name="ppt.render_preview",
        status="failed",
        error=ToolError(
            error_type="PreviewRenderError",
            message="Preview rendering produced no images",
            error_signature="ppt.render_preview:PreviewRenderError:no_images",
        ),
        latency_ms=12,
    )

    quality_error = tool_result_to_quality_error(result)

    assert quality_error == {
        "tool": "ppt.render_preview",
        "stage": "ppt.render_preview",
        "status": "failed",
        "error_type": "PreviewRenderError",
        "error_signature": "ppt.render_preview:PreviewRenderError:no_images",
        "message": "Preview rendering produced no images",
        "latency_ms": 12,
    }
    assert tool_result_to_quality_error(result.model_copy(update={"status": "success", "error": None})) is None


def test_builtin_ppt_tools_are_wrappers_and_can_be_monkeypatched(monkeypatch, tmp_path: Path) -> None:
    from backend.tools import pptx_skill

    output_path = tmp_path / "deck.pptx"
    source_path = tmp_path / "source.pptx"
    source_path.write_bytes(b"pptx marker")
    preview_dir = tmp_path / "preview"

    monkeypatch.setattr(pptx_skill, "check_js_syntax", lambda code, timeout=20: (True, ""))

    def fake_run_js(code: str, output_path: str, timeout: int = 60) -> str:
        Path(output_path).write_bytes(b"pptx")
        return output_path

    monkeypatch.setattr(pptx_skill, "run_js", fake_run_js)
    monkeypatch.setattr(pptx_skill, "read_pptx", lambda path: "hello ppt")
    monkeypatch.setattr(pptx_skill, "pptx_to_images", lambda pptx_path, output_dir=None: [str(Path(output_dir) / "slide-1.jpg")])

    registry = create_default_tool_registry()
    executor = ToolExecutor(registry)

    syntax = asyncio.run(executor.execute(_call("ppt.check_js_syntax", {"code": "const x = 1;"})))
    generated = asyncio.run(
        executor.execute(
            _call(
                "ppt.run_pptxgenjs",
                {"code": "pptx.writeFile()", "output_path": str(output_path), "timeout_s": 5},
            )
        )
    )
    text = asyncio.run(executor.execute(_call("ppt.read_pptx_text", {"pptx_path": str(source_path)})))
    preview = asyncio.run(
        executor.execute(
            _call(
                "ppt.render_preview",
                {"pptx_path": str(source_path), "output_dir": str(preview_dir)},
            )
        )
    )

    assert syntax.status == "success"
    assert syntax.output["valid"] is True
    assert generated.status == "success"
    assert generated.output["exists"] is True
    assert text.output["text_length"] == 9
    assert preview.output["preview_count"] == 1


def test_ppt_run_pptxgenjs_fails_when_artifact_is_missing(monkeypatch, tmp_path: Path) -> None:
    from backend.tools import pptx_skill

    output_path = tmp_path / "missing.pptx"
    monkeypatch.setattr(pptx_skill, "run_js", lambda code, output_path, timeout=60: output_path)

    registry = create_default_tool_registry()
    executor = ToolExecutor(registry)

    result = asyncio.run(
        executor.execute(
            _call(
                "ppt.run_pptxgenjs",
                {"code": "pptx.writeFile()", "output_path": str(output_path), "timeout_s": 5},
            )
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_type == "PptxArtifactMissing"
    assert result.error.error_signature.startswith("ppt.run_pptxgenjs:PptxArtifactMissing:")
    assert output_path.exists() is False


def test_builtin_search_document_and_eval_tools_do_not_call_external_services(monkeypatch, tmp_path: Path) -> None:
    from backend.harness.agents import document_summary
    from backend.tools import search_backend

    class DisabledSearchBackend:
        enabled = False

    monkeypatch.setattr(search_backend, "SearchBackend", DisabledSearchBackend)
    monkeypatch.setattr(document_summary, "extract_document_content", lambda path: ("doc text", [[["h"], ["v"]]], 1))

    document_path = tmp_path / "input.md"
    document_path.write_text("# doc", encoding="utf-8")

    registry = create_default_tool_registry()
    executor = ToolExecutor(registry)

    search_result = asyncio.run(executor.execute(_call("search.web_text", {"query": "hello", "max_results": 1})))
    image_result = asyncio.run(executor.execute(_call("search.image", {"query": "hello", "max_results": 1})))
    extracted = asyncio.run(executor.execute(_call("document.extract_text", {"document_path": str(document_path)})))
    summarized = asyncio.run(executor.execute(_call("document.summarize", {"text": "hello", "language": "zh-CN"})))
    visual_eval = asyncio.run(executor.execute(_call("eval.visual_slides", {"preview_images": [], "outline": {}})))
    content_eval = asyncio.run(executor.execute(_call("eval.content_text", {"text": "hello", "outline": {}})))

    assert search_result.status == "skipped"
    assert image_result.status == "skipped"
    assert extracted.status == "success"
    assert extracted.output["text_length"] == 8
    assert summarized.status == "skipped"
    assert visual_eval.status == "skipped"
    assert content_eval.status == "skipped"
