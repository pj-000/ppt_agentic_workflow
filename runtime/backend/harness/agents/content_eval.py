from __future__ import annotations

import json
import queue
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from openai import OpenAI
from pydantic import BaseModel, Field

import config
from backend.harness.runtime import (
    HarnessTrace,
    PromptComposer,
    PromptSection,
    RepairOrchestrator,
    SkillContext,
    get_default_principle_descriptions,
    get_evaluation_metric_aliases,
    merge_prompt_sections,
)
from backend.tools.openai_compat import build_chat_completion_kwargs, stream_chat_completion_text


class PPTEvaluationRequest(BaseModel):
    course: str = Field(..., description="课程名称或 PPT 主题")
    units: str = Field(default="", description="补充单元信息")
    lessons: str = Field(default="", description="补充课时信息")
    constraint: str = Field(default="", description="附加要求")
    page_limit: int = Field(default=0, ge=0, le=50, description="PPT 页数")
    ppt_content: str = Field(..., description="PPT 文本内容或 Markdown")
    evaluation_metrics: list[str] = Field(default_factory=list, description="评估维度")
    model_type: str = Field(default="QWen", description="兼容旧前端字段")
    lang: str = Field(default="zh", description="评估语言")


@dataclass
class PPTEvaluationArtifacts:
    course: str
    units: str
    lessons: str
    constraint: str
    page_limit: int
    evaluation_metrics: list[str]
    evaluation_score: str
    principle_descriptions: list[str]
    evaluator: str
    lang: str
    harness_trace: dict[str, Any] | None = None

    def to_response(self) -> dict[str, Any]:
        return {
            "course": self.course,
            "units": self.units,
            "lessons": self.lessons,
            "constraint": self.constraint,
            "page_limit": self.page_limit,
            "evaluation_metrics": self.evaluation_metrics,
            "evaluation_score": self.evaluation_score,
            "principle_descriptions": self.principle_descriptions,
            "evaluator": self.evaluator,
            "lang": self.lang,
            "harness_trace": dict(self.harness_trace or {}),
        }


DEFAULT_PPT_EVAL_METRICS = [
    "1 指令遵循与任务完成",
    "3 内容相关性与范围控制",
    "5 基础事实准确性",
    "6 领域知识专业性",
    "10 清晰易懂与表达启发",
]

LEGACY_PPT_METRIC_ALIASES = get_evaluation_metric_aliases()
DEFAULT_PRINCIPLE_DESCRIPTIONS = get_default_principle_descriptions()

BASE_DIR = Path(__file__).resolve().parents[3]
PPT_PRINCIPLES_DIR = BASE_DIR / "principles" / "ppt"
PPT_PRINCIPLES_PATHS = {
    "zh": PPT_PRINCIPLES_DIR / "principles_zh_whiten.json",
    "en": PPT_PRINCIPLES_DIR / "principles_en_whiten.json",
}
_PRINCIPLES_CACHE: dict[str, dict[str, Any]] = {}

EVAL_NODE_LABELS = ["读取PPT内容", "按维度评分", "整理评估报告"]


def _build_content_eval_components():
    composer = PromptComposer()
    runtime = composer.runtime
    repair_orchestrator = RepairOrchestrator(
        runtime,
        run_id=uuid.uuid4().hex[:8],
        phase="evaluation-and-repair",
    )
    return (
        composer,
        runtime,
        repair_orchestrator,
        composer.load_content_evaluation_system_prompt(),
        composer.load_content_evaluation_user_prompt_template(),
    )


def _record_prompt_bundle(
    *,
    harness_trace: HarnessTrace | None,
    stage: str,
    mode: str,
    context: SkillContext,
    bundle,
    attempt: int | None = None,
    error_signature: str = "",
) -> None:
    if not harness_trace or not bundle.loaded_records:
        return
    harness_trace.record(
        stage=stage,
        payload=bundle.to_trace_payload(
            mode=mode,
            context=context,
            attempt=attempt,
            error_signature=error_signature,
        ),
    )


