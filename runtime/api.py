from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import config
from backend.harness.agents import orchestrator as orchestrator_module
from backend.harness.agents import planner as planner_module
from backend.harness.agents.content_eval import (
    PPTEvaluationRequest,
    evaluate_ppt_content,
    stream_evaluate_ppt_content,
)
from backend.harness.agents.document_summary import (
    DocumentSummary,
    extract_document_content,
    summarize_document,
)
from backend.harness.agents.orchestrator import OrchestratorAgent
from backend.harness.quality import QualityCollector, write_quality_report
from backend.harness.runtime import (
    HarnessTrace,
    get_learned_skill_registry,
    get_skill_asset_registry,
    get_skill_policy_map,
)
from backend.models.schemas import OutlinePlan, SlideLayout
from backend.tools.pptx_skill import get_preview_runtime_diagnostics, read_pptx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_ROOT = Path(config.OUTPUT_DIR).resolve()
FRONTEND_INDEX = (BASE_DIR / "static" / "index.html").resolve()
MAX_UPLOAD_DOCUMENTS = 10
logger = logging.getLogger(__name__)


def _load_ppt_render_concurrency() -> int:
    try:
        value = int(os.getenv("DIRECTIONAI_PPT_RENDER_CONCURRENCY", "2"))
    except ValueError:
        return 2
    return max(1, value)