def load_principles(lang: str) -> dict[str, Any]:
    normalized_lang = "en" if str(lang).lower().startswith("en") else "zh"
    cached = _PRINCIPLES_CACHE.get(normalized_lang)
    if cached is not None:
        return cached

    path = PPT_PRINCIPLES_PATHS[normalized_lang]
    if not path.exists():
        _PRINCIPLES_CACHE[normalized_lang] = {}
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}

    _PRINCIPLES_CACHE[normalized_lang] = data if isinstance(data, dict) else {}
    return _PRINCIPLES_CACHE[normalized_lang]


def normalize_metric(metric: str) -> str:
    cleaned = str(metric or "").strip()
    if not cleaned:
        return cleaned
    return LEGACY_PPT_METRIC_ALIASES.get(cleaned, cleaned)


def normalize_metrics(metrics: list[str]) -> list[str]:
    normalized = [normalize_metric(metric) for metric in metrics if str(metric or "").strip()]
    return normalized or list(DEFAULT_PPT_EVAL_METRICS)


def get_metric_definition(metric: str, lang: str) -> dict[str, Any]:
    principles = load_principles(lang)
    normalized_metric = normalize_metric(metric)
    principle = principles.get(normalized_metric)
    return principle if isinstance(principle, dict) else {}


def build_principle_descriptions(metrics: list[str], lang: str) -> list[str]:
    descriptions: list[str] = []
    for metric in metrics:
        principle = get_metric_definition(metric, lang)
        descriptions.append(principle.get("description") or DEFAULT_PRINCIPLE_DESCRIPTIONS.get(metric, metric))
    return descriptions


def strip_metric_label(metric: str) -> str:
    return re.sub(r"^\s*\d+(?:\.\d+)*\s*", "", str(metric or "")).strip() or str(metric or "").strip()


def build_evaluation_prompt(
    req: PPTEvaluationRequest,
    metrics: list[str],
    template: str,
) -> str:
    composer = PromptComposer()
    metric_lines = []
    for item in metrics:
        principle = get_metric_definition(item, req.lang)
        description = principle.get("description") or DEFAULT_PRINCIPLE_DESCRIPTIONS.get(item, strip_metric_label(item))
        levels = principle.get("levels")
        levels_block = ""
        if isinstance(levels, list) and levels:
            levels_block = composer.load_content_evaluation_metric_levels_template().format(
                levels="\n  ".join(str(level) for level in levels),
            )
        line = composer.load_content_evaluation_metric_line_template().format(
            metric=item,
            description=description,
            levels_block=levels_block,
        ).rstrip()
        metric_lines.append(line)

    return (
        template
        .replace("{course}", req.course)
        .replace("{units}", req.units or "无")
        .replace("{lessons}", req.lessons or "无")
        .replace("{constraint}", req.constraint or "无")
        .replace("{page_limit}", str(req.page_limit or 0))
        .replace("{lang}", req.lang)
        .replace("{metric_lines}", "\n".join(metric_lines))
        .replace("{ppt_content}", req.ppt_content)
    )


def extract_json_block(raw: str) -> dict[str, Any]:
    cleaned = str(raw or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    candidates = [cleaned] if cleaned else []
    if "{" in cleaned and "}" in cleaned:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            fragment = cleaned[start:end + 1]
            if fragment not in candidates:
                candidates.append(fragment)

    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue

    raise ValueError(f"无法解析评估 JSON，原始内容前 200 字：{cleaned[:200]}")


def normalize_scores(data: dict[str, Any], metrics: list[str]) -> dict[str, Any]:
    composer = PromptComposer()
    default_reason = composer.load_content_evaluation_default_reason_template().strip()
    default_suggestion = composer.load_content_evaluation_default_suggestion_template().strip()
    raw_scores = data.get("detailed_scores")
    scores = raw_scores if isinstance(raw_scores, list) else []

    normalized: list[dict[str, Any]] = []
    for index, metric in enumerate(metrics):
        item = scores[index] if index < len(scores) and isinstance(scores[index], dict) else {}
        score = item.get("score", 6)
        try:
            score_int = int(round(float(score)))
        except Exception:
            score_int = 6
        score_int = max(1, min(10, score_int))

        normalized.append({
            "principle": str(item.get("principle") or metric),
            "score": score_int,
            "reason": str(item.get("reason") or default_reason),
            "optimization_suggestion": str(item.get("optimization_suggestion") or default_suggestion),
        })

    return {"detailed_scores": normalized}


def wrap_evaluation_score(data: dict[str, Any]) -> str:
    return "```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```"


def evaluate_ppt_content(
    req: PPTEvaluationRequest,
    thinking_callback: Callable[[str], None] | None = None,
    harness_trace: HarnessTrace | None = None,
) -> tuple[PPTEvaluationArtifacts, str]:
    metrics = normalize_metrics(req.evaluation_metrics)
    (
        composer,
        runtime,
        repair_orchestrator,
        system_prompt,
        user_template,
    ) = _build_content_eval_components()
    base_prompt = build_evaluation_prompt(req, metrics, user_template)
    trigger_stage = "content_evaluation"
    layout_scope = "deck-review"
    visual_mode_scope = "text_only"
    prompt_context = SkillContext(
        phase="evaluation-and-repair",
        trigger_stage=trigger_stage,
        layout_scope=layout_scope,
        visual_mode_scope=visual_mode_scope,
        course_type=req.course or "*",
        provider=config.PLANNER_MODEL,
        language=req.lang,
    )
    prevention_bundle = runtime.build_prevention_bundle(
        context=prompt_context,
        heading="## 长期技能目录（内容评估）",
        max_items=2,
    )
    _record_prompt_bundle(
        harness_trace=harness_trace,
        stage=trigger_stage,
        mode="prevention",
        context=prompt_context,
        bundle=prevention_bundle,
    )

    client = OpenAI(
        api_key=config.PLANNER_API_KEY,
        base_url=config.PLANNER_BASE_URL,
    )
    last_error = ""
    last_error_signature: str | None = None
    retry_feedback = ""
    reasoning_text = ""
    parsed: dict[str, Any] | None = None
    for attempt in range(1, 4):
        loaded_repair_memory_ids: list[str] = []
        user_prompt = merge_prompt_sections(
            PromptSection(source_type="static_prompt", identifier="content_evaluation:user", content=base_prompt),
            prevention_bundle,
        )
        if last_error_signature:
            repair_bundle = runtime.build_repair_bundle(
                context=prompt_context,
                error_signature=last_error_signature,
                max_items=1,
            )
            loaded_repair_memory_ids = list(repair_bundle.runtime_memory_ids)
            _record_prompt_bundle(
                harness_trace=harness_trace,
                stage=trigger_stage,
                mode="repair",
                context=prompt_context,
                bundle=repair_bundle,
                attempt=attempt,
                error_signature=last_error_signature,
            )
            user_prompt = merge_prompt_sections(user_prompt, repair_bundle)
        if retry_feedback:
            user_prompt = merge_prompt_sections(
                user_prompt,
                PromptSection(
                    source_type="repair_feedback",
                    identifier="content_evaluation:retry_feedback",
                    content=retry_feedback,
                ),
            )

        raw, reasoning_text = stream_chat_completion_text(
            client,
            model=config.PLANNER_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_prompt,
                },
            ],
            temperature=0.2,
            max_tokens=2200,
            on_reasoning_chunk=thinking_callback,
            **build_chat_completion_kwargs(config.PLANNER_MODEL),
        )
        try:
            parsed = normalize_scores(extract_json_block(raw), metrics)
            if last_error_signature:
                repair_instruction = repair_orchestrator.build_repair_instruction(
                    error_signature=last_error_signature,
                    error=last_error,
                    layout_scope=layout_scope,
                    visual_mode_scope=visual_mode_scope,
                )
                repair_orchestrator.remember_success(
                    trigger_stage=trigger_stage,
                    error_signature=last_error_signature,
                    error=last_error,
                    repair_instruction=repair_instruction,
                    layout_scope=layout_scope,
                    visual_mode_scope=visual_mode_scope,
                    course_type_scope=req.course or "*",
                    provider_scope=config.PLANNER_MODEL,
                    language_scope=req.lang,
                    before_pattern=raw[:400],
                    after_pattern=json.dumps(parsed, ensure_ascii=False)[:400],
                    conditions=[f"metrics={len(metrics)}"],
                )
            break
        except Exception as exc:
            for memory_id in dict.fromkeys(loaded_repair_memory_ids):
                repair_orchestrator.mark_memory_failure(memory_id)
            last_error = str(exc)
            last_error_signature = repair_orchestrator.classify_error(
                last_error,
                stage=trigger_stage,
            )
            retry_feedback = "\n".join(
                repair_orchestrator.build_retry_feedback(
                    error=last_error,
                    error_signature=last_error_signature,
                    layout_scope=layout_scope,
                    visual_mode_scope=visual_mode_scope,
                )
            )
            retry_feedback = composer.load_content_evaluation_retry_feedback_template().replace(
                "{retry_feedback_block}",
                retry_feedback,
            )
            if attempt == 3:
                raise

    return (
        PPTEvaluationArtifacts(
            course=req.course,
            units=req.units,
            lessons=req.lessons,
            constraint=req.constraint,
            page_limit=req.page_limit,
            evaluation_metrics=metrics,
            evaluation_score=wrap_evaluation_score(parsed or {"detailed_scores": []}),
            principle_descriptions=build_principle_descriptions(metrics, req.lang),
            evaluator=config.PLANNER_MODEL,
            lang=req.lang,
            harness_trace=harness_trace.to_dict() if harness_trace else None,
        ),
        reasoning_text,
    )