_PPT_PROCESS_POOL_WORKERS = _load_ppt_render_concurrency()
_PPT_RENDER_IN_PROCESS = os.getenv("DIRECTIONAI_PPT_RENDER_IN_PROCESS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_PPT_PROCESS_POOL_LOCK = threading.Lock()
_PPT_PROCESS_POOL = None if _PPT_RENDER_IN_PROCESS else ProcessPoolExecutor(max_workers=_PPT_PROCESS_POOL_WORKERS)
_PPT_FALLBACK_SEM = threading.Semaphore(_PPT_PROCESS_POOL_WORKERS)


def _replace_ppt_process_pool() -> ProcessPoolExecutor:
    global _PPT_PROCESS_POOL
    if _PPT_RENDER_IN_PROCESS:
        raise RuntimeError("PPT render process pool is disabled")
    with _PPT_PROCESS_POOL_LOCK:
        previous = _PPT_PROCESS_POOL
        _PPT_PROCESS_POOL = ProcessPoolExecutor(max_workers=_PPT_PROCESS_POOL_WORKERS)
    if previous is not None:
        previous.shutdown(wait=False, cancel_futures=True)
    return _PPT_PROCESS_POOL


def _submit_ppt_process_job(job_name: str, timeout_seconds: int, func: Callable[..., Any], *args: Any) -> Any:
    global _PPT_PROCESS_POOL
    if _PPT_RENDER_IN_PROCESS or _PPT_PROCESS_POOL is None:
        with _PPT_FALLBACK_SEM:
            return func(*args)
    try:
        future = _PPT_PROCESS_POOL.submit(func, *args)
        return future.result(timeout=timeout_seconds)
    except BrokenProcessPool:
        logger.exception("[PPTRender] Process pool broke while running %s; resetting pool", job_name)
        _replace_ppt_process_pool()
        logger.warning("[PPTRender] Falling back to in-process execution for %s after pool reset", job_name)
        with _PPT_FALLBACK_SEM:
            return func(*args)


def _run_pptx_to_images_in_process(pptx_path: str, output_dir: str | None = None) -> list[str]:
    from backend.tools.pptx_skill import pptx_to_images as _real

    return _real(pptx_path, output_dir)


def _run_js_in_process(code: str, output_path: str, timeout: int = 60) -> str:
    from backend.tools.pptx_skill import run_js as _real

    return _real(code, output_path, timeout=timeout)


def _pptx_to_images_via_process_pool(pptx_path: str, output_dir: str | None = None) -> list[str]:
    return _submit_ppt_process_job(
        "pptx_to_images",
        180,
        _run_pptx_to_images_in_process,
        pptx_path,
        output_dir,
    )


def _run_js_via_process_pool(code: str, output_path: str, timeout: int = 60) -> str:
    return _submit_ppt_process_job(
        "assemble_pptx",
        max(180, timeout + 60),
        _run_js_in_process,
        code,
        output_path,
        timeout,
    )


planner_module.run_js = _run_js_in_process if _PPT_RENDER_IN_PROCESS else _run_js_via_process_pool
orchestrator_module.pptx_to_images = (
    _run_pptx_to_images_in_process if _PPT_RENDER_IN_PROCESS else _pptx_to_images_via_process_pool
)

if _PPT_RENDER_IN_PROCESS:
    logger.info("Initialized PPT render in-process mode")
else:
    logger.info("Initialized PPT render process pool with %s workers", _PPT_PROCESS_POOL_WORKERS)


def _install_ppt_render_guard(orchestrator: OrchestratorAgent) -> OrchestratorAgent:
    """Compatibility hook for existing call sites.

    Heavyweight PPT rendering is now isolated through the module-level process
    pool wrappers installed above, so the instance itself no longer needs an
    extra in-process semaphore guard.
    """
    return orchestrator


def slugify(text: str) -> str:
    name = re.sub(r"[^\w\u4e00-\u9fff]", "_", text)
    name = re.sub(r"_+", "_", name).strip("_")
    return name[:50] or "output"


def parse_slide_range(slides_raw: str, default: tuple[int, int] = (6, 10)) -> tuple[int, int]:
    text = (slides_raw or "").strip()
    if not text:
        return default

    range_match = re.match(r"^(\d+)\s*[-~～]\s*(\d+)$", text)
    if range_match:
        min_slides, max_slides = int(range_match.group(1)), int(range_match.group(2))
        return tuple(sorted((min_slides, max_slides)))

    if text.isdigit():
        value = int(text)
        return value, value

    return default


def _derive_topic(
    topic: str | None,
    course: str | None,
    constraint: str | None,
    units: list[str],
    lessons: list[str],
    knowledge_points: list[str],
) -> str:
    direct = (topic or "").strip()
    if direct:
        return direct

    parts: list[str] = []

    course_text = (course or "").strip()
    if course_text and course_text != "不限制":
        parts.append(course_text)

    if lessons:
        parts.append("、".join(item.strip() for item in lessons if item and item.strip())[:80])
    elif units:
        parts.append("、".join(item.strip() for item in units if item and item.strip())[:80])
    elif knowledge_points:
        parts.append("、".join(item.strip() for item in knowledge_points if item and item.strip())[:80])

    constraint_text = (constraint or "").strip()
    if constraint_text:
        parts.append(constraint_text[:120])

    cleaned = [part for part in parts if part]
    return " - ".join(cleaned[:2]).strip()


class PPTGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    topic: str | None = Field(default=None, description="PPT 主题")
    model_provider: Literal["minmax", "claude", "qwen", "deepseek"] = Field(default="minmax", description="模型供应商")
    output_language: str = Field(default="中文", description="输出语言")
    target_audience: str = Field(default="general", description="目标受众")

    style: str = Field(default="", description="PPT 风格，为空时自动决定")
    enable_web_search: bool = Field(default=False, description="是否启用联网检索")
    image_mode: Literal["generate", "search", "auto", "off"] = Field(
        default="generate",
        description="图片模式：generate=豆包生图，search=仅搜图，auto=先搜图后生图，off=关闭图片",
    )
    min_slides: int = Field(default=6, ge=2, le=config.MAX_PPT_SLIDES, description="最少页数")
    max_slides: int = Field(default=10, ge=2, le=config.MAX_PPT_SLIDES, description="最多页数")
    debug_layout: bool = Field(default=False, description="是否输出调试布局")

    # Compatibility fields for existing frontend payloads.
    language: str | None = None
    audience: str | None = None
    slides: str | None = None
    page_limit: int | None = Field(default=None, ge=2, le=config.MAX_PPT_SLIDES)
    use_rag: bool | None = None
    course: str | None = None
    units: list[str] = Field(default_factory=list)
    lessons: list[str] = Field(default_factory=list)
    knowledge_points: list[str] = Field(default_factory=list)
    constraint: str | None = None
    content: str | None = None
    document_text: str | None = Field(
        default=None,
        description="上传文档提取后的原始文本，与 document_name 配套使用",
    )
    document_name: str | None = Field(
        default=None,
        description="上传文档的文件名，用于标识来源",
    )
    document_texts: list[str] = Field(
        default_factory=list,
        description="上传文档提取后的原始文本列表，按上传顺序排列",
    )
    document_names: list[str] = Field(
        default_factory=list,
        description="上传文档文件名列表，与 document_texts 一一对应",
    )

    @model_validator(mode="after")
    def normalize(self) -> PPTGenerationRequest:
        if self.language and self.output_language == "中文":
            self.output_language = self.language
        if self.audience and self.target_audience == "general":
            self.target_audience = self.audience
        if self.use_rag is not None:
            self.enable_web_search = bool(self.use_rag)

        if self.page_limit:
            self.min_slides = self.page_limit
            self.max_slides = self.page_limit
        elif self.slides:
            self.min_slides, self.max_slides = parse_slide_range(
                self.slides,
                default=(self.min_slides, self.max_slides),
            )

        if self.max_slides < self.min_slides:
            self.min_slides, self.max_slides = self.max_slides, self.min_slides

        self.topic = _derive_topic(
            topic=self.topic,
            course=self.course,
            constraint=self.constraint,
            units=self.units,
            lessons=self.lessons,
            knowledge_points=self.knowledge_points,
        )
        if not self.topic:
            raise ValueError("topic 不能为空")

        self.output_language = (self.output_language or "中文").strip() or "中文"
        self.target_audience = (self.target_audience or "general").strip() or "general"

        if (self.style or "").lower() == "auto":
            self.style = ""

        normalized_document_texts = [str(item or "").strip() for item in self.document_texts if str(item or "").strip()]
        normalized_document_names = [str(item or "").strip() for item in self.document_names]

        if normalized_document_texts:
            self.document_texts = normalized_document_texts
            self.document_names = [normalized_document_names[index] if index < len(normalized_document_names) and normalized_document_names[index] else f"上传文档{index + 1}" for index in range(len(normalized_document_texts))]
            if not (self.document_text or "").strip():
                self.document_text = normalized_document_texts[0]
            if not (self.document_name or "").strip():
                self.document_name = self.document_names[0]
        elif (self.document_text or "").strip():
            self.document_text = str(self.document_text or "").strip()
            self.document_name = (self.document_name or "上传文档").strip() or "上传文档"
            self.document_texts = [self.document_text]
            self.document_names = [self.document_name]
        else:
            self.document_text = None
            self.document_name = None
            self.document_texts = []
            self.document_names = []

        return self


@dataclass
class GenerationArtifacts:
    output_path: str
    output_filename: str
    markdown_content: str
    total_slides: int
    biz_id: str
    preview_images: list[str] | None = None
    preview_warning: str = ""
    harness_trace: dict[str, Any] | None = None
    harness_trace_path: str = ""

    def to_response(self) -> dict[str, Any]:
        download_url = f"/download_ppt/{self.output_filename}"
        preview_images = self.preview_images
        if preview_images is None:
            preview_images = _collect_preview_image_urls(self.output_filename, self.output_path)
        return {
            "status": "success",
            "markdown_content": self.markdown_content,
            "pptx_file_name": self.output_filename,
            "pptx_file_path": self.output_path,
            "display_url": download_url,
            "download_url": download_url,
            "preview_images": preview_images,
            "preview_warning": self.preview_warning,
            "biz_id": self.biz_id,
            "harness_trace_available": bool(self.harness_trace_path),
            "harness_trace_path": self.harness_trace_path,
        }


def _preview_dir_for_artifact(output_filename: str, output_path: str | None = None) -> Path:
    stem = Path(output_filename).stem
    base_dir = Path(output_path).resolve().parent if output_path else OUTPUT_ROOT
    return (base_dir / "slides_preview" / stem).resolve()


def _harness_trace_path_for_artifact(output_filename: str, output_path: str | None = None) -> Path:
    stem = Path(output_filename).stem
    base_dir = Path(output_path).resolve().parent if output_path else OUTPUT_ROOT
    return (base_dir / "harness_traces" / f"{stem}.json").resolve()


def _write_harness_trace(output_filename: str, output_path: str, harness_trace: dict[str, Any]) -> str:
    if not harness_trace:
        return ""
    trace_path = _harness_trace_path_for_artifact(output_filename, output_path)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(json.dumps(harness_trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(trace_path)


def _write_quality_report_safely(
    *,
    run_id: str,
    topic: str | None,
    pptx_path: str | None,
    preview_images: list[str] | None,
    extracted_text: str | None,
    visual_eval_results: list[Any] | None,
    content_issues: list[Any] | None,
    repair_events: list[Any] | None,
    tool_errors: list[Any] | None = None,
    stage_latency_ms: dict[str, int] | None = None,
    harness_trace: HarnessTrace | None = None,
) -> dict[str, str]:
    try:
        report = QualityCollector().collect(
            run_id=run_id,
            topic=topic,
            pptx_path=pptx_path,
            preview_images=preview_images,
            extracted_text=extracted_text,
            visual_eval_results=visual_eval_results,
            content_issues=content_issues,
            repair_events=repair_events,
            tool_errors=tool_errors,
            stage_latency_ms=stage_latency_ms,
        )
        paths = write_quality_report(report, OUTPUT_ROOT)
        if harness_trace:
            harness_trace.record(
                stage="quality_report",
                payload={
                    "status": report.summary.get("status"),
                    "json_path": paths.get("json_path", ""),
                    "markdown_path": paths.get("markdown_path", ""),
                    "issue_count": report.summary.get("issue_count", 0),
                },
            )
        return paths
    except Exception as exc:
        logger.warning("[QualityHarness] Failed to write quality report; continuing generation: %s", exc)
        if harness_trace:
            harness_trace.record(
                stage="quality_report",
                payload={"status": "failed", "error": str(exc)[:300]},
            )
        return {}


def _preview_image_sort_key(path: Path) -> tuple[int, str]:
    match = re.search(r"(\d+)(?=\.[^.]+$)", path.name)
    if not match:
        return (10**9, path.name)
    return (int(match.group(1)), path.name)


def _collect_preview_image_urls(output_filename: str, output_path: str | None = None) -> list[str]:
    preview_dir = _preview_dir_for_artifact(output_filename, output_path)
    if not preview_dir.exists() or not preview_dir.is_dir():
        return []

    images = sorted(
        (path for path in preview_dir.iterdir() if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}),
        key=_preview_image_sort_key,
    )
    return [f"/preview_ppt/{output_filename}/{path.name}" for path in images]


def _build_preview_warning(
    output_filename: str,
    output_path: str | None,
    preview_images: list[str],
    *,
    visual_qa_enabled: bool,
) -> str:
    if preview_images:
        return ""

    if not visual_qa_enabled:
        return "当前环境未开启视觉 QA（QWEN_API_KEY 或 QWEN_BASE_URL 未配置），当前流程会跳过缩略图渲染，请先使用“导出为PPT”查看文件。"

    runtime = get_preview_runtime_diagnostics()
    missing: list[str] = []
    if not runtime.get("soffice_found"):
        missing.append("未找到 soffice（LibreOffice）")
    if not runtime.get("pdftoppm_found"):
        missing.append("未找到 pdftoppm")

    if missing:
        return "当前未生成缩略图预览：" + "；".join(missing) + "。请安装缺失依赖后重试，或先使用“导出为PPT”查看文件。"

    preview_dir = _preview_dir_for_artifact(output_filename, output_path)
    if preview_dir.exists() and preview_dir.is_dir():
        return "当前未生成缩略图预览：已创建 slides_preview 目录但没有图片文件，通常是 soffice 转 PDF 或 pdftoppm 转图失败，请查看后端日志中的 [PptxSkill] 记录。"

    return "当前未生成缩略图预览：PPT 文件已生成，但后台没有产出 slides_preview 图片。请检查后端日志中的 [PptxSkill] 记录，以及 workspace/outputs/slides_preview 目录写权限。"


def _refresh_progressive_preview_images(
    *,
    orchestrator: OrchestratorAgent,
    slide_codes: list[str],
    theme: dict[str, Any],
    output_path: str,
) -> list[str]:
    runtime = get_preview_runtime_diagnostics()
    if not runtime.get("soffice_found") or not runtime.get("pdftoppm_found"):
        return []

    if not slide_codes:
        return []

    preview_output = orchestrator.planner.assemble_pptx(list(slide_codes), output_path, theme)
    preview_dir = _preview_dir_for_artifact(Path(preview_output).name, preview_output)
    preview_dir.mkdir(parents=True, exist_ok=True)
    _pptx_to_images_via_process_pool(preview_output, str(preview_dir))
    return _collect_preview_image_urls(Path(preview_output).name, preview_output)


def _render_preview_images_for_pptx(pptx_path: str) -> list[str]:
    runtime = get_preview_runtime_diagnostics()
    if not runtime.get("soffice_found") or not runtime.get("pdftoppm_found"):
        return []

    output_filename = Path(pptx_path).name
    preview_dir = _preview_dir_for_artifact(output_filename, pptx_path)
    preview_dir.mkdir(parents=True, exist_ok=True)
    _pptx_to_images_via_process_pool(pptx_path, str(preview_dir))
    return _collect_preview_image_urls(output_filename, pptx_path)


def _collect_or_render_preview_images(pptx_path: str) -> list[str]:
    output_filename = Path(pptx_path).name
    preview_images = _collect_preview_image_urls(output_filename, pptx_path)
    if preview_images:
        return preview_images
    return _render_preview_images_for_pptx(pptx_path)


def _build_slide_summary(slide, research: dict | None, image_path: str | None) -> str:
    summary_parts = [
        f"布局：{slide.layout.value}",
        f"目标：{slide.objective or slide.topic}",
    ]

    bullet_points = (research or {}).get("bullet_points") or []
    if bullet_points:
        summary_parts.append(f"研究要点：{len(bullet_points)} 条")
    if image_path:
        summary_parts.append("已提供本地配图")
    elif slide.layout in {SlideLayout.CONTENT, SlideLayout.TWO_COLUMN}:
        summary_parts.append("本页将以图形或文字视觉为主")

    return "；".join(summary_parts) + "。"


def _build_page_thinking_summary(slide, research: dict | None, image_path: str | None) -> str:
    parts = [f"正在整理“{slide.topic}”这一页的内容。"]

    objective = (getattr(slide, "objective", "") or "").strip()
    if objective:
        parts.append(f"这一页会重点讲清：{objective}。")

    bullets = (research or {}).get("bullet_points") or []
    if bullets:
        parts.append("会把查到的关键信息压缩成更容易理解的表达。")

    if image_path:
        parts.append("配图会跟着这一页的重点走，避免喧宾夺主。")

    return "".join(parts)


def _should_apply_document_slide_suggestion(req: PPTGenerationRequest) -> bool:
    return not bool(req.page_limit or req.slides)


def _request_documents(req: PPTGenerationRequest) -> list[UploadDocumentItem]:
    return [
        UploadDocumentItem(
            document_name=req.document_names[index],
            document_text=req.document_texts[index],
            char_count=len(req.document_texts[index]),
            page_count=0,
        )
        for index in range(len(req.document_texts))
    ]


def _document_excerpt(text: str, limit: int = 220) -> str:
    normalized = re.sub(r"\s+", " ", (text or "")).strip()
    if not normalized:
        return "未提取到有效文本。"
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _fallback_document_slide_count(text: str) -> int:
    length = len((text or "").strip())
    if length >= 8000:
        return 3
    if length >= 3000:
        return 2
    return 1


def _build_raw_document_context(documents: list[UploadDocumentItem], limit_per_document: int = 12000) -> str:
    blocks: list[str] = []
    for index, document in enumerate(documents):
        blocks.append(f"【文档 {index + 1}：{document.document_name}】\n{document.document_text[:limit_per_document]}")
    return "\n\n".join(blocks)


def _book_ppt_direct_document_context_enabled() -> bool:
    return os.getenv("DIRECTIONAI_BOOK_PPT_DIRECT_DOCUMENT_CONTEXT", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _book_ppt_direct_document_context_limit() -> int:
    try:
        value = int(os.getenv("DIRECTIONAI_BOOK_PPT_DIRECT_DOCUMENT_CONTEXT_CHARS", "60000"))
    except ValueError:
        return 60000
    return max(12000, min(120000, value))


def _build_book_ppt_direct_document_context(documents: list[UploadDocumentItem]) -> str:
    return (
        "【电子书课时原文直读模式】\n"
        "本任务已经由 Phase1/Phase2 完成目录、页码和课时规划。"
        "不要再用通用摘要替代原文，请直接依据以下当前课时原文和 content 中的 toc_index/lesson_plan 约束规划大纲。\n\n"
        + _build_raw_document_context(
            documents,
            limit_per_document=_book_ppt_direct_document_context_limit(),
        )
    )


def _merge_document_summaries(
    documents: list[UploadDocumentItem],
    summaries: list[DocumentSummary | None],
) -> DocumentSummary | None:
    if not documents:
        return None

    section_payloads: list[dict[str, Any]] = []
    recommended_visual_elements: list[str] = []
    summary_parts: list[str] = []
    title_candidates: list[str] = []
    total_pages = 0
    audience = "general"
    style_preference = "informative"
    content_focus = "knowledge_sharing"
    special_requirements: list[str] = []
    multiple_documents = len(documents) > 1

    for index, document in enumerate(documents):
        summary = summaries[index] if index < len(summaries) else None
        summary_title = (summary.document_title or "").strip() if summary else ""
        document_label = summary_title or document.document_name or f"文档{index + 1}"
        title_candidates.append(document_label)

        if summary:
            summary_parts.append(f"{document_label}：{summary.document_summary.strip()}")
            total_pages += int(summary.metadata.total_pages or document.page_count or 0)

            hints = summary.ppt_generation_hints
            if audience == "general" and (hints.audience or "").strip() and hints.audience != "general":
                audience = hints.audience
            if style_preference == "informative" and (hints.style_preference or "").strip() and hints.style_preference != "informative":
                style_preference = hints.style_preference
            if content_focus == "knowledge_sharing" and (hints.content_focus or "").strip() and hints.content_focus != "knowledge_sharing":
                content_focus = hints.content_focus
            if hints.special_requirements and hints.special_requirements not in special_requirements:
                special_requirements.append(hints.special_requirements)
            for item in hints.recommended_visual_elements:
                candidate = str(item or "").strip()
                if candidate and candidate not in recommended_visual_elements:
                    recommended_visual_elements.append(candidate)

            for section in summary.sections:
                section_payloads.append(
                    {
                        "section_title": (f"{document_label} · {section.section_title}" if multiple_documents else section.section_title),
                        "section_summary": section.section_summary,
                        "key_points": list(section.key_points),
                        "tables": [table.model_dump(mode="json") for table in section.tables],
                        "important_data": list(section.important_data),
                        "suggested_slide_count": section.suggested_slide_count,
                        "narrative_transition": section.narrative_transition,
                    }
                )
            continue

        total_pages += int(document.page_count or 0)
        excerpt = _document_excerpt(document.document_text)
        summary_parts.append(f"{document_label}：{excerpt}")
        section_payloads.append(
            {
                "section_title": document_label,
                "section_summary": excerpt,
                "key_points": [],
                "tables": [],
                "important_data": [],
                "suggested_slide_count": _fallback_document_slide_count(document.document_text),
                "narrative_transition": None,
            }
        )

    if not section_payloads:
        return None

    body_slide_count = sum(int(item.get("suggested_slide_count") or 1) for item in section_payloads)
    suggested_total_slides = max(4, min(config.MAX_PPT_SLIDES, body_slide_count + 2))
    title_preview = "、".join(title_candidates[:3])
    if len(title_candidates) > 3:
        title_preview += "等"
    document_title = title_candidates[0] if len(title_candidates) == 1 else f"综合资料：{title_preview}"

    return DocumentSummary.model_validate(
        {
            "document_title": document_title,
            "document_summary": " ".join(summary_parts[:3])[:500] or document_title,
            "sections": section_payloads,
            "metadata": {
                "source_file": "；".join(item.document_name for item in documents),
                "total_pages": total_pages or None,
            },
            "ppt_generation_hints": {
                "suggested_total_slides": suggested_total_slides,
                "audience": audience,
                "style_preference": style_preference,
                "recommended_visual_elements": recommended_visual_elements,
                "content_focus": content_focus,
                "special_requirements": "；".join(special_requirements) if special_requirements else None,
            },
        }
    )


def _build_document_context(
    documents: list[UploadDocumentItem],
    *,
    model_provider: Literal["minmax", "claude", "qwen", "deepseek"],
    harness_trace: HarnessTrace,
    thinking_callback: Callable[[str], None] | None = None,
) -> tuple[str, list[dict[str, Any] | None], DocumentSummary | None]:
    logger = logging.getLogger(__name__)
    context_blocks: list[str] = []
    summary_models: list[DocumentSummary | None] = []
    summary_payloads: list[dict[str, Any] | None] = []

    for index, document in enumerate(documents):
        try:
            summary = summarize_document(
                document.document_text,
                source_file=document.document_name,
                page_count=document.page_count or None,
                model_provider=model_provider,
                thinking_callback=thinking_callback,
                harness_trace=harness_trace,
            )
            summary_models.append(summary)
            summary_payloads.append(summary.model_dump(mode="json"))
            context_blocks.append(f"【文档 {index + 1}：{document.document_name}】\n{summary.to_planner_context()}")
        except Exception as exc:
            logger.warning(
                "[document_context] 文档摘要失败(%s): %s，降级为原始文本节选",
                document.document_name,
                exc,
            )
            summary_models.append(None)
            summary_payloads.append(None)
            context_blocks.append(f"【文档 {index + 1}：{document.document_name}】\n{document.document_text[:12000]}")

    combined_summary = _merge_document_summaries(documents, summary_models)
    return "\n\n".join(context_blocks), summary_payloads, combined_summary


def _merge_content_requirements_with_document_context(content_requirements: str, document_context: str) -> str:
    """Keep user/task requirements visible when uploaded documents are summarized.

    The document summarizer produces a compact source summary for outline planning,
    but callers may also pass important generation constraints in content/constraint.
    For book PPT generation this includes lesson_plan order, toc_index descriptions,
    page ranges, style continuity, and evidence rules. These constraints must stay
    in the outline prompt instead of being replaced by the document summary.
    """

    content_requirements = str(content_requirements or "").strip()
    document_context = str(document_context or "").strip()
    if content_requirements and document_context:
        return (
            "## 生成任务与约束\n"
            f"{content_requirements}\n\n"
            "## 当前文档理解摘要\n"
            f"{document_context}"
        )
    return content_requirements or document_context


def _build_preview_markdown(
    outline: OutlinePlan,
    research_results: list[dict | None] | None = None,
    completed_slides: int = 0,
) -> str:
    research_results = research_results or []
    lines = [
        f"# {outline.title}",
        "",
        f"- 主题：{outline.topic}",
        f"- 总页数：{len(outline.slides)}",
        "",
    ]

    for index, slide in enumerate(outline.slides):
        status = "已生成" if index < completed_slides else "待生成"
        lines.append(f"## 第{index + 1}页 · {slide.topic} [{status}]")
        lines.append(f"- 布局：{slide.layout.value}")
        if slide.objective:
            lines.append(f"- 页面目标：{slide.objective}")

        research = research_results[index] if index < len(research_results) else None
        bullet_points = (research or {}).get("bullet_points") or []
        if bullet_points and index < completed_slides:
            for bullet in bullet_points[:4]:
                lines.append(f"- {bullet}")

        lines.append("")

    return "\n".join(lines).strip() + "\n"


def _serialize_sse(
    *,
    event: str | None = None,
    data: Any | None = None,
    comment: str | None = None,
) -> str:
    if comment is not None:
        return f": {comment}\n\n"

    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    lines: list[str] = []
    if event:
        lines.append(f"event: {event}")
    for line in payload.splitlines() or [""]:
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


def _iter_thinking_chunks(text: Any, chunk_size: int = 48) -> list[str]:
    raw = str(text or "")
    if not raw.strip():
        return []

    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    chunks: list[str] = []

    for paragraph in normalized.split("\n"):
        if not paragraph:
            chunks.append("\n")
            continue

        buffer = ""
        for char in paragraph:
            buffer += char
            if len(buffer) >= chunk_size:
                chunks.append(buffer)
                buffer = ""

        if buffer:
            chunks.append(buffer)
        chunks.append("\n")

    while chunks and chunks[-1] == "\n":
        chunks.pop()

    return [chunk for chunk in chunks if chunk]


SENSITIVE_THINKING_PATTERNS = (
    "豆包",
    "tavily",
    "搜图",
    "生图",
    "降级逻辑",
    "image_mode",
    "image_source",
    "你是",
    "system",
    "只输出",
    "输出要求",
    "输出json格式",
    "json 格式",
    "不要 markdown",
    "不要解释",
    "必须遵守",
    "设计规则",
    "评估维度",
    "layout 只能是",
    "layout只",
    "第 0 页必须是 cover",
    "第0页必须是cover",
    "第 1 页必须是 toc",
    "第1页必须是toc",
    "最后一页必须是 closing",
    "最后一页必须是closing",
    "image_prompt",
    "visual_mode",
    "slide_index",
    "ppt 基本信息",
    "页面列表",
    "补充修正要求",
    "用户要求我",
    "让我分析需求",
    "根据硬约束",
    "layout_wide",
    "hero-cover",
    "cover layout",
    "rounded_rectangle",
    "opacity 属性",
    "pres.layout",
    "编写代码",
    "代码：",
    "letslide",
    "const ",
    "slide.",
    "pres.",
    "addslide",
    "addshape",
    "addtext",
    "for(let",
    "foreach(",
    "function(",
    "=>",
    "the user wants me to",
    "this is page",
    "cover slide",
    "visual theme",
    "title font",
    "body font",
    "font size",
    "positioned at",
    "starting at",
    "below that",
    "i'm creating",
    "i am creating",
    "i'm placing",
    "i am placing",
    "rounded rectangle",
    "off-white color",
    "视觉母题",
    "布局类型",
    "无图片模式",
    "标题字体",
    "正文字体",
    "主色",
    "辅色",
    "点缀色",
    "禁止使用addimage",
    "用 shapes 实现",
    "visual motif",
)


def _is_sensitive_thinking_text(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if not normalized:
        return False
    if normalized.startswith("```") or normalized.endswith("```"):
        return True
    if _looks_like_internal_logic(normalized):
        return True
    if any(pattern in normalized for pattern in SENSITIVE_THINKING_PATTERNS):
        return True

    rule_like_patterns = (
        r"`?layout`?\s*只能是",
        r"第\s*0\s*页(?:[^。！？!?；;\n]{0,40})必须是\s*cover",
        r"第\s*1\s*页(?:[^。！？!?；;\n]{0,40})必须是\s*toc",
        r"最后一页(?:[^。！？!?；;\n]{0,40})必须是\s*closing",
        r"第\s*\d+\s*页(?:[^。！？!?；;\n]{0,40})必须是\s*(?:cover|toc|closing)",
        r"根据\s*规则",
        r"规则\s*[：:]",
        r"cover/toc/closing",
        r"content/two_column",
        r"只允许\s*`?auto`?",
        r"必须填写一句英文视觉描述",
        r"只输出\s*json",
        r"输出\s*json\s*格式",
        r"第\s*\d+\s*页\s*[:：]\s*(?:cover|toc|closing)",
        r"总共\s*\d+\s*页",
        r"让我规划一下结构",
        r"\btheme\s*:",
        r"\bgoal\s*:",
        r"\bmain colors?\s*:",
        r"\bfonts?\s*:",
        r"\blayout\s*:",
        r"\bimage asset\s*:",
        r"\bgenerated_image\b",
        r"\bcontent page with\b",
        r"\baddimage\b",
    )
    return any(re.search(pattern, normalized) for pattern in rule_like_patterns)


def _looks_like_internal_logic(text: str) -> bool:
    code_signals = (
        r"(?:^|[\s{(])let\s+[a-z_]",
        r"(?:^|[\s{(])const\s+[a-z_]",
        r"(?:^|[\s{(])for\s*\(",
        r"(?:^|[\s{(])if\s*\(",
        r"[a-z_]+\.[a-z_]+\s*\(",
        r"\b(?:x|y|w|h|size|opacity|rotate|fill|color)\s*:",
    )
    if any(re.search(pattern, text) for pattern in code_signals):
        return True
    punctuation_density = sum(text.count(token) for token in ("{", "}", ";", "=>"))
    return punctuation_density >= 3


def _sanitize_thinking_text(text: Any) -> str:
    raw = str(text or "")
    if not raw.strip():
        return ""

    cleaned_lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped and _is_sensitive_thinking_text(stripped):
            continue
        cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines).strip()
    cleaned = re.sub(r"```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned


class _ThinkingStreamSanitizer:
    def __init__(self) -> None:
        self.pending = ""
        self.block_current_line = False
        self.sentence_endings = "。！？!?；;"

    def reset(self) -> None:
        self.pending = ""
        self.block_current_line = False

    def _try_emit_pending(self) -> str | None:
        if self.block_current_line:
            self.pending = ""
            self.block_current_line = False
            return None
        cleaned = _sanitize_thinking_text(self.pending)
        self.pending = ""
        self.block_current_line = False
        return cleaned or None

    def feed(self, text: Any) -> list[str]:
        outputs: list[str] = []
        for char in str(text or ""):
            self.pending += char

            line_probe = self.pending.strip()
            if line_probe and _is_sensitive_thinking_text(line_probe):
                self.block_current_line = True

            if char == "\n":
                cleaned = self._try_emit_pending()
                if cleaned:
                    outputs.append(cleaned)
                continue

            if char in self.sentence_endings:
                cleaned = self._try_emit_pending()
                if cleaned:
                    outputs.append(cleaned)

        return outputs

    def flush(self) -> list[str]:
        cleaned = self._try_emit_pending()
        if not cleaned:
            return []
        return [cleaned]


async def _yield_stream_item(item: dict[str, Any], sanitizer: _ThinkingStreamSanitizer):
    event = item.get("event")
    data = item.get("data")

    if event == "thinking_safe_chunk" and isinstance(data, str):
        for chunk in _iter_thinking_chunks(data, chunk_size=3):
            yield _serialize_sse(event="thinking_chunk", data=chunk)
            await asyncio.sleep(0.02)
        return

    if event == "thinking_chunk" and isinstance(data, str):
        for chunk in sanitizer.feed(data):
            yield _serialize_sse(event=event, data=chunk)
            await asyncio.sleep(0.02)
        return

    if event == "thinking_start":
        sanitizer.reset()
    elif event == "thinking_end":
        for chunk in sanitizer.flush():
            yield _serialize_sse(event="thinking_chunk", data=chunk)
            await asyncio.sleep(0.02)

    yield _serialize_sse(event=event, data=data)


def generate_ppt_bundle(
    req: PPTGenerationRequest,
    emit: Callable[[str, Any], None] | None = None,
) -> GenerationArtifacts:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    output_filename = f"{slugify(req.topic)}.pptx"
    output_path = str((OUTPUT_ROOT / output_filename).resolve())
    biz_id = f"ppt_{int(time.time() * 1000)}"
    raw_reasoning_enabled = {"value": False}
    current_node = {"value": "初始化生成任务"}

    def emit_event(event: str, data: Any) -> None:
        if emit:
            emit(event, data)

    def emit_reasoning(text: str) -> None:
        if raw_reasoning_enabled["value"] and text:
            emit_event("thinking_chunk", text)

    def emit_safe_thinking(text: str) -> None:
        if text:
            emit_event("thinking_safe_chunk", text)

    image_source = "generate" if req.image_mode == "off" else req.image_mode
    orchestrator = _install_ppt_render_guard(
        OrchestratorAgent(
            debug_layout=req.debug_layout,
            no_research=not req.enable_web_search,
            no_images=req.image_mode == "off",
            image_source=image_source,
            model_provider=req.model_provider,
            thinking_callback=emit_reasoning,
        )
    )

    def start_step(step: int, node: str, summary: str | None = None) -> None:
        current_node["value"] = node
        emit_event("thinking_start", {"step": step, "node": node})
        if summary:
            emit_safe_thinking(summary)

    def end_step(step: int, node: str) -> None:
        emit_event("thinking_end", {"step": step, "node": node})

    try:
        emit_event("progress", {"step": 0, "total": 0, "message": "正在启动 PPT 生成任务..."})

        current_step = 1

        documents = _request_documents(req)
        content_requirements = req.content or req.constraint or ""

        if documents:
            current_step = 0  # 先走 summarizer step
            start_step(
                1,
                "理解文档内容",
                (f"正在阅读上传的 {len(documents)} 份文档，提炼章节结构和核心要点，为 PPT 大纲规划做准备。"),
            )
            try:
                if _book_ppt_direct_document_context_enabled():
                    document_context = _build_book_ppt_direct_document_context(documents)
                    combined_summary = None
                    emit_safe_thinking("电子书课件已跳过通用文档摘要，直接使用当前课时原文、目录索引和课时规划来生成大纲。")
                else:
                    document_context, _, combined_summary = _build_document_context(
                        documents,
                        model_provider=req.model_provider,
                        harness_trace=orchestrator.harness_trace,
                        thinking_callback=emit_reasoning,
                    )
                content_requirements = _merge_content_requirements_with_document_context(
                    content_requirements,
                    document_context,
                )
                if combined_summary:
                    emit_safe_thinking(f"文档解析完成，已整合 {len(documents)} 份文档，共识别 {len(combined_summary.sections)} 个主题章节，建议生成约 {combined_summary.ppt_generation_hints.suggested_total_slides} 页 PPT。")

                    suggested = combined_summary.ppt_generation_hints.suggested_total_slides
                    if _should_apply_document_slide_suggestion(req) and 4 <= suggested <= config.MAX_PPT_SLIDES:
                        req.max_slides = min(req.max_slides, suggested + 2)
                        req.min_slides = min(req.min_slides, max(4, suggested - 2))

                    hint_audience = combined_summary.ppt_generation_hints.audience
                    if hint_audience and hint_audience != "general":
                        req.target_audience = hint_audience

            except Exception as exc:
                logging.getLogger(__name__).warning(f"[generate_ppt_bundle] 文档摘要失败: {exc}，降级为直接使用原始文本")
                content_requirements = _merge_content_requirements_with_document_context(
                    content_requirements,
                    f"【文档内容摘要失败，降级处理】\n\n{_build_raw_document_context(documents)}",
                )
            end_step(1, "理解文档内容")
            current_step = 2
        else:
            current_step = 1

        start_step(
            current_step,
            "规划PPT结构",
            "正在拆解主题，先安排封面、目录和正文的整体顺序，再细化每一页要讲什么。",
        )
        outline = orchestrator.planner.plan_outline(
            req.topic,
            min_slides=req.min_slides,
            max_slides=req.max_slides,
            style=req.style,
            audience=req.target_audience,
            language=req.output_language,
            content_requirements=content_requirements,
        )
        end_step(current_step, "规划PPT结构")
        outline_topics = " / ".join(slide.topic for slide in outline.slides if slide.topic)
        if outline_topics:
            emit_safe_thinking(f"这份 PPT 目前会按这些页面往下展开：{outline_topics}")
        body_slide_count = max(len(outline.slides) - 3, 0)
        if body_slide_count:
            emit_safe_thinking(f"结构上会先用封面和目录起势，中间用 {body_slide_count} 页正文展开，最后再用总结页收束。")

        total_steps = len(outline.slides) + 3
        # +3 = 文档理解(如需) + 规划PPT结构 + 组装与校验；outline.slides = 逐页生成
        # document step 已在上面计入 current_step，故 base 里的 +3 已覆盖它
        if req.enable_web_search:
            total_steps += 1
        if req.image_mode != "off":
            total_steps += 1

        emit_event(
            "progress",
            {
                "step": current_step,
                "total": total_steps,
                "message": f"大纲规划完成，共 {len(outline.slides)} 页。",
            },
        )
        emit_event(
            "preview",
            {
                "markdown_content": _build_preview_markdown(outline, completed_slides=0),
                "completed_slides": 0,
                "total_slides": len(outline.slides),
                "current_title": "",
            },
        )

        research_results: list[dict | None] = []
        image_paths: list[str | None] = []
        current_step += 1

        if req.enable_web_search:
            start_step(
                current_step,
                "补充联网资料",
            )
            research_results = orchestrator._research_outline(outline, req.output_language)
            outline = orchestrator.planner.enrich_image_prompts(outline, research_results)
            researched_pages = sum(1 for item in research_results if item and item.get("bullet_points"))
            emit_safe_thinking(f"资料补充完成，已经为 {researched_pages} 页补到了可用信息。")
            sample_points = []
            for item in research_results:
                bullets = (item or {}).get("bullet_points") or []
                if bullets:
                    sample_points.extend(bullets[:2])
                if len(sample_points) >= 3:
                    break
            if sample_points:
                emit_safe_thinking("先整理出几条能直接用在页里的信息：" + "；".join(sample_points[:3]))
            end_step(current_step, "补充联网资料")
            emit_event(
                "progress",
                {
                    "step": current_step,
                    "total": total_steps,
                    "message": "联网资料已完成，开始准备视觉素材。",
                },
            )
            emit_event(
                "preview",
                {
                    "markdown_content": _build_preview_markdown(outline, research_results, completed_slides=0),
                    "completed_slides": 0,
                    "total_slides": len(outline.slides),
                    "current_title": "",
                },
            )
            current_step += 1

        if req.image_mode != "off":
            start_step(
                current_step,
                "准备页面配图",
                "正在为需要主视觉的页面找合适素材，顺手把整套风格往一个方向收。",
            )
            image_paths = orchestrator._fetch_assets(outline, req.output_language)
            fetched_pages = sum(1 for item in image_paths if item)
            emit_safe_thinking(f"图片素材准备得差不多了，已有 {fetched_pages} 页拿到可用配图。")
            if fetched_pages:
                emit_safe_thinking("需要主视觉的页面已经有图可用，后面会继续统一风格。")
            end_step(current_step, "准备页面配图")
            emit_event(
                "progress",
                {
                    "step": current_step,
                    "total": total_steps,
                    "message": "图片素材已准备完成。",
                },
            )
            current_step += 1

        start_step(
            current_step,
            "确定视觉主题",
        )
        theme = orchestrator.planner.decide_visual_theme(
            outline,
            style=req.style,
            audience=req.target_audience,
            language=req.output_language,
        )
        consistency_brief = orchestrator.planner._build_consistency_brief(theme)
        motif = theme.get("motif_description", "")
        if motif:
            emit_safe_thinking(f"这套 PPT 的整体视觉方向先定成了：{motif}")
        palette = theme.get("palette") or theme.get("color_palette") or []
        if isinstance(palette, list) and palette:
            emit_safe_thinking("会主要围绕这组颜色展开：" + " / ".join(str(item) for item in palette[:4]))
        end_step(current_step, "确定视觉主题")
        emit_event(
            "progress",
            {
                "step": current_step,
                "total": total_steps,
                "message": "视觉主题已确定，开始逐页生成。",
            },
        )
        current_step += 1

        slide_codes: list[str] = []
        prev_summary_lines: list[str] = []

        for index, slide in enumerate(outline.slides):
            research = research_results[index] if index < len(research_results) else None
            image_path = image_paths[index] if index < len(image_paths) else None
            node = f"生成第{index + 1}页：{slide.topic}"
            start_step(current_step, node, _build_page_thinking_summary(slide, research, image_path))
            layout_name = str(getattr(getattr(slide, "layout", None), "value", getattr(slide, "layout", "")) or "").lower()
            raw_reasoning_enabled["value"] = layout_name in {"content", "two_column"}
            if research:
                bullets = (research or {}).get("bullet_points") or []
                if bullets:
                    emit_safe_thinking("这一页会先抓这几个重点：" + "；".join(str(item) for item in bullets[:3]))
            if image_path:
                emit_safe_thinking("这页已经配好图片，排版时会把图文主次拉开。")
            layout_intent = orchestrator.planner._layout_planner.plan_layout_intent(
                slide,
                research=research,
                image_path=image_path,
            )
            try:
                code = orchestrator.planner.plan_slide(
                    slide=slide,
                    theme=theme,
                    research=research,
                    image_path=image_path,
                    layout_intent=layout_intent,
                    prev_slides_summary="\n".join(prev_summary_lines[-5:]),
                    consistency_brief=consistency_brief,
                    content_requirements=req.content or req.constraint or "",
                    audience=req.target_audience,
                    course_type=req.course or "*",
                    language=req.output_language,
                )
            finally:
                raw_reasoning_enabled["value"] = False
            emit_safe_thinking("这一页的内容方向已经定下来了，继续往后生成。")
            slide_codes.append(code)
            prev_summary_lines.append(f"第{slide.slide_index}页 [{slide.layout.value}] {slide.topic} | 标题区稳定、装饰锚点固定、卡片语言一致")
            end_step(current_step, node)
            emit_event(
                "preview",
                {
                    "markdown_content": _build_preview_markdown(
                        outline,
                        research_results=research_results,
                        completed_slides=index + 1,
                    ),
                    "completed_slides": index + 1,
                    "total_slides": len(outline.slides),
                    "current_title": slide.topic,
                },
            )
            emit_event(
                "progress",
                {
                    "step": current_step,
                    "total": total_steps,
                    "message": f"第 {index + 1} 页已生成：{slide.topic}",
                },
            )
            current_step += 1

        start_step(
            current_step,
            "组装与校验PPT",
            "正在把所有页面组装起来，再做一轮文字和版面的检查。",
        )
        result_path = orchestrator.planner.assemble_pptx(slide_codes, output_path, theme)
        content_issues = orchestrator._content_qa(result_path, outline)
        if content_issues:
            emit_safe_thinking(f"检查时发现 {len(content_issues)} 处页面内容还需要修一下，正在自动处理。")
            result_path, slide_codes, theme = orchestrator._fix_content_issues(
                content_issues,
                slide_codes,
                theme,
                outline,
                research_results,
                image_paths,
                output_path,
                content_requirements=req.content or req.constraint or "",
            )
        result_path = orchestrator._qa_loop(
            result_path,
            slide_codes,
            theme,
            outline,
            research_results,
            image_paths,
            content_requirements=req.content or req.constraint or "",
        )
        final_markdown = read_pptx(result_path).strip() or _build_preview_markdown(
            outline,
            research_results=research_results,
            completed_slides=len(outline.slides),
        )
        preview_images = _collect_or_render_preview_images(result_path)
        preview_warning = _build_preview_warning(
            Path(result_path).name,
            result_path,
            preview_images,
            visual_qa_enabled=orchestrator.evaluator.enabled,
        )
        _write_quality_report_safely(
            run_id=orchestrator.harness_trace.run_id,
            topic=req.topic,
            pptx_path=result_path,
            preview_images=preview_images,
            extracted_text=final_markdown,
            visual_eval_results=getattr(orchestrator, "_last_visual_eval_results", []),
            content_issues=content_issues,
            repair_events=getattr(orchestrator, "_last_repair_events", []),
            harness_trace=orchestrator.harness_trace,
        )
        harness_trace = orchestrator.harness_trace.to_dict()
        harness_trace_path = _write_harness_trace(Path(result_path).name, result_path, harness_trace)
        end_step(current_step, "组装与校验PPT")

        return GenerationArtifacts(
            output_path=result_path,
            output_filename=Path(result_path).name,
            markdown_content=final_markdown,
            total_slides=len(outline.slides),
            biz_id=biz_id,
            preview_images=preview_images,
            preview_warning=preview_warning,
            harness_trace=harness_trace,
            harness_trace_path=harness_trace_path,
        )
    except Exception as exc:
        step_name = current_node["value"]
        raise RuntimeError(f"{step_name} 失败: {exc}") from exc


app = FastAPI(title="PPTAgent FastAPI", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def index() -> FileResponse:
    if not FRONTEND_INDEX.exists():
        raise HTTPException(status_code=404, detail="前端页面不存在")
    return FileResponse(str(FRONTEND_INDEX))


@app.get("/health")
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/debug/harness/registry")
def harness_registry_route() -> dict[str, Any]:
    return {
        "status": "ok",
        "registry": get_learned_skill_registry(),
    }


@app.get("/debug/harness/assets")
def harness_assets_route() -> dict[str, Any]:
    return {
        "status": "ok",
        "assets": get_skill_asset_registry(),
    }


@app.get("/debug/harness/policy")
def harness_policy_route() -> dict[str, Any]:
    return {
        "status": "ok",
        "policy": get_skill_policy_map(),
    }


@app.get("/debug/harness/trace/{filename}")
def harness_trace_route(filename: str) -> dict[str, Any]:
    safe_name = Path(filename).name
    trace_path = _harness_trace_path_for_artifact(safe_name)
    if not trace_path.exists():
        raise HTTPException(status_code=404, detail="trace 不存在")
    return json.loads(trace_path.read_text(encoding="utf-8"))


@app.post("/generate_ppt")
def generate_ppt_route(req: PPTGenerationRequest) -> dict[str, Any]:
    try:
        artifacts = generate_ppt_bundle(req)
        return artifacts.to_response()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class UploadDocumentItem(BaseModel):
    document_name: str
    document_text: str
    char_count: int
    page_count: int
    summary_json: dict[str, Any] | None = None


class UploadDocumentResponse(BaseModel):
    statusCode: int = Field(default=200, description="前端 axios interceptor 依赖此字段进行 unwrap")
    document_name: str
    document_text: str
    char_count: int
    page_count: int
    summary_json: dict[str, Any] | None = None
    document_names: list[str] = Field(default_factory=list)
    document_texts: list[str] = Field(default_factory=list)
    documents: list[UploadDocumentItem] = Field(default_factory=list)


class PPTGenerationFromOutlineRequest(PPTGenerationRequest):
    outline: dict[str, Any] = Field(..., description="前端确认/修改后的大纲")
    research_results: list[dict[str, Any] | None] = Field(
        default_factory=list,
        description="兼容旧前端保留字段；继续生成时会基于确认后的大纲重新 research，不复用这里的结果",
    )


def _emit_stage_status(
    emit: Callable[[str, Any], None] | None,
    *,
    key: str,
    label: str,
    status: str,
    message: str,
    step: int,
    total: int,
) -> None:
    if not emit:
        return
    emit(
        "stage_status",
        {
            "key": key,
            "label": label,
            "status": status,
            "message": message,
            "step": step,
            "total": total,
        },
    )


def _make_search_action_event(search_event: dict[str, Any], slide_index: int) -> dict[str, Any]:
    topic = str(search_event.get("topic") or "")
    query = str(search_event.get("query") or topic)
    raw_slide_index = search_event.get("slide_index")
    if isinstance(raw_slide_index, int):
        slide_index = raw_slide_index
    snippet_count = int(search_event.get("snippet_count") or 0)
    items = search_event.get("items") if isinstance(search_event.get("items"), list) else []
    max_results = int(search_event.get("max_results") or 0)
    search_error = str(search_event.get("search_error") or "").strip()
    status = "error" if search_error else ("done" if snippet_count > 0 else "empty")
    message = search_error or (f"已检索到 {snippet_count} 条资料" + (f"（本页预算 {max_results} 条）" if max_results > 0 else "") if snippet_count > 0 else "暂未检索到可用资料")
    return {
        "slide_index": slide_index,
        "slide_title": topic,
        "query": query,
        "status": status,
        "result_count": snippet_count,
        "message": message,
        "items": items,
    }


def _build_outline_artifact(
    outline: OutlinePlan,
    *,
    research_results: list[dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    return {
        "title": outline.title,
        "topic": outline.topic,
        "total_slides": len(outline.slides),
        "outline": outline.model_dump(mode="json"),
        "research_results": research_results or [],
    }


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event and cancel_event.is_set():
        raise InterruptedError("generation_cancelled")


def _plan_outline_bundle(
    req: PPTGenerationRequest,
    *,
    emit: Callable[[str, Any], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[OutlinePlan, list[dict[str, Any] | None], list[dict[str, Any]]]:
    current_node = {"value": "初始化大纲任务"}

    def emit_event(event: str, data: Any) -> None:
        if emit:
            emit(event, data)

    def emit_reasoning(text: str) -> None:
        # 大纲阶段只展示整理后的过程摘要，不直接暴露模型原始推理或 prompt 痕迹。
        return

    def emit_safe_thinking(text: str) -> None:
        if text:
            emit_event("thinking_safe_chunk", text)

    orchestrator = _install_ppt_render_guard(
        OrchestratorAgent(
            debug_layout=req.debug_layout,
            no_research=not req.enable_web_search,
            no_images=True,
            image_source="off",
            model_provider=req.model_provider,
            thinking_callback=emit_reasoning,
        )
    )

    def start_step(step: int, node: str, summary: str | None = None) -> None:
        current_node["value"] = node
        emit_event("thinking_start", {"step": step, "node": node})
        if summary:
            emit_safe_thinking(summary)

    def end_step(step: int, node: str) -> None:
        emit_event("thinking_end", {"step": step, "node": node})

    try:
        emit_event("progress", {"step": 0, "total": 0, "message": "正在启动 PPT 大纲规划..."})

        documents = _request_documents(req)
        current_step = 1
        total_steps = 2 + (1 if documents else 0)
        content_requirements = req.content or req.constraint or ""
        _raise_if_cancelled(cancel_event)

        if documents:
            _emit_stage_status(
                emit,
                key="document",
                label="理解文档",
                status="active",
                message=f"正在解析上传的 {len(documents)} 份文档并提炼核心章节",
                step=1,
                total=total_steps,
            )
            start_step(
                1,
                "理解文档内容",
                f"正在阅读上传的 {len(documents)} 份文档，提炼章节结构和重点，为大纲规划做准备。",
            )
            try:
                if _book_ppt_direct_document_context_enabled():
                    document_context = _build_book_ppt_direct_document_context(documents)
                    combined_summary = None
                    emit_safe_thinking("电子书课件已跳过通用文档摘要，直接使用当前课时原文、目录索引和课时规划来生成大纲。")
                else:
                    document_context, _, combined_summary = _build_document_context(
                        documents,
                        model_provider=req.model_provider,
                        harness_trace=orchestrator.harness_trace,
                        thinking_callback=emit_reasoning,
                    )
                content_requirements = _merge_content_requirements_with_document_context(
                    content_requirements,
                    document_context,
                )
                if combined_summary:
                    emit_safe_thinking(f"文档解析完成，已整合 {len(documents)} 份文档，共识别 {len(combined_summary.sections)} 个主题章节，建议生成约 {combined_summary.ppt_generation_hints.suggested_total_slides} 页 PPT。")
                    suggested = combined_summary.ppt_generation_hints.suggested_total_slides
                    if _should_apply_document_slide_suggestion(req) and 4 <= suggested <= config.MAX_PPT_SLIDES:
                        req.max_slides = min(req.max_slides, suggested + 2)
                        req.min_slides = min(req.min_slides, max(4, suggested - 2))
                    if combined_summary.ppt_generation_hints.audience and combined_summary.ppt_generation_hints.audience != "general":
                        req.target_audience = combined_summary.ppt_generation_hints.audience
            except Exception as exc:
                logging.getLogger(__name__).warning(f"[_plan_outline_bundle] 文档摘要失败: {exc}，降级为直接使用原始文本")
                content_requirements = _merge_content_requirements_with_document_context(
                    content_requirements,
                    f"【文档内容摘要失败，降级处理】\n\n{_build_raw_document_context(documents)}",
                )
            end_step(1, "理解文档内容")
            _raise_if_cancelled(cancel_event)
            _emit_stage_status(
                emit,
                key="document",
                label="理解文档",
                status="done",
                message="文档理解完成",
                step=1,
                total=total_steps,
            )
            current_step = 2

        _emit_stage_status(
            emit,
            key="outline",
            label="规划大纲",
            status="active",
            message="正在规划 PPT 大纲结构",
            step=current_step,
            total=total_steps,
        )
        start_step(
            current_step,
            "规划PPT结构",
            "正在拆解主题，先安排封面、目录和正文结构，再细化每一页要讲什么。",
        )
        _raise_if_cancelled(cancel_event)
        outline = orchestrator.planner.plan_outline(
            req.topic,
            min_slides=req.min_slides,
            max_slides=req.max_slides,
            style=req.style,
            audience=req.target_audience,
            language=req.output_language,
            content_requirements=content_requirements,
        )
        end_step(current_step, "规划PPT结构")
        _raise_if_cancelled(cancel_event)
        _emit_stage_status(
            emit,
            key="outline",
            label="规划大纲",
            status="done",
            message=f"大纲规划完成，共 {len(outline.slides)} 页",
            step=current_step,
            total=total_steps,
        )

        outline_topics = " / ".join(slide.topic for slide in outline.slides if slide.topic)
        if outline_topics:
            emit_safe_thinking(f"这份 PPT 目前会按这些页面往下展开：{outline_topics}")
        if req.enable_web_search:
            emit_safe_thinking("当前先停在大纲确认阶段；等你确认或修改完大纲后，才会按这个版本去联网补资料和准备图片。")

        source_items: list[dict[str, Any]] = []
        emit_event("sources_ready", {"sources": source_items})
        artifact = _build_outline_artifact(outline, research_results=[])
        emit_event("outline_ready", artifact)
        return outline, [], []
    except Exception as exc:
        step_name = current_node["value"]
        raise RuntimeError(f"{step_name} 失败: {exc}") from exc


def _generate_from_outline_bundle(
    req: PPTGenerationFromOutlineRequest,
    *,
    emit: Callable[[str, Any], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> GenerationArtifacts:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    outline = OutlinePlan.model_validate(req.outline)
    research_results: list[dict[str, Any] | None] = []
    output_filename = f"{slugify(outline.topic or req.topic)}.pptx"
    output_path = str((OUTPUT_ROOT / output_filename).resolve())
    biz_id = f"ppt_{int(time.time() * 1000)}"
    raw_reasoning_enabled = {"value": False}
    current_node = {"value": "初始化生成任务"}

    def emit_event(event: str, data: Any) -> None:
        if emit:
            emit(event, data)

    def emit_reasoning(text: str) -> None:
        # 生成阶段统一输出整理后的讲解式过程，不直接向前端透出模型原始推理文本。
        return

    def emit_safe_thinking(text: str) -> None:
        if text:
            emit_event("thinking_safe_chunk", text)

    def on_search(search_event: dict[str, Any]) -> None:
        slide_index = search_event.get("slide_index")
        if not isinstance(slide_index, int):
            slide_index = next(
                (slide.slide_index for slide in outline.slides if slide.topic == search_event.get("topic")),
                0,
            )
        max_results = int(search_event.get("max_results") or 0)
        search_error = str(search_event.get("search_error") or "").strip()
        snippet_count = int(search_event.get("snippet_count") or 0)
        slide_title = str(search_event.get("topic") or "")

        if search_error:
            emit_safe_thinking(f"第 {slide_index} 页“{slide_title}”检索遇到问题，先降级处理：{search_error}")
        else:
            budget_text = f"，这页先抓取 {max_results} 条候选资料" if max_results > 0 else ""
            emit_safe_thinking(f"正在按确认后的大纲补第 {slide_index} 页“{slide_title}”的资料{budget_text}，当前拿到 {snippet_count} 条结果。")

        emit_event("search_action", _make_search_action_event(search_event, slide_index))

    orchestrator = _install_ppt_render_guard(
        OrchestratorAgent(
            debug_layout=req.debug_layout,
            no_research=not req.enable_web_search,
            no_images=req.image_mode == "off",
            image_source=("generate" if req.image_mode == "off" else req.image_mode),
            model_provider=req.model_provider,
            thinking_callback=emit_reasoning,
            search_callback=on_search,
        )
    )

    def start_step(step: int, node: str, summary: str | None = None) -> None:
        current_node["value"] = node
        emit_event("thinking_start", {"step": step, "node": node})
        if summary:
            emit_safe_thinking(summary)

    def end_step(step: int, node: str) -> None:
        emit_event("thinking_end", {"step": step, "node": node})

    total_steps = len(outline.slides) + 2 + (1 if req.enable_web_search else 0) + (1 if req.image_mode != "off" else 0)
    current_step = 1
    progressive_preview_runtime = get_preview_runtime_diagnostics()
    progressive_preview_enabled = bool(progressive_preview_runtime.get("soffice_found") and progressive_preview_runtime.get("pdftoppm_found"))

    try:
        emit_event("progress", {"step": 0, "total": 0, "message": "正在根据已确认大纲生成 PPT..."})

        image_paths: list[str | None] = []
        _raise_if_cancelled(cancel_event)
        if req.enable_web_search:
            _emit_stage_status(
                emit,
                key="research",
                label="补充资料",
                status="active",
                message="正在按确认后的大纲联网检索每页资料",
                step=current_step,
                total=total_steps,
            )
            start_step(current_step, "补充联网资料", "正在基于你确认后的大纲逐页补资料，后面的图片和页面内容都会按这个版本来生成。")
            if req.research_results:
                emit_safe_thinking("已忽略大纲阶段的旧资料缓存，正在按当前确认后的大纲重新检索。")
            _raise_if_cancelled(cancel_event)
            research_results = orchestrator._research_outline(outline, req.output_language)
            _raise_if_cancelled(cancel_event)
            outline = orchestrator.planner.enrich_image_prompts(outline, research_results)
            researched_pages = sum(1 for item in research_results if item and item.get("bullet_points"))
            emit_safe_thinking(f"资料补充完成，已经为 {researched_pages} 页补到了可用信息。")
            end_step(current_step, "补充联网资料")
            _emit_stage_status(
                emit,
                key="research",
                label="补充资料",
                status="done",
                message=f"资料补充完成，覆盖 {researched_pages} 页",
                step=current_step,
                total=total_steps,
            )
            current_step += 1

        _raise_if_cancelled(cancel_event)
        if req.image_mode != "off":
            _emit_stage_status(
                emit,
                key="assets",
                label="准备配图",
                status="active",
                message="正在为需要主视觉的页面准备图片素材",
                step=current_step,
                total=total_steps,
            )
            start_step(current_step, "准备页面配图", "正在为需要主视觉的页面找合适素材，顺手把整套风格统一起来。")
            _raise_if_cancelled(cancel_event)
            image_paths = orchestrator._fetch_assets(outline, req.output_language)
            _raise_if_cancelled(cancel_event)
            fetched_pages = sum(1 for item in image_paths if item)
            emit_safe_thinking(f"图片素材准备得差不多了，已有 {fetched_pages} 页拿到可用配图。")
            end_step(current_step, "准备页面配图")
            _emit_stage_status(
                emit,
                key="assets",
                label="准备配图",
                status="done",
                message=f"图片素材准备完成，覆盖 {fetched_pages} 页",
                step=current_step,
                total=total_steps,
            )
            current_step += 1

        _raise_if_cancelled(cancel_event)
        _emit_stage_status(
            emit,
            key="theme",
            label="视觉主题",
            status="active",
            message="正在确定整套 PPT 的视觉主题",
            step=current_step,
            total=total_steps,
        )
        start_step(current_step, "确定视觉主题")
        _raise_if_cancelled(cancel_event)
        theme = orchestrator.planner.decide_visual_theme(
            outline,
            style=req.style,
            audience=req.target_audience,
            language=req.output_language,
        )
        consistency_brief = orchestrator.planner._build_consistency_brief(theme)
        motif = theme.get("motif_description", "")
        if motif:
            emit_safe_thinking(f"这套 PPT 的整体视觉方向先定成了：{motif}")
        end_step(current_step, "确定视觉主题")
        _emit_stage_status(
            emit,
            key="theme",
            label="视觉主题",
            status="done",
            message="视觉主题已确定",
            step=current_step,
            total=total_steps,
        )
        emit_event("progress", {"step": current_step, "total": total_steps, "message": "视觉主题已确定，开始逐页生成。"})
        current_step += 1

        slide_codes: list[str] = []
        prev_summary_lines: list[str] = []
        for index, slide in enumerate(outline.slides):
            _raise_if_cancelled(cancel_event)
            research = research_results[index] if index < len(research_results) else None
            image_path = image_paths[index] if index < len(image_paths) else None
            node = f"生成第{index + 1}页：{slide.topic}"
            _emit_stage_status(
                emit,
                key=f"slide_{index + 1}",
                label=f"第 {index + 1} 页",
                status="active",
                message=f"正在生成：{slide.topic}",
                step=current_step,
                total=total_steps,
            )
            start_step(current_step, node, _build_page_thinking_summary(slide, research, image_path))
            layout_name = str(getattr(getattr(slide, "layout", None), "value", getattr(slide, "layout", "")) or "").lower()
            raw_reasoning_enabled["value"] = layout_name in {"content", "two_column"}
            if research:
                bullets = (research or {}).get("bullet_points") or []
                if bullets:
                    emit_safe_thinking("这一页会先抓这几个重点：" + "；".join(str(item) for item in bullets[:3]))
            layout_intent = orchestrator.planner._layout_planner.plan_layout_intent(
                slide,
                research=research,
                image_path=image_path,
            )
            try:
                code = orchestrator.planner.plan_slide(
                    slide=slide,
                    theme=theme,
                    research=research,
                    image_path=image_path,
                    layout_intent=layout_intent,
                    prev_slides_summary="\n".join(prev_summary_lines[-5:]),
                    consistency_brief=consistency_brief,
                    content_requirements=req.content or req.constraint or "",
                    audience=req.target_audience,
                    course_type=req.course or "*",
                    language=req.output_language,
                )
            finally:
                raw_reasoning_enabled["value"] = False
            _raise_if_cancelled(cancel_event)
            slide_codes.append(code)
            prev_summary_lines.append(f"第{slide.slide_index}页 [{slide.layout.value}] {slide.topic} | 标题区稳定、装饰锚点固定、卡片语言一致")
            end_step(current_step, node)
            _emit_stage_status(
                emit,
                key=f"slide_{index + 1}",
                label=f"第 {index + 1} 页",
                status="done",
                message=f"已生成：{slide.topic}",
                step=current_step,
                total=total_steps,
            )
            emit_event(
                "slide_progress",
                {
                    "completed_slides": index + 1,
                    "total_slides": len(outline.slides),
                    "current_slide_index": slide.slide_index,
                    "current_title": slide.topic,
                },
            )
            emit_event(
                "preview_ready",
                {
                    "completed_slides": index + 1,
                    "total_slides": len(outline.slides),
                    "current_slide_index": slide.slide_index,
                    "current_title": slide.topic,
                    "preview_images": [],
                },
            )
            if progressive_preview_enabled:
                try:
                    progressive_preview_images = _refresh_progressive_preview_images(
                        orchestrator=orchestrator,
                        slide_codes=slide_codes,
                        theme=theme,
                        output_path=output_path,
                    )
                    if progressive_preview_images:
                        emit_event(
                            "preview_ready",
                            {
                                "completed_slides": index + 1,
                                "total_slides": len(outline.slides),
                                "current_slide_index": slide.slide_index,
                                "current_title": slide.topic,
                                "preview_images": progressive_preview_images,
                            },
                        )
                except Exception as exc:
                    print(f"[Preview] 第 {index + 1} 页实时预览生成失败: {exc}")
            emit_event("progress", {"step": current_step, "total": total_steps, "message": f"第 {index + 1} 页已生成：{slide.topic}"})
            current_step += 1

        emit_event(
            "slide_progress",
            {
                "completed_slides": len(outline.slides),
                "total_slides": len(outline.slides),
                "current_slide_index": 0,
                "current_title": "",
            },
        )

        _emit_stage_status(
            emit,
            key="assemble",
            label="组装校验",
            status="active",
            message="正在组装并校验整份 PPT",
            step=current_step,
            total=total_steps,
        )
        start_step(current_step, "组装与校验PPT", "正在把所有页面组装起来，再做一轮文字和版面的检查。")
        _raise_if_cancelled(cancel_event)
        result_path = orchestrator.planner.assemble_pptx(slide_codes, output_path, theme)
        _raise_if_cancelled(cancel_event)
        content_issues = orchestrator._content_qa(result_path, outline)
        if content_issues:
            emit_safe_thinking(f"检查时发现 {len(content_issues)} 处页面内容还需要修一下，正在自动处理。")
            _raise_if_cancelled(cancel_event)
            result_path, slide_codes, theme = orchestrator._fix_content_issues(
                content_issues,
                slide_codes,
                theme,
                outline,
                research_results,
                image_paths,
                output_path,
                content_requirements=req.content or req.constraint or "",
            )

        def handle_revision_start(payload: dict[str, Any]) -> None:
            slide_index = int(payload.get("slide_index", 0))
            slide_title = str(payload.get("slide_title") or "")
            round_index = int(payload.get("round", 0))
            overall = payload.get("overall")
            detail = f"第 {slide_index + 1} 页正在做视觉修复"
            if slide_title:
                detail += f"：{slide_title}"
            if overall is not None:
                detail += f"（上一版评分 {float(overall):.1f}）"
            emit_safe_thinking(detail + "。先保留当前预览，修复完成后会自动替换。")
            _emit_stage_status(
                emit,
                key="assemble",
                label="组装校验",
                status="active",
                message=f"第 {round_index} 轮质检修复中：第 {slide_index + 1} 页 {slide_title}".strip(),
                step=current_step,
                total=total_steps,
            )
            emit_event(
                "slide_revision_start",
                {
                    "slide_index": slide_index,
                    "slide_title": slide_title,
                    "round": round_index,
                    "overall": overall,
                },
            )

        def handle_revision_round_complete(payload: dict[str, Any]) -> None:
            revised_output_path = str(payload.get("output_path") or result_path)
            slide_indices = [int(item) for item in payload.get("slide_indices") or []]
            preview_images_after_revision = _render_preview_images_for_pptx(revised_output_path)
            emit_event(
                "preview_ready",
                {
                    "completed_slides": len(outline.slides),
                    "total_slides": len(outline.slides),
                    "current_slide_index": 0,
                    "current_title": "",
                    "preview_images": preview_images_after_revision,
                },
            )
            for slide_index in slide_indices:
                slide_title = outline.slides[slide_index].topic if 0 <= slide_index < len(outline.slides) else ""
                emit_event(
                    "slide_revision_done",
                    {
                        "slide_index": slide_index,
                        "slide_title": slide_title,
                        "round": int(payload.get("round", 0)),
                    },
                )
            if slide_indices:
                page_labels = "、".join(f"第 {item + 1} 页" for item in slide_indices[:4])
                suffix = "等页面" if len(slide_indices) > 4 else ""
                emit_safe_thinking(f"{page_labels}{suffix} 修复完成，右侧预览已更新到最新版本。")

        def handle_revision_failed(payload: dict[str, Any]) -> None:
            slide_index = int(payload.get("slide_index", 0))
            slide_title = str(payload.get("slide_title") or "")
            emit_event(
                "slide_revision_failed",
                {
                    "slide_index": slide_index,
                    "slide_title": slide_title,
                    "round": int(payload.get("round", 0)),
                    "detail": str(payload.get("detail") or ""),
                },
            )
            emit_safe_thinking(f"第 {slide_index + 1} 页{f'“{slide_title}”' if slide_title else ''} 修复时遇到问题，先保留上一版预览。")

        result_path = orchestrator._qa_loop(
            result_path,
            slide_codes,
            theme,
            outline,
            research_results,
            image_paths,
            on_revision_start=handle_revision_start,
            on_revision_round_complete=handle_revision_round_complete,
            on_revision_failed=handle_revision_failed,
            content_requirements=req.content or req.constraint or "",
        )
        _raise_if_cancelled(cancel_event)
        final_markdown = read_pptx(result_path).strip() or _build_preview_markdown(
            outline,
            research_results=research_results,
            completed_slides=len(outline.slides),
        )
        preview_images = _collect_or_render_preview_images(result_path)
        preview_warning = _build_preview_warning(
            Path(result_path).name,
            result_path,
            preview_images,
            visual_qa_enabled=orchestrator.evaluator.enabled,
        )
        _write_quality_report_safely(
            run_id=orchestrator.harness_trace.run_id,
            topic=outline.topic or req.topic,
            pptx_path=result_path,
            preview_images=preview_images,
            extracted_text=final_markdown,
            visual_eval_results=getattr(orchestrator, "_last_visual_eval_results", []),
            content_issues=content_issues,
            repair_events=getattr(orchestrator, "_last_repair_events", []),
            harness_trace=orchestrator.harness_trace,
        )
        harness_trace = orchestrator.harness_trace.to_dict()
        harness_trace_path = _write_harness_trace(Path(result_path).name, result_path, harness_trace)
        end_step(current_step, "组装与校验PPT")
        _emit_stage_status(
            emit,
            key="assemble",
            label="组装校验",
            status="done",
            message="PPT 组装与校验完成",
            step=current_step,
            total=total_steps,
        )
        emit_event(
            "preview_ready",
            {
                "completed_slides": len(outline.slides),
                "total_slides": len(outline.slides),
                "current_slide_index": len(outline.slides),
                "current_title": outline.slides[-1].topic if outline.slides else "",
                "preview_images": preview_images,
            },
        )

        return GenerationArtifacts(
            output_path=result_path,
            output_filename=Path(result_path).name,
            markdown_content=final_markdown,
            total_slides=len(outline.slides),
            biz_id=biz_id,
            preview_images=preview_images,
            preview_warning=preview_warning,
            harness_trace=harness_trace,
            harness_trace_path=harness_trace_path,
        )
    except Exception as exc:
        step_name = current_node["value"]
        raise RuntimeError(f"{step_name} 失败: {exc}") from exc


@app.post("/upload_document")
async def upload_document_route(
    files: list[UploadFile] | None = File(default=None, description="可一次上传多个文档"),
    file: UploadFile | None = File(default=None, description="兼容旧版单文件字段"),
) -> UploadDocumentResponse:
    """
    上传一个或多个文档 → 提取文本 → 生成结构化摘要预览。
    前端可先带着 document_texts 调用 /stream_ppt_outline 生成并确认大纲，
    然后再调用 /stream_ppt_from_outline 继续生成 PPT。
    """
    upload_files = list(files or [])
    if file is not None:
        upload_files.append(file)
    if not upload_files:
        raise HTTPException(status_code=400, detail="请至少上传一个文档")
    if len(upload_files) > MAX_UPLOAD_DOCUMENTS:
        raise HTTPException(
            status_code=400,
            detail=f"最多支持上传 {MAX_UPLOAD_DOCUMENTS} 个文档",
        )

    import tempfile

    extracted_documents: list[UploadDocumentItem] = []
    allowed_suffixes = {".pdf", ".docx", ".doc", ".md", ".pptx"}

    try:
        for uploaded in upload_files:
            suffix = Path(uploaded.filename or "").suffix.lower()
            if suffix not in allowed_suffixes:
                raise HTTPException(
                    status_code=400,
                    detail=(f"不支持的文件格式：{suffix}（文件：{uploaded.filename or 'unknown'}），仅支持 PDF、Word、Markdown 和 PowerPoint 文档"),
                )

            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                content = await uploaded.read()
                tmp.write(content)
                tmp_path = tmp.name

            try:
                raw_text, _tables, page_count = extract_document_content(tmp_path)
                if not raw_text or not raw_text.strip():
                    raise HTTPException(
                        status_code=422,
                        detail=(f"文档“{uploaded.filename or 'unknown'}”内容提取失败，可能是扫描件或加密文档，请尝试导出为可复制文本后重新上传"),
                    )
                extracted_documents.append(
                    UploadDocumentItem(
                        document_name=uploaded.filename or "unknown",
                        document_text=raw_text,
                        char_count=len(raw_text),
                        page_count=page_count,
                    )
                )
            finally:
                os.unlink(tmp_path)

        _, per_document_summaries, combined_summary = _build_document_context(
            extracted_documents,
            model_provider="minmax",
            harness_trace=HarnessTrace(run_id=f"document_upload_{int(time.time() * 1000)}"),
        )

        documents = [document.model_copy(update={"summary_json": per_document_summaries[index]}) for index, document in enumerate(extracted_documents)]
        combined_summary_json = combined_summary.model_dump(mode="json") if combined_summary else None
        total_char_count = sum(document.char_count for document in documents)
        total_page_count = sum(document.page_count for document in documents)
        aggregate_name = documents[0].document_name if len(documents) == 1 else f"{len(documents)} 份文档"

        return UploadDocumentResponse(
            document_name=aggregate_name,
            document_text=(_build_raw_document_context(documents) if len(documents) > 1 else documents[0].document_text),
            char_count=total_char_count,
            page_count=total_page_count,
            summary_json=combined_summary_json or documents[0].summary_json,
            document_names=[document.document_name for document in documents],
            document_texts=[document.document_text for document in documents],
            documents=documents,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "[upload_document] 摘要生成失败: %s，继续返回纯文本",
            exc,
        )
        if not extracted_documents:
            raise HTTPException(status_code=500, detail="文档处理失败，请稍后重试") from exc

        return UploadDocumentResponse(
            document_name=(extracted_documents[0].document_name if len(extracted_documents) == 1 else f"{len(extracted_documents)} 份文档"),
            document_text=_build_raw_document_context(extracted_documents),
            char_count=sum(document.char_count for document in extracted_documents),
            page_count=sum(document.page_count for document in extracted_documents),
            summary_json=None,
            document_names=[document.document_name for document in extracted_documents],
            document_texts=[document.document_text for document in extracted_documents],
            documents=extracted_documents,
        )


@app.post("/evaluate/ppt")
@app.post("/evaluate/ppt")
def evaluate_ppt_route(req: PPTEvaluationRequest) -> dict[str, Any]:
    try:
        return evaluate_ppt_content(
            req,
            harness_trace=HarnessTrace(run_id=f"ppt_eval_{int(time.time() * 1000)}"),
        )[0].to_response()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/stream_ppt_outline")
async def stream_ppt_outline_route(req: PPTGenerationRequest, request: Request) -> StreamingResponse:
    event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
    cancel_event = threading.Event()

    def emit(event: str, data: Any) -> None:
        event_queue.put({"event": event, "data": data})

    def worker() -> None:
        try:
            _plan_outline_bundle(req, emit=emit, cancel_event=cancel_event)
            emit("done", {"status": "success"})
        except InterruptedError:
            return
        except Exception as exc:
            emit("error", {"detail": str(exc)})

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    async def event_stream():
        sanitizer = _ThinkingStreamSanitizer()
        while True:
            if await request.is_disconnected():
                cancel_event.set()
                break

            try:
                item = await asyncio.to_thread(event_queue.get, timeout=15)
            except queue.Empty:
                if not thread.is_alive():
                    yield _serialize_sse(event="error", data={"detail": "工作线程意外退出"})
                    break
                if cancel_event.is_set():
                    break
                yield _serialize_sse(comment="keepalive")
                continue

            async for payload in _yield_stream_item(item, sanitizer):
                yield payload
            if item.get("event") in {"done", "error"}:
                break

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/stream_ppt_from_outline")
async def stream_ppt_from_outline_route(
    req: PPTGenerationFromOutlineRequest,
    request: Request,
) -> StreamingResponse:
    event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
    cancel_event = threading.Event()

    def emit(event: str, data: Any) -> None:
        event_queue.put({"event": event, "data": data})

    def worker() -> None:
        try:
            artifacts = _generate_from_outline_bundle(req, emit=emit, cancel_event=cancel_event)
            emit("done", artifacts.to_response())
        except InterruptedError:
            return
        except Exception as exc:
            emit("error", {"detail": str(exc)})

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    async def event_stream():
        sanitizer = _ThinkingStreamSanitizer()
        while True:
            if await request.is_disconnected():
                cancel_event.set()
                break

            try:
                item = await asyncio.to_thread(event_queue.get, timeout=15)
            except queue.Empty:
                if not thread.is_alive():
                    yield _serialize_sse(event="error", data={"detail": "工作线程意外退出"})
                    break
                if cancel_event.is_set():
                    break
                yield _serialize_sse(comment="keepalive")
                continue

            async for payload in _yield_stream_item(item, sanitizer):
                yield payload
            if item.get("event") in {"done", "error"}:
                break

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/stream_evaluate/ppt")
async def stream_evaluate_ppt_route(req: PPTEvaluationRequest, request: Request) -> StreamingResponse:
    event_queue = stream_evaluate_ppt_content(req)

    async def event_stream():
        sanitizer = _ThinkingStreamSanitizer()
        while True:
            if await request.is_disconnected():
                break

            try:
                item = await asyncio.to_thread(event_queue.get, timeout=15)
            except queue.Empty:
                yield _serialize_sse(comment="keepalive")
                continue

            async for payload in _yield_stream_item(item, sanitizer):
                yield payload
            if item.get("event") in {"done", "error"}:
                break

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/download_ppt/{filename}")
def download_ppt_route(filename: str) -> FileResponse:
    safe_name = Path(filename).name
    target = (OUTPUT_ROOT / safe_name).resolve()

    if target.parent != OUTPUT_ROOT:
        raise HTTPException(status_code=404, detail="文件不存在")
    if not target.exists():
        raise HTTPException(status_code=404, detail="文件不存在")

    return FileResponse(
        path=str(target),
        filename=safe_name,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )


@app.get("/preview_ppt/{filename}/{image_name}")
def preview_ppt_image_route(filename: str, image_name: str) -> FileResponse:
    safe_name = Path(filename).name
    safe_image = Path(image_name).name
    preview_dir = _preview_dir_for_artifact(safe_name)
    target = (preview_dir / safe_image).resolve()

    if target.parent != preview_dir:
        raise HTTPException(status_code=404, detail="文件不存在")
    if not target.exists():
        raise HTTPException(status_code=404, detail="文件不存在")

    suffix = target.suffix.lower()
    media_type = "image/png" if suffix == ".png" else "image/jpeg"
    return FileResponse(path=str(target), media_type=media_type)