def stream_evaluate_ppt_content(req: PPTEvaluationRequest) -> queue.Queue:
    event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
    harness_trace = HarnessTrace(run_id=f"ppt_eval_stream_{uuid.uuid4().hex[:8]}")
    composer = PromptComposer()

    def emit(event: str, data: Any) -> None:
        payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        event_queue.put({"event": event, "data": payload})

    def worker() -> None:
        try:
            emit("thinking_start", {"step": 1, "node": EVAL_NODE_LABELS[0]})
            emit("thinking_chunk", composer.load_content_evaluation_stream_reading_template().strip())
            emit("thinking_end", {})
            emit("progress", {"step": 1, "total": 3, "message": composer.load_content_evaluation_progress_scoring_template().strip()})

            emit("thinking_start", {"step": 2, "node": EVAL_NODE_LABELS[1]})
            emit("thinking_chunk", composer.load_content_evaluation_stream_scoring_template().strip())
            artifacts, reasoning_text = evaluate_ppt_content(
                req,
                thinking_callback=lambda chunk: emit("thinking_chunk", chunk),
                harness_trace=harness_trace,
            )
            emit("thinking_end", {})
            emit("progress", {"step": 2, "total": 3, "message": composer.load_content_evaluation_progress_reporting_template().strip()})

            emit("thinking_start", {"step": 3, "node": EVAL_NODE_LABELS[2]})
            emit("thinking_chunk", composer.load_content_evaluation_stream_reporting_template().strip())
            emit("thinking_end", {})
            emit("progress", {"step": 3, "total": 3, "message": composer.load_content_evaluation_progress_done_template().strip()})
            emit("done", artifacts.to_response())
        except Exception as exc:
            emit("error", {"detail": str(exc)})

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    emit("progress", {"step": 0, "total": 3, "message": composer.load_content_evaluation_progress_start_template().strip()})
    return event_queue
