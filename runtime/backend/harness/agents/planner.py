import json
import re
import os
import sys
import uuid
import logging
from functools import cached_property
from pathlib import Path
from typing import Callable
from openai import OpenAI
from backend.harness.agents.layout_planner import LayoutPlanner
from backend.tools.pptx_skill import run_js, assert_skill_present, check_js_syntax
from backend.tools.openai_compat import build_chat_completion_kwargs, stream_chat_completion_text
from backend.models.schemas import (
    OutlinePlan,
    SlideLayoutIntent,
    SlideOutline,
    SlideLayout,
    SlideEvalResult,
    VisualMode,
    resolve_visual_mode,
)
from backend.harness.runtime import (
    HarnessTrace,
    PromptComposer,
    PromptSection,
    RepairOrchestrator,
    SkillContext,
    SkillRuntime,
    get_audience_aliases,
    get_audience_profiles,
    get_shape_value_map,
    get_supported_audiences,
    get_supported_styles,
    merge_prompt_sections,
)
import config

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
SUPPORTED_STYLES = get_supported_styles()
SUPPORTED_AUDIENCES = get_supported_audiences()
AUDIENCE_ALIASES = get_audience_aliases()
AUDIENCE_PROFILES = get_audience_profiles()


def normalize_audience(audience: str | None) -> str:
    return (audience or "").strip() or "general"


def suggest_audience_label(audience: str | None) -> str | None:
    raw = normalize_audience(audience)
    normalized = AUDIENCE_ALIASES.get(raw.lower())
    if normalized:
        return normalized
    return raw.lower() if raw.lower() in AUDIENCE_PROFILES else None


def suggest_style_label(style: str | None) -> str | None:
    raw = (style or "").strip()
    if not raw or raw.lower() == "auto":
        return None
    lowered = raw.lower()
    return lowered if lowered in SUPPORTED_STYLES else raw


SHAPE_VALUE_MAP = get_shape_value_map()


class PlannerAgent:
    """
    读取本地 Anthropic PPTX skill 文档作为 system prompt，
    让 GLM 生成 PptxGenJS 代码，通过 pptx_skill.run_js() 执行。
    """

    def __init__(
        self,
        model_provider: str = "minmax",
        thinking_callback: Callable[[str], None] | None = None,
        harness_trace: HarnessTrace | None = None,
    ):
        assert_skill_present()
        provider_settings = config.get_llm_provider_settings(model_provider)
        env_key = provider_settings["api_key"]
        self.model_provider = provider_settings["provider_id"]
        self.api_key = provider_settings["api_key"]
        self.base_url = provider_settings["base_url"]
        self.model_id = provider_settings["model_id"]
        def mask_key(value: str) -> str:
            if not value:
                return "<empty>"
            if len(value) > 16:
                return f"{value[:12]}...{value[-6:]}"
            return value
        print(
            "[Planner] LLM config: "
            f"provider={self.model_provider} | "
            f"base_url={self.base_url} | "
            f"model={self.model_id} | "
            f"api_key={mask_key(self.api_key)}"
        )
        print(
            "[Planner] Env check: "
            f"os.getenv(PLANNER_API_KEY)={mask_key(env_key)} | "
            f"selected_api_key={mask_key(self.api_key)} | "
            f"same={'yes' if env_key == self.api_key else 'no'}"
        )
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )
        self.last_reasoning = ""
        self.thinking_callback = thinking_callback
        self.harness_trace = harness_trace
        self._composer = PromptComposer()
        self._skill_runtime = self._composer.runtime
        self._repair_orchestrator = RepairOrchestrator(
            self._skill_runtime,
            run_id=uuid.uuid4().hex[:8],
        )
        self._outline_repair_orchestrator = RepairOrchestrator(
            self._skill_runtime,
            run_id=uuid.uuid4().hex[:8],
            phase="outline-planning",
        )
        self._image_prompt_repair_orchestrator = RepairOrchestrator(
            self._skill_runtime,
            run_id=uuid.uuid4().hex[:8],
            phase="visual-production",
        )
        self._layout_planner = LayoutPlanner()
        self._last_slide_contexts: list[dict] = []
        self._prompt_template_cache: dict[tuple[str, str], str] = {}

        # 保持 vendor skill 为 PPT 生成主链的一部分，本地增强规则由 harness skills 等价重组。
        self._skill_md = self._composer.load_vendor_pptx_skill()
        self._pptxgenjs_md = self._composer.load_vendor_pptxgenjs()
        self._local_rules_md = self._composer.compose_local_visual_rules()
        self._user_template = self._composer.load_deck_generation_user_prompt_template()
        self._outline_system_template = self._composer.load_outline_system_prompt()
        self._outline_user_template = self._composer.load_outline_planning_user_prompt_template()
        self._outline_default_style_text_template = self._coerce_template_text(
            self._composer.load_outline_default_style_text_template(),
            "未指定（请根据主题与受众自动决定艺术方向）",
        )
        self._outline_extra_requirements_template = self._coerce_template_text(
            self._composer.load_outline_extra_requirements_template(),
            "补充内容要求：\n{content_requirements}\n上面这段是用户明确希望 PPT 展示的重点内容、章节安排或特殊要求。规划页级大纲时必须优先吸收，不能忽略。",
        )
        self._theme_decision_system_template = self._composer.load_theme_decision_system_prompt()
        self._theme_decision_user_template = self._composer.load_theme_decision_user_prompt_template()
        self._theme_decision_retry_template = self._composer.load_theme_decision_retry_prompt_template()
        self._theme_style_auto_template = self._composer.load_theme_style_auto_template()
        self._theme_style_preference_template = self._composer.load_theme_style_preference_template()
        self._image_prompt_enrichment_template = self._composer.load_image_prompt_enrichment_system_prompt()
        self._image_prompt_enrichment_user_template = self._composer.load_image_prompt_enrichment_user_prompt_template()
        self._deck_generation_system_template = self._composer.load_deck_generation_system_template()
        self._slide_generation_system_template = self._composer.load_slide_generation_system_template()
        self._theme_section_template = self._coerce_template_text(
            self._composer.load_theme_section_template(),
            "- 主色：#{primary_color}\n"
            "- 辅色：#{secondary_color}\n"
            "- 点缀色：#{accent_color}\n"
            "- 标题字体：{header_font}\n"
            "- 正文字体：{body_font}\n"
            "- 视觉母题：{motif_description}",
        )
        self._page_info_section_template = self._coerce_template_text(
            self._composer.load_page_info_section_template(),
            "- 页码：第 {slide_index} 页\n"
            "- 布局类型：{layout}\n"
            "- 页面主题：{topic}\n"
            "- 页面目标：{objective}\n"
            "- 主视觉方式：{visual_mode}\n"
            "{image_prompt_line}",
        )
        self._audience_profile_template = self._composer.load_audience_profile_template()
        self._consistency_brief_template = self._composer.load_consistency_brief_template()
        self._outline_context_template = self._composer.load_outline_context_template()
        self._research_context_template = self._composer.load_research_context_template()
        self._image_context_template = self._composer.load_image_context_template()
        self._default_outline_context_template = self._coerce_template_text(
            self._composer.load_default_outline_context_template(),
            "无显式页级大纲，请自行规划结构。",
        )
        self._default_research_context_template = self._coerce_template_text(
            self._composer.load_default_research_context_template(),
            "无额外研究资料，请基于常识与准确性完成。",
        )
        self._default_image_context_template = self._coerce_template_text(
            self._composer.load_default_image_context_template(),
            "无可用图片。",
        )
        self._revision_issue_line_template = self._coerce_template_text(
            self._composer.load_revision_issue_line_template(),
            "- 问题：{issue}",
        )
        self._revision_suggestion_line_template = self._coerce_template_text(
            self._composer.load_revision_suggestion_line_template(),
            "- 建议：{suggestion}",
        )
        self._shape_parameter_fix_template = self._coerce_template_text(
            self._composer.load_shape_parameter_fix_template(),
            "修正所有 `addShape()` 的第一个参数：请使用合法形状值，例如 "
            "`\"rect\"`、`\"ellipse\"`、`\"line\"`、`\"roundRect\"`，"
            "或等价的 `pres.shapes.RECTANGLE/OVAL/LINE/ROUNDED_RECTANGLE`。",
        )
        self._no_image_addimage_error_template = self._coerce_template_text(
            self._composer.load_no_image_addimage_error_template(),
            "无图片模式下禁止使用 addImage；请改用 addChart/addShape/addText 做正常插图。",
        )
        self._no_image_resource_error_template = self._coerce_template_text(
            self._composer.load_no_image_resource_error_template(),
            "无图片模式下禁止引用图片资源：{marker}",
        )
        self._illegal_image_reference_error_template = self._coerce_template_text(
            self._composer.load_illegal_image_reference_error_template(),
            "检测到非法图片引用：{marker}。有图片资产时只能使用提供的本地图片路径。",
        )
        self._unauthorized_image_path_error_template = self._coerce_template_text(
            self._composer.load_unauthorized_image_path_error_template(),
            "检测到未授权图片路径：{image_path}",
        )

    def _load_skill_template(
        self,
        skill_name: str,
        template_name: str,
        *,
        fallback: str = "",
    ) -> str:
        cache_key = (skill_name, template_name)
        cached = self._prompt_template_cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            template = self._composer.load_template(skill_name, template_name)
        except FileNotFoundError:
            if not fallback:
                raise
            template = fallback

        cached = self._coerce_template_text(template, fallback) if fallback else template
        self._prompt_template_cache[cache_key] = cached
        return cached

    def _render_skill_template(
        self,
        skill_name: str,
        template_name: str,
        variables: dict[str, str | int | float],
        *,
        fallback: str = "",
    ) -> str:
        return self._render_template_string(
            self._load_skill_template(skill_name, template_name, fallback=fallback),
            variables,
        )

    def _record_prompt_bundle(
        self,
        *,
        stage: str,
        mode: str,
        context: SkillContext,
        bundle,
        attempt: int | None = None,
        error_signature: str = "",
    ) -> None:
        if not self.harness_trace or not bundle.loaded_records:
            return
        self.harness_trace.record(
            stage=stage,
            payload=bundle.to_trace_payload(
                mode=mode,
                context=context,
                attempt=attempt,
                error_signature=error_signature,
            ),
        )

    @staticmethod
    def _coerce_template_text(template: object, fallback: str) -> str:
        return template if isinstance(template, str) and template.strip() else fallback

    def _handle_reasoning_chunk(self, chunk: str) -> None:
        if not chunk:
            return
        self.last_reasoning += chunk
        if self.thinking_callback:
            self.thinking_callback(chunk)

    def _build_system_prompt(self) -> str:
        """
        system prompt = 角色指令 + 官方 SKILL.md Before Starting + Design Ideas + Typography + Avoid + 完整 pptxgenjs.md
        """
        # 从 SKILL.md 中提取 "Before Starting"（在 Design Ideas 下）到 QA 之前的内容
        skill = self._skill_md
        start_marker = "### Before Starting"
        end_marker = "## QA"
        start_idx = skill.find(start_marker)
        end_idx = skill.find(end_marker)
        if start_idx >= 0 and end_idx > start_idx:
            design_section = skill[start_idx:end_idx].strip()
        elif start_idx >= 0:
            design_section = skill[start_idx:].strip()
        else:
            # fallback: Design Ideas 之后
            idx = skill.find("## Design Ideas")
            design_section = skill[idx:].strip() if idx >= 0 else skill

        return (
            self._deck_generation_system_template
            .replace("{design_section}", design_section)
            .replace("{local_rules}", self._local_rules_md)
            .replace("{pptxgenjs}", self._pptxgenjs_md)
        )

    def _build_outline_system_prompt(self) -> str:
        return self._outline_system_template

    @staticmethod
    def _contains_cjk(text: str | None) -> bool:
        if not text:
            return False
        return any("\u4e00" <= ch <= "\u9fff" for ch in text)

    def _should_use_cjk_safe_fonts(self, outline: OutlinePlan, language: str) -> bool:
        if self._contains_cjk(language):
            return True
        if self._contains_cjk(outline.title) or self._contains_cjk(outline.topic):
            return True
        return any(
            self._contains_cjk(slide.topic) or self._contains_cjk(slide.objective)
            for slide in outline.slides
        )

    def _preferred_cjk_font(self) -> str:
        if sys.platform == "darwin":
            return "PingFang SC"
        return "Microsoft YaHei"

    def _stabilize_theme(self, theme: dict, outline: OutlinePlan, language: str) -> dict:
        normalized = dict(theme or {})
        if self._should_use_cjk_safe_fonts(outline, language):
            cjk_font = self._preferred_cjk_font()
            normalized["header_font"] = cjk_font
            normalized["body_font"] = cjk_font
            normalized["font_strategy_note"] = (
                f"本套中文 PPT 统一使用 {cjk_font}，避免导出评审图时出现英文字体对中文 fallback 不稳定。"
            )
        return normalized

    def _build_consistency_brief(self, theme: dict) -> str:
        motif = theme.get("motif_description", "")
        note = theme.get("font_strategy_note", "")
        return self._skill_runtime.render_template(
            "visual-production",
            "consistency_brief.txt",
            {
                "motif_line": f"- 当前视觉母题：{motif}" if motif else "",
                "font_line": f"- 字体策略：{note}" if note else "",
            },
        ).strip()

    def _build_outline_user_prompt(
        self,
        topic: str,
        min_slides: int = 6,
        max_slides: int = 10,
        style: str = "",
        audience: str = "general",
        language: str = "中文",
        content_requirements: str = "",
    ) -> str:
        audience = normalize_audience(audience)
        audience_ref = suggest_audience_label(audience) or "无"
        style = (style or "").strip()
        style_ref = suggest_style_label(style) or "无"
        style_text = style or self._outline_default_style_text_template
        extra = (content_requirements or "").strip()
        extra_requirements = ""
        if extra:
            extra_requirements = "\n\n" + self._outline_extra_requirements_template.format(
                content_requirements=extra,
            )
        return self._skill_runtime.render_template(
            "outline-planning",
            "outline_planning_user.txt",
            {
                "topic": topic,
                "language": language,
                "style_text": style_text,
                "style_ref": style_ref,
                "audience": audience,
                "audience_ref": audience_ref,
                "audience_profile": self._build_audience_profile(audience),
                "min_slides": str(min_slides),
                "max_slides": str(max_slides),
                "extra_requirements": extra_requirements,
            },
        )

    def _build_user_prompt(self, topic: str, min_slides: int = 6, max_slides: int = 10,
                           style: str = "", audience: str = "general",
                           language: str = "中文", research_context: str = "",
                           outline_context: str = "", image_context: str = "") -> str:
        audience = normalize_audience(audience)
        style = (style or "").strip()
        audience_profile = self._build_audience_profile(audience)
        style_text = style or self._outline_default_style_text_template
        return (self._user_template
                .replace("{topic}", topic)
                .replace("{slide_width}", str(config.SLIDE_WIDTH_INCH))
                .replace("{slide_height}", str(config.SLIDE_HEIGHT_INCH))
                .replace("{language}", language)
                .replace("{style}", style_text)
                .replace("{audience}", audience)
                .replace("{audience_profile}", audience_profile)
                .replace("{style_reference}", suggest_style_label(style) or "无")
                .replace("{audience_reference}", suggest_audience_label(audience) or "无")
                .replace("{outline_context}", outline_context.strip() or self._default_outline_context_template)
                .replace("{research_context}", research_context.strip() or self._default_research_context_template)
                .replace("{image_context}", image_context.strip() or self._default_image_context_template)
                .replace("{min_slides}", str(min_slides))
                .replace("{max_slides}", str(max_slides)))

    def plan_outline(
        self,
        topic: str,
        min_slides: int = 6,
        max_slides: int = 10,
        style: str = "",
        audience: str = "general",
        language: str = "中文",
        content_requirements: str = "",
    ) -> OutlinePlan:
        print(f"[Planner] 开始规划页级大纲：{topic}")
        system_prompt = self._build_outline_system_prompt()
        user_prompt = self._build_outline_user_prompt(
            topic,
            min_slides=min_slides,
            max_slides=max_slides,
            style=style,
            audience=audience,
            language=language,
            content_requirements=content_requirements,
        )

        last_raw = ""
        retry_feedback = ""
        last_error = ""
        last_error_signature: str | None = None
        outline_max_tokens = self._outline_max_tokens(
            min_slides=min_slides,
            max_slides=max_slides,
        )
        outline_scope = "long-deck" if max(min_slides, max_slides) >= 24 else "standard-deck"
        outline_context = SkillContext(
            phase="outline-planning",
            trigger_stage="outline_generation",
            layout_scope=outline_scope,
            visual_mode_scope="*",
            audience=normalize_audience(audience),
            provider=self.model_id,
            language=language,
        )
        prevention_bundle = self._skill_runtime.build_prevention_bundle(
            context=outline_context,
            heading="## 长期技能目录（大纲规划）",
            max_items=2,
        )
        self._record_prompt_bundle(
            stage="outline_generation",
            mode="prevention",
            context=outline_context,
            bundle=prevention_bundle,
        )
        for attempt in range(1, MAX_RETRIES + 1):
            print(f"[Planner] 大纲规划第 {attempt}/{MAX_RETRIES} 次尝试...")
            attempt_user_prompt = merge_prompt_sections(
                PromptSection(source_type="static_prompt", identifier="outline:user", content=user_prompt),
                prevention_bundle,
            )
            loaded_repair_memory_ids: list[str] = []
            if last_error_signature:
                repair_bundle = self._skill_runtime.build_repair_bundle(
                    context=outline_context,
                    error_signature=last_error_signature,
                    max_items=1,
                )
                loaded_repair_memory_ids = list(repair_bundle.runtime_memory_ids)
                self._record_prompt_bundle(
                    stage="outline_generation",
                    mode="repair",
                    context=outline_context,
                    bundle=repair_bundle,
                    attempt=attempt,
                    error_signature=last_error_signature,
                )
                attempt_user_prompt = merge_prompt_sections(attempt_user_prompt, repair_bundle)
            if retry_feedback:
                attempt_user_prompt = merge_prompt_sections(
                    attempt_user_prompt,
                    PromptSection(
                        source_type="repair_feedback",
                        identifier="outline:retry_feedback",
                        content=retry_feedback,
                    ),
                )
            self.last_reasoning = ""
            raw_content, reasoning_text = stream_chat_completion_text(
                self.client,
                model=self.model_id,
                max_tokens=outline_max_tokens,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": attempt_user_prompt},
                ],
                on_reasoning_chunk=self._handle_reasoning_chunk,
                **build_chat_completion_kwargs(self.model_id),
            )
            self.last_reasoning = reasoning_text
            last_raw = raw_content

            try:
                data = self._extract_json(last_raw)
                outline = self._parse_outline_plan(data, topic)
                if last_error_signature:
                    repair_instruction = self._outline_repair_orchestrator.build_repair_instruction(
                        error_signature=last_error_signature,
                        error=last_error,
                        layout_scope=outline_scope,
                        visual_mode_scope="*",
                    )
                    self._outline_repair_orchestrator.remember_success(
                        trigger_stage="outline_generation",
                        error_signature=last_error_signature,
                        error=last_error,
                        repair_instruction=repair_instruction,
                        layout_scope=outline_scope,
                        visual_mode_scope="*",
                        audience_scope=normalize_audience(audience),
                        provider_scope=self.model_id,
                        language_scope=language,
                        before_pattern=last_raw[:400],
                        after_pattern=json.dumps(data, ensure_ascii=False)[:400],
                        conditions=[f"requested_slides={max(min_slides, max_slides)}"],
                    )
                print(f"[Planner] 大纲规划完成，共 {len(outline.slides)} 页")
                return outline
            except Exception as e:
                err_msg = str(e)
                logger.warning(f"[Planner] 大纲规划第 {attempt} 次失败: {err_msg[:200]}")
                print(f"[Planner] 大纲规划第 {attempt} 次失败: {err_msg[:200]}")
                for memory_id in dict.fromkeys(loaded_repair_memory_ids):
                    self._outline_repair_orchestrator.mark_memory_failure(memory_id)
                last_error = err_msg
                last_error_signature = self._outline_repair_orchestrator.classify_error(
                    err_msg,
                    stage="outline_generation",
                )
                retry_feedback = self._skill_runtime.render_template(
                    "outline-planning",
                    "outline_retry_feedback.txt",
                    {
                        "error_excerpt": err_msg[:220],
                        "requested_slides": str(max(min_slides, max_slides)),
                        "compact_mode": "开启" if max(min_slides, max_slides) >= 24 else "关闭",
                    },
                )

        raise RuntimeError(f"页级大纲规划失败，最后响应前500字：{last_raw[:500]}")

    @staticmethod
    def _outline_max_tokens(*, min_slides: int, max_slides: int) -> int:
        requested = max(min_slides, max_slides)
        if requested >= 48:
            return min(config.MAX_TOKENS_PLANNER, 12288)
        if requested >= 30:
            return min(config.MAX_TOKENS_PLANNER, 8192)
        return 4096

    # ------------------------------------------------------------------ #
    #  逐页生成                                                             #
    # ------------------------------------------------------------------ #

    def decide_visual_theme(
        self,
        outline: OutlinePlan,
        style: str = "",
        audience: str = "general",
        language: str = "中文",
    ) -> dict:
        """
        一次 LLM 调用，确定整份 PPT 的视觉母题。
        返回 dict，包含 primary_color / secondary_color / accent_color /
        header_font / body_font / motif_description / pres_init_code。
        """
        style = (style or "").strip()
        style_ref = suggest_style_label(style) or "无"
        auto_style = not style or style.lower() == "auto"
        system = self._theme_decision_system_template
        slide_topics = "\n".join(
            f"- 第{s.slide_index}页 [{s.layout.value}] {s.topic}"
            for s in outline.slides
        )
        if auto_style:
            style_block = self._theme_style_auto_template
        else:
            style_block = self._theme_style_preference_template.format(
                style=style,
                style_ref=style_ref,
            )
        user = self._skill_runtime.render_template(
            "visual-production",
            "theme_decision_user.txt",
            {
                "title": outline.title,
                "audience": audience,
                "language": language,
                "style_block": style_block,
                "slide_topics": slide_topics,
            },
        )
        retry_instruction = ""
        last_error: Exception | None = None
        for attempt in range(1, 3):
            try:
                attempt_user = user
                if retry_instruction:
                    attempt_user = merge_prompt_sections(
                        PromptSection(source_type="static_prompt", identifier="theme_decision:user", content=user),
                        PromptSection(
                            source_type="repair_feedback",
                            identifier="theme_decision:retry_instruction",
                            content=retry_instruction,
                        ),
                    )
                self.last_reasoning = ""
                raw_content, reasoning_text = stream_chat_completion_text(
                    self.client,
                    model=self.model_id,
                    max_tokens=1024,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": attempt_user}],
                    on_reasoning_chunk=self._handle_reasoning_chunk,
                    **build_chat_completion_kwargs(self.model_id),
                )
                self.last_reasoning = reasoning_text
                if not raw_content or not raw_content.strip():
                    raise ValueError("LLM 返回空内容")
                data = self._extract_json(raw_content)
                if isinstance(data, dict) and "primary_color" in data:
                    data = self._stabilize_theme(data, outline, language)
                    print(f"[Planner] 视觉母题：{data.get('motif_description', '')}")
                    return data
                raise ValueError("返回结果不是预期的主题 JSON 对象")
            except Exception as e:
                last_error = e
                retry_instruction = self._theme_decision_retry_template
                logger.warning(f"[Planner] decide_visual_theme 第 {attempt} 次失败: {e}")

        if last_error is not None:
            logger.warning(f"[Planner] decide_visual_theme 失败，使用默认: {last_error}")
        return self._stabilize_theme({
            "primary_color": "1F3864",
            "secondary_color": "2E75B6",
            "accent_color": "FFFFFF",
            "header_font": "Arial Black",
            "body_font": "Calibri",
            "motif_description": "深色封面 + 浅色内容页 + 左侧色带装饰",
            "pres_init_code": 'pres.layout = "LAYOUT_WIDE";',
        }, outline, language)

    def plan_slide(
        self,
        slide: SlideOutline,
        theme: dict,
        research: dict | None,
        image_path: str | None,
        layout_intent: SlideLayoutIntent | None = None,
        prev_slides_summary: str = "",
        recent_layout_intents: list[SlideLayoutIntent] | None = None,
        revision_feedback: SlideEvalResult | None = None,
        consistency_brief: str = "",
        content_requirements: str = "",
        audience: str = "general",
        course_type: str = "*",
        language: str = "中文",
        trigger_stage: str = "slide_generation",
        forced_retry_feedback: list[str] | None = None,
        forced_error_signature: str | None = None,
        forced_error_message: str = "",
    ) -> str:
        """
        为单页生成 PptxGenJS 代码片段（不含 require / pres 初始化 / writeFile）。
        返回形如 `{ let slide = pres.addSlide(); ... }` 的代码块字符串。
        """
        system = self._build_slide_system_prompt()
        effective_visual_mode = resolve_visual_mode(slide)
        layout_intent = layout_intent or self._layout_planner.plan_layout_intent(
            slide,
            research=research,
            image_path=image_path,
        )
        slide_context = SkillContext(
            phase="visual-production",
            trigger_stage=trigger_stage,
            layout_scope=layout_intent.archetype,
            visual_mode_scope=effective_visual_mode.value,
            audience=normalize_audience(audience),
            course_type=course_type,
            provider=self.model_id,
            language=language,
        )
        prevention_bundle = self._skill_runtime.build_prevention_bundle(
            context=slide_context,
            heading="## 长期技能目录（视觉生产）",
            max_items=2,
        )
        prevention_memory_section = prevention_bundle.text
        self._record_prompt_bundle(
            stage=trigger_stage,
            mode="prevention",
            context=slide_context,
            bundle=prevention_bundle,
        )

        last_raw = ""
        retry_feedback: list[str] = list(forced_retry_feedback or [])
        last_error = forced_error_message
        last_error_signature: str | None = forced_error_signature
        loaded_repair_memory_ids: list[str] = []
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                repair_memory_section = ""
                loaded_repair_memory_ids = []
                if last_error_signature:
                    repair_bundle = self._skill_runtime.build_repair_bundle(
                        context=slide_context,
                        error_signature=last_error_signature,
                        max_items=1,
                    )
                    loaded_repair_memory_ids = list(repair_bundle.runtime_memory_ids)
                    repair_memory_section = repair_bundle.text
                    self._record_prompt_bundle(
                        stage=trigger_stage,
                        mode="repair",
                        context=slide_context,
                        bundle=repair_bundle,
                        attempt=attempt,
                        error_signature=last_error_signature,
                    )
                user = self._build_slide_user_prompt(
                    slide=slide,
                    theme=theme,
                    research=research,
                    image_path=image_path,
                    layout_intent=layout_intent,
                    prev_slides_summary=prev_slides_summary,
                    recent_layout_intents=recent_layout_intents,
                    revision_feedback=revision_feedback,
                    retry_feedback=retry_feedback,
                    consistency_brief=consistency_brief,
                    content_requirements=content_requirements,
                    prevention_memory_section=prevention_memory_section,
                    repair_memory_section=repair_memory_section,
                )
                self.last_reasoning = ""
                raw_content, reasoning_text = stream_chat_completion_text(
                    self.client,
                    model=self.model_id,
                    max_tokens=config.MAX_TOKENS_PLANNER,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    on_reasoning_chunk=self._handle_reasoning_chunk,
                    **build_chat_completion_kwargs(self.model_id),
                )
                self.last_reasoning = reasoning_text
                last_raw = raw_content
                code = self._extract_code(last_raw)
                code = self._sanitize_generated_code(code)
                self._validate_generated_slide_code(code, image_path=image_path)
                code = code.strip()
                if not code.startswith("{"):
                    code = "{\n" + code + "\n}"
                syntax_error = self._check_slide_code_syntax(code)
                if syntax_error:
                    raise ValueError(syntax_error)
                if last_error_signature:
                    repair_instruction = self._repair_orchestrator.build_repair_instruction(
                        error_signature=last_error_signature,
                        error=last_error,
                        layout_scope=layout_intent.archetype,
                        visual_mode_scope=effective_visual_mode.value,
                    )
                    self._repair_orchestrator.remember_success(
                        trigger_stage=trigger_stage,
                        error_signature=last_error_signature,
                        error=last_error,
                        repair_instruction=repair_instruction,
                        layout_scope=layout_intent.archetype,
                        visual_mode_scope=effective_visual_mode.value,
                        audience_scope=normalize_audience(audience),
                        course_type_scope=course_type,
                        provider_scope=self.model_id,
                        language_scope=language,
                        before_pattern=last_raw[:400],
                        after_pattern=code[:400],
                        conditions=[
                            f"slide_layout={slide.layout.value}",
                            f"visual_mode={effective_visual_mode.value}",
                        ],
                    )
                print(f"[Planner] 第 {slide.slide_index} 页生成成功（{len(code)} 字符）")
                return code
            except Exception as e:
                err_msg = str(e)
                print(f"[Planner] 第 {slide.slide_index} 页第 {attempt} 次失败: {err_msg[:150]}")
                logger.warning(f"[Planner] 第{slide.slide_index}页第{attempt}次失败: {e}")
                for memory_id in dict.fromkeys(loaded_repair_memory_ids):
                    self._repair_orchestrator.mark_memory_failure(memory_id)
                last_error = err_msg
                last_error_signature = self._repair_orchestrator.classify_error(
                    err_msg,
                    stage=trigger_stage,
                    image_path=image_path,
                )
                retry_feedback = self._repair_orchestrator.build_retry_feedback(
                    error=err_msg,
                    error_signature=last_error_signature,
                    layout_scope=layout_intent.archetype,
                    visual_mode_scope=effective_visual_mode.value,
                )

        raise RuntimeError(
            self._build_slide_generation_failure(
                slide_index=slide.slide_index,
                last_error=last_error,
                last_error_signature=last_error_signature,
                last_raw=last_raw,
            )
        )

    def _build_slide_user_prompt(
        self,
        slide: SlideOutline,
        theme: dict,
        research: dict | None,
        image_path: str | None,
        layout_intent: SlideLayoutIntent | None = None,
        prev_slides_summary: str = "",
        recent_layout_intents: list[SlideLayoutIntent] | None = None,
        revision_feedback: SlideEvalResult | None = None,
        retry_feedback: list[str] | None = None,
        consistency_brief: str = "",
        content_requirements: str = "",
        prevention_memory_section: str = "",
        repair_memory_section: str = "",
    ) -> str:
        effective_visual_mode = resolve_visual_mode(slide)
        layout_intent = layout_intent or self._layout_planner.plan_layout_intent(
            slide,
            research=research,
            image_path=image_path,
        )
        instruction_sections = merge_prompt_sections(
            *self._build_slide_instruction_sections(
                slide=slide,
                effective_visual_mode=effective_visual_mode,
                layout_intent=layout_intent,
                research=research,
                image_path=image_path,
                prev_slides_summary=prev_slides_summary,
                recent_layout_intents=recent_layout_intents or [],
                revision_feedback=revision_feedback,
                retry_feedback=retry_feedback,
                consistency_brief=consistency_brief,
                content_requirements=content_requirements,
                prevention_memory_section=prevention_memory_section,
                repair_memory_section=repair_memory_section,
            )
        )
        return self._skill_runtime.render_template(
            "visual-production",
            "slide_user.txt",
            {
                "theme_section": self._format_theme_section(theme),
                "page_info_section": self._format_page_info_section(slide, effective_visual_mode),
                "instruction_sections": self._format_optional_section(instruction_sections),
            },
        ).strip()

    def _build_slide_instruction_sections(
        self,
        *,
        slide: SlideOutline,
        effective_visual_mode: VisualMode,
        layout_intent: SlideLayoutIntent,
        research: dict | None,
        image_path: str | None,
        prev_slides_summary: str,
        recent_layout_intents: list[SlideLayoutIntent],
        revision_feedback: SlideEvalResult | None,
        retry_feedback: list[str] | None,
        consistency_brief: str,
        content_requirements: str,
        prevention_memory_section: str,
        repair_memory_section: str,
    ) -> list[PromptSection]:
        sections: list[PromptSection] = []

        def append_section(
            *,
            identifier: str,
            content: str,
            group: str,
            source_type: str = "static_skill_section",
            required: bool = False,
            reason: str = "",
        ) -> None:
            cleaned = (content or "").strip()
            if not cleaned:
                return
            metadata = {
                "skill": "visual-production",
                "group": group,
                "required": required,
            }
            if reason:
                metadata["reason"] = reason
            sections.append(
                PromptSection(
                    source_type=source_type,
                    identifier=identifier,
                    content=cleaned,
                    metadata=metadata,
                )
            )

        append_section(
            identifier="slide_generation:role",
            content=self._format_slide_role_section(slide),
            group="core",
            required=True,
            reason=f"layout={slide.layout.value}",
        )
        append_section(
            identifier="slide_generation:content_depth",
            content=self._format_content_depth_section(slide, layout_intent, research),
            group="core",
            required=True,
            reason=f"archetype={layout_intent.archetype}",
        )
        append_section(
            identifier="slide_generation:layout_intent",
            content=self._render_layout_intent_section(layout_intent),
            group="layout",
            required=True,
            reason=f"archetype={layout_intent.archetype}",
        )
        append_section(
            identifier="slide_generation:page_intent",
            content=self._format_page_intent_section(layout_intent),
            group="semantic",
            required=True,
            reason=f"page_intent={layout_intent.page_intent.value}",
        )
        append_section(
            identifier="slide_generation:evidence_mode",
            content=self._format_evidence_mode_section(layout_intent),
            group="semantic",
            required=True,
            reason=f"evidence_mode={layout_intent.evidence_mode.value}",
        )
        if recent_layout_intents:
            append_section(
                identifier="slide_generation:layout_rhythm",
                content=self._format_layout_rhythm_section(layout_intent, recent_layout_intents),
                group="continuity",
                reason=f"recent_layouts={len(recent_layout_intents)}",
            )
        if research and slide.layout not in {SlideLayout.COVER, SlideLayout.TOC, SlideLayout.CLOSING}:
            append_section(
                identifier="slide_generation:research",
                content=self._format_research_section(slide, research),
                group="context",
                reason="research available",
            )
        if slide.visual_mode == VisualMode.AUTO and effective_visual_mode != VisualMode.AUTO:
            append_section(
                identifier="slide_generation:visual_inference",
                content=self._format_visual_inference_section(slide, effective_visual_mode),
                group="visual",
                reason=f"resolved_visual_mode={effective_visual_mode.value}",
            )
        append_section(
            identifier="slide_generation:visual_strategy",
            content=self._format_visual_strategy_section(effective_visual_mode),
            group="visual",
            required=True,
            reason=f"visual_mode={effective_visual_mode.value}",
        )
        append_section(
            identifier="slide_generation:image",
            content=self._format_image_section(image_path),
            group="asset",
            required=True,
            reason="image asset available" if image_path else "no local image asset",
        )
        if content_requirements.strip():
            append_section(
                identifier="slide_generation:user_requirements",
                content=self._format_user_requirements_section(content_requirements),
                group="context",
                reason="user supplied extra requirements",
            )
        if prev_slides_summary.strip():
            append_section(
                identifier="slide_generation:previous_layouts",
                content=self._format_previous_layouts_section(prev_slides_summary),
                group="continuity",
                reason="recent slide summaries available",
            )
        if consistency_brief.strip():
            append_section(
                identifier="slide_generation:consistency",
                content=self._format_consistency_section(consistency_brief),
                group="continuity",
                reason="deck-level theme already decided",
            )
        if prevention_memory_section.strip():
            append_section(
                identifier="slide_generation:prevention_memory",
                content=prevention_memory_section,
                group="memory",
                source_type="prevention_bundle",
                reason="matched prevention lessons",
            )
        if repair_memory_section.strip():
            append_section(
                identifier="slide_generation:repair_memory",
                content=repair_memory_section,
                group="memory",
                source_type="repair_bundle",
                reason="matched repair lessons",
            )
        if revision_feedback:
            append_section(
                identifier="slide_generation:revision_feedback",
                content=self._format_revision_feedback_section(revision_feedback),
                group="repair",
                source_type="revision_feedback",
                reason="evaluator requested targeted fixes",
            )
        if retry_feedback:
            append_section(
                identifier="slide_generation:retry_feedback",
                content=self._format_retry_feedback_section(retry_feedback),
                group="repair",
                source_type="retry_feedback",
                reason="previous generation attempt failed",
            )
        return sections

    def plan_all_slides(
        self,
        outline: OutlinePlan,
        research_results: list[dict | None] | None,
        image_paths: list[str | None] | None,
        style: str = "",
        audience: str = "general",
        language: str = "中文",
        content_requirements: str = "",
    ) -> tuple[list[str], dict]:
        """
        逐页生成代码片段。
        返回 (slide_codes, theme)，slide_codes 与 outline.slides 一一对应。
        """
        research_results = research_results or [None] * len(outline.slides)
        image_paths = image_paths or [None] * len(outline.slides)

        print("[Planner] 确定视觉母题...")
        theme = self.decide_visual_theme(outline, style=style, audience=audience, language=language)
        consistency_brief = self._build_consistency_brief(theme)

        slide_codes: list[str] = []
        prev_summary_lines: list[str] = []
        previous_layout_intents: list[SlideLayoutIntent] = []
        self._last_slide_contexts = []

        for i, slide in enumerate(outline.slides):
            print(f"[Planner] 生成第 {slide.slide_index} 页（{slide.layout.value}: {slide.topic}）...")
            research = research_results[i] if i < len(research_results) else None
            img = image_paths[i] if i < len(image_paths) else None
            prev_summary = "\n".join(prev_summary_lines[-5:])  # 最近5页
            layout_intent = self._layout_planner.plan_layout_intent(
                slide,
                research=research,
                image_path=img,
            )
            recent_layout_intents = list(previous_layout_intents[-3:])

            code = self.plan_slide(
                slide,
                theme,
                research,
                img,
                layout_intent,
                prev_summary,
                recent_layout_intents=recent_layout_intents,
                consistency_brief=consistency_brief,
                content_requirements=content_requirements,
                audience=audience,
                language=language,
            )
            slide_codes.append(code)
            self._last_slide_contexts.append(
                {
                    "slide": slide,
                    "theme": theme,
                    "research": research,
                    "image_path": img,
                    "layout_intent": layout_intent,
                    "prev_summary": prev_summary,
                    "recent_layout_intents": recent_layout_intents,
                    "consistency_brief": consistency_brief,
                    "content_requirements": content_requirements,
                    "audience": audience,
                    "language": language,
                    "course_type": "*",
                }
            )
            prev_summary_lines.append(
                self._summarize_slide_for_layout_history(slide, layout_intent)
            )
            previous_layout_intents.append(layout_intent)

        print(f"[Planner] 逐页生成完成，共 {len(slide_codes)} 页")
        return slide_codes, theme

    def assemble_pptx(
        self,
        slide_codes: list[str],
        output_path: str,
        theme: dict,
    ) -> str:
        """
        把所有页代码片段组装成完整 JS，执行生成 .pptx。
        """
        output_path = os.path.abspath(output_path)
        full_code = self._compose_full_code(slide_codes, output_path, theme)
        self._export_generated_js(slide_codes, full_code, output_path)

        total_chars = sum(len(c) for c in slide_codes)
        print(f"[Planner] 组装完成（{len(slide_codes)} 页，{total_chars} 字符），执行生成 PPTX...")
        try:
            run_js(full_code, output_path)
        except RuntimeError as exc:
            repaired_codes = self._repair_failed_assembly(slide_codes, theme, str(exc))
            if repaired_codes is None:
                raise
            slide_codes[:] = repaired_codes
            full_code = self._compose_full_code(slide_codes, output_path, theme)
            self._export_generated_js(slide_codes, full_code, output_path)
            run_js(full_code, output_path)
        print(f"[Planner] PPTX 生成成功: {output_path}")
        return output_path

    @staticmethod
    def _generated_js_dir(output_path: str) -> Path:
        output_file = Path(output_path)
        return output_file.parent / f"{output_file.stem}_generated_js"

    def _export_generated_js(self, slide_codes: list[str], full_code: str, output_path: str) -> None:
        export_dir = self._generated_js_dir(output_path)
        export_dir.mkdir(parents=True, exist_ok=True)

        for stale in export_dir.glob("slide_*.js"):
            stale.unlink()

        (export_dir / "presentation.js").write_text(full_code, encoding="utf-8")
        for index, code in enumerate(slide_codes):
            filename = export_dir / f"slide_{index:02d}.js"
            filename.write_text(code.rstrip() + "\n", encoding="utf-8")

        print(f"[Planner] 已导出每页 JS: {export_dir}")

    def _compose_full_code(self, slide_codes: list[str], output_path: str, theme: dict) -> str:
        safe_path = output_path.replace("\\", "/")
        header_font = theme.get("header_font", "Arial Black")
        body_font = theme.get("body_font", "Calibri")
        lines = [
            'const pptxgen = require("pptxgenjs");',
            "let pres = new pptxgen();",
            'pres.layout = "LAYOUT_WIDE";',
            f'pres.theme = {{ headFontFace: "{header_font}", bodyFontFace: "{body_font}", lang: "zh-CN" }};',
            'pres.title = "Presentation";',
            f'// 视觉母题：{theme.get("motif_description", "")}',
            f'// 主色：#{theme.get("primary_color", "")}  辅色：#{theme.get("secondary_color", "")}  点缀：#{theme.get("accent_color", "")}',
            f'// 字体：{header_font} / {body_font}',
            "",
        ]
        for i, code in enumerate(slide_codes):
            lines.append(f"// ===== 第 {i} 页 =====")
            lines.append(code)
            lines.append("")
        lines.append(f'pres.writeFile({{ fileName: "{safe_path}" }});')
        full_code = "\n".join(lines)
        full_code = self._sanitize_generated_code(full_code)
        return self._enforce_theme_fonts(full_code, theme)

    def _repair_failed_assembly(
        self,
        slide_codes: list[str],
        theme: dict,
        error: str,
    ) -> list[str] | None:
        failing_index = None
        for index, code in enumerate(slide_codes):
            syntax_error = self._check_slide_code_syntax(code)
            if syntax_error:
                failing_index = index
                error = syntax_error
                break

        if failing_index is None or failing_index >= len(self._last_slide_contexts):
            return None

        context = self._last_slide_contexts[failing_index]
        slide = context["slide"]
        layout_intent = context["layout_intent"]
        visual_mode_scope = resolve_visual_mode(slide).value
        error_signature = self._repair_orchestrator.classify_error(
            error,
            stage="assembly",
            image_path=context["image_path"],
        )
        repaired_code = self.plan_slide(
            slide=slide,
            theme=context["theme"],
            research=context["research"],
            image_path=context["image_path"],
            layout_intent=layout_intent,
            prev_slides_summary=context["prev_summary"],
            recent_layout_intents=context.get("recent_layout_intents", []),
            consistency_brief=context["consistency_brief"],
            content_requirements=context["content_requirements"],
            audience=context.get("audience", "general"),
            language=context.get("language", "中文"),
            course_type=context.get("course_type", "*"),
            trigger_stage="assembly",
            forced_retry_feedback=self._repair_orchestrator.build_retry_feedback(
                error=error,
                error_signature=error_signature,
                layout_scope=layout_intent.archetype,
                visual_mode_scope=visual_mode_scope,
            ),
            forced_error_signature=error_signature,
            forced_error_message=error,
        )
        repair_instruction = self._repair_orchestrator.build_repair_instruction(
            error_signature=error_signature,
            error=error,
            layout_scope=layout_intent.archetype,
            visual_mode_scope=visual_mode_scope,
        )
        self._repair_orchestrator.remember_success(
            trigger_stage="assembly",
            error_signature=error_signature,
            error=error,
            repair_instruction=repair_instruction,
            layout_scope=layout_intent.archetype,
            visual_mode_scope=visual_mode_scope,
            audience_scope=context.get("audience", "general"),
            course_type_scope=context.get("course_type", "*"),
            provider_scope=self.model_id,
            language_scope=context.get("language", "中文"),
            before_pattern=slide_codes[failing_index][:400],
            after_pattern=repaired_code[:400],
            conditions=[f"slide_index={slide.slide_index}", f"layout={slide.layout.value}"],
        )
        updated = list(slide_codes)
        updated[failing_index] = repaired_code
        self._last_slide_contexts[failing_index]["repaired_from_assembly"] = True
        return updated

    def _build_slide_system_prompt(self) -> str:
        """单页生成的 system prompt：设计规范 + API 教程，要求只输出片段。"""
        skill = self._skill_md
        start_idx = skill.find("### Before Starting")
        end_idx = skill.find("## QA")
        if start_idx >= 0 and end_idx > start_idx:
            design_section = skill[start_idx:end_idx].strip()
        elif start_idx >= 0:
            design_section = skill[start_idx:].strip()
        else:
            idx = skill.find("## Design Ideas")
            design_section = skill[idx:].strip() if idx >= 0 else skill

        return (
            self._slide_generation_system_template
            .replace("{design_section}", design_section)
            .replace("{local_rules}", self._local_rules_md)
            .replace("{pptxgenjs}", self._pptxgenjs_md)
        )

    def plan(self, topic: str, output_path: str = None,
             min_slides: int = 6, max_slides: int = 10,
             style: str = "", audience: str = "general",
             language: str = "中文", research_context: str = "",
             content_requirements: str = "",
             outline: OutlinePlan | None = None,
             research_results: list[dict | None] | None = None,
             image_paths: list[str | None] | None = None) -> tuple[str, list[str], dict]:
        """
        逐页生成 PptxGenJS 代码并执行，直接产出 .pptx 文件。

        Returns:
            (output_path, slide_codes, theme)
        """
        if output_path is None:
            os.makedirs(config.OUTPUT_DIR, exist_ok=True)
            output_path = os.path.join(config.OUTPUT_DIR, "output.pptx")

        output_path = os.path.abspath(output_path)

        if outline is None:
            raise ValueError("plan() 需要传入 outline，请先调用 plan_outline()")

        slide_codes, theme = self.plan_all_slides(
            outline,
            research_results=research_results,
            image_paths=image_paths,
            style=style,
            audience=audience,
            language=language,
            content_requirements=content_requirements,
        )
        result_path = self.assemble_pptx(slide_codes, output_path, theme)
        return result_path, slide_codes, theme

    def enrich_image_prompts(
        self,
        outline: OutlinePlan,
        research_results: list[dict | None],
    ) -> OutlinePlan:
        """
        用 research 结果重新生成每页的 image_prompt。
        单次批量 LLM 调用，返回更新后的 OutlinePlan。
        cover/toc/closing 页保持空。
        """
        SKIP = {SlideLayout.COVER, SlideLayout.TOC, SlideLayout.CLOSING}

        # 构造输入：只包含需要生成 image_prompt 的页
        items = []
        for slide, result in zip(outline.slides, research_results or []):
            if slide.layout in SKIP:
                continue
            summary = (result or {}).get("summary", "")
            bullets = (result or {}).get("bullet_points", [])
            items.append({
                "slide_index": slide.slide_index,
                "topic": slide.topic,
                "summary": summary,
                "bullets": bullets[:3],  # 只取前3条，控制 token
            })

        if not items:
            return outline

        print(f"[Planner] 基于 research 重写 image_prompt（{len(items)} 页）...")
        system = self._image_prompt_enrichment_template
        user = self._skill_runtime.render_template(
            "visual-production",
            "image_prompt_enrichment_user.txt",
            {"items_json": json.dumps(items, ensure_ascii=False)},
        )

        trigger_stage = "image_prompt_enrichment"
        layout_scope = "multi-slide-batch"
        visual_mode_scope = "generated_image"
        prompt_context = SkillContext(
            phase="visual-production",
            trigger_stage=trigger_stage,
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
            provider=self.model_id,
        )
        prevention_bundle = self._skill_runtime.build_prevention_bundle(
            context=prompt_context,
            heading="## 长期技能目录（图像提示词增强）",
            max_items=2,
        )
        self._record_prompt_bundle(
            stage=trigger_stage,
            mode="prevention",
            context=prompt_context,
            bundle=prevention_bundle,
        )

        try:
            raw = ""
            retry_feedback = ""
            last_error = ""
            last_error_signature: str | None = None
            for attempt in range(1, MAX_RETRIES + 1):
                attempt_user = merge_prompt_sections(
                    PromptSection(source_type="static_prompt", identifier="image_prompt_enrichment:user", content=user),
                    prevention_bundle,
                )
                loaded_repair_memory_ids: list[str] = []
                if last_error_signature:
                    repair_bundle = self._skill_runtime.build_repair_bundle(
                        context=prompt_context,
                        error_signature=last_error_signature,
                        max_items=1,
                    )
                    loaded_repair_memory_ids = list(repair_bundle.runtime_memory_ids)
                    self._record_prompt_bundle(
                        stage=trigger_stage,
                        mode="repair",
                        context=prompt_context,
                        bundle=repair_bundle,
                        attempt=attempt,
                        error_signature=last_error_signature,
                    )
                    attempt_user = merge_prompt_sections(attempt_user, repair_bundle)
                if retry_feedback:
                    attempt_user = merge_prompt_sections(
                        attempt_user,
                        PromptSection(
                            source_type="repair_feedback",
                            identifier="image_prompt_enrichment:retry_feedback",
                            content=retry_feedback,
                        ),
                    )

                self.last_reasoning = ""
                raw, reasoning_text = stream_chat_completion_text(
                    self.client,
                    model=self.model_id,
                    max_tokens=2048,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": attempt_user},
                    ],
                    on_reasoning_chunk=self._handle_reasoning_chunk,
                    **build_chat_completion_kwargs(self.model_id),
                )
                self.last_reasoning = reasoning_text
                try:
                    enriched = self._extract_json(raw)
                    if not isinstance(enriched, list):
                        raise ValueError("期望 JSON 数组")
                    if last_error_signature:
                        repair_instruction = self._image_prompt_repair_orchestrator.build_repair_instruction(
                            error_signature=last_error_signature,
                            error=last_error,
                            layout_scope=layout_scope,
                            visual_mode_scope=visual_mode_scope,
                        )
                        self._image_prompt_repair_orchestrator.remember_success(
                            trigger_stage=trigger_stage,
                            error_signature=last_error_signature,
                            error=last_error,
                            repair_instruction=repair_instruction,
                            layout_scope=layout_scope,
                            visual_mode_scope=visual_mode_scope,
                            provider_scope=self.model_id,
                            before_pattern=raw[:400],
                            after_pattern=json.dumps(enriched[:3], ensure_ascii=False)[:400],
                            conditions=[f"batch_size={len(items)}"],
                        )
                    break
                except Exception as exc:
                    for memory_id in dict.fromkeys(loaded_repair_memory_ids):
                        self._image_prompt_repair_orchestrator.mark_memory_failure(memory_id)
                    last_error = str(exc)
                    last_error_signature = self._image_prompt_repair_orchestrator.classify_error(
                        last_error,
                        stage=trigger_stage,
                    )
                    retry_feedback = "\n".join(
                        self._image_prompt_repair_orchestrator.build_retry_feedback(
                            error=last_error,
                            error_signature=last_error_signature,
                            layout_scope=layout_scope,
                            visual_mode_scope=visual_mode_scope,
                        )
                    )
                    if attempt == MAX_RETRIES:
                        raise

            prompt_map = {item["slide_index"]: item.get("image_prompt", "") for item in enriched}

            updated_slides = []
            for slide in outline.slides:
                if slide.slide_index in prompt_map and prompt_map[slide.slide_index]:
                    updated_slides.append(slide.model_copy(update={"image_prompt": prompt_map[slide.slide_index]}))
                else:
                    updated_slides.append(slide)

            print(f"[Planner] image_prompt 已基于 research 更新，覆盖 {len(prompt_map)} 页")
            return outline.model_copy(update={"slides": updated_slides})

        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"[Planner] enrich_image_prompts 失败，保留原始 image_prompt: {e}")
            return outline

    def outline_to_research_slides(self, outline: OutlinePlan) -> list:
        topics = [slide.topic for slide in outline.slides if slide.topic]
        total_slides = len(outline.slides)
        return [
            self._make_research_slide(
                slide,
                total_slides=total_slides,
                previous_topic=outline.slides[index - 1].topic if index > 0 else "",
                next_topic=outline.slides[index + 1].topic if index + 1 < total_slides else "",
                outline_topics=topics,
            )
            for index, slide in enumerate(outline.slides)
        ]

    def _build_audience_profile(self, audience: str) -> str:
        reference = suggest_audience_label(audience)
        profile_line = (
            f"- 可参考的适配策略：{AUDIENCE_PROFILES[reference]}"
            if reference
            else "- 如果原始描述不属于常见标签，请根据字面含义自行判断术语深度、语气和信息密度。"
        )
        return self._skill_runtime.render_template(
            "outline-planning",
            "audience_profile.txt",
            {
                "audience": audience,
                "reference": reference or "无",
                "profile_line": profile_line,
            },
        )

    def _build_outline_context(self, outline: OutlinePlan | None) -> str:
        if not outline:
            return ""
        lines = []
        for slide in outline.slides:
            lines.append(
                f"- 第 {slide.slide_index} 页 | layout={slide.layout.value} | topic={slide.topic} | objective={slide.objective} | visual_mode={slide.visual_mode.value}"
            )
        return self._render_template_string(
            self._outline_context_template,
            {"outline_lines": "\n".join(lines)},
        ).strip()

    def _build_research_context(
        self,
        outline: OutlinePlan | None,
        research_results: list[dict | None] | None,
    ) -> str:
        if not outline or not research_results:
            return ""

        lines = []
        for slide, result in zip(outline.slides, research_results):
            if not result:
                continue
            summary = result.get("summary") or slide.topic
            bullet_points = result.get("bullet_points") or []
            key_data = result.get("key_data") or []
            lines.append(f"- 第 {slide.slide_index} 页 {slide.topic}")
            lines.append(f"  - 摘要：{summary}")
            for point in bullet_points:
                lines.append(f"  - 要点：{point}")
            for kd in key_data:
                lines.append(f"  - 核心数据（适合大字展示）：{kd}")
        if not lines:
            return ""
        return self._render_template_string(
            self._research_context_template,
            {"research_lines": "\n".join(lines)},
        ).strip()

    def _build_image_context(
        self,
        outline: OutlinePlan | None,
        image_paths: list[str | None] | None,
    ) -> str:
        if not outline or not image_paths:
            return ""

        lines = []
        has_image = False
        for slide, path in zip(outline.slides, image_paths):
            if path:
                lines.append(f"- 第 {slide.slide_index} 页（{slide.topic}）：{path}")
                has_image = True

        if not has_image:
            return ""
        return self._render_template_string(
            self._image_context_template,
            {"image_lines": "\n".join(lines)},
        ).strip()

    @staticmethod
    def _format_optional_section(text: str) -> str:
        cleaned = (text or "").strip()
        return cleaned + "\n" if cleaned else ""

    def _format_theme_section(self, theme: dict) -> str:
        return self._render_template_string(
            self._theme_section_template,
            {
                "primary_color": theme.get("primary_color", "1F3864"),
                "secondary_color": theme.get("secondary_color", "2E75B6"),
                "accent_color": theme.get("accent_color", "FFFFFF"),
                "header_font": theme.get("header_font", "Arial Black"),
                "body_font": theme.get("body_font", "Calibri"),
                "motif_description": theme.get("motif_description", ""),
            },
        ).strip()

    def _format_page_info_section(self, slide: SlideOutline, effective_visual_mode: VisualMode) -> str:
        return self._render_template_string(
            self._page_info_section_template,
            {
                "slide_index": slide.slide_index,
                "layout": slide.layout.value,
                "topic": slide.topic,
                "objective": slide.objective,
                "visual_mode": effective_visual_mode.value,
                "image_prompt_line": f"- 图片描述：{slide.image_prompt}" if slide.image_prompt else "",
            },
        ).strip()

    def _render_layout_intent_section(self, layout_intent: SlideLayoutIntent) -> str:
        return self._skill_runtime.render_template(
            "visual-production",
            "layout_intent.txt",
            {
                "archetype": layout_intent.archetype,
                "page_intent": layout_intent.page_intent.value,
                "evidence_mode": layout_intent.evidence_mode.value,
                "title_x": layout_intent.title_region.x,
                "title_y": layout_intent.title_region.y,
                "title_w": layout_intent.title_region.width,
                "title_h": layout_intent.title_region.height,
                "body_line": self._format_region_line("正文区", layout_intent.body_region),
                "visual_line": self._format_region_line("主视觉区", layout_intent.visual_region),
                "emphasis_line": self._format_region_line("强调区", layout_intent.emphasis_region),
                "text_density": layout_intent.text_density,
                "required_anchors": " / ".join(layout_intent.required_anchors) or "无",
                "forbidden_regions": " / ".join(layout_intent.forbidden_regions) or "无",
                "rationale": layout_intent.rationale or "无",
                "fallback_archetypes": " / ".join(layout_intent.fallback_archetypes) or "无",
            },
        )

    def _format_slide_role_section(self, slide: SlideOutline) -> str:
        role_map = {
            SlideLayout.COVER: (
                "建立主题、语气和首屏视觉冲击，不要堆太多正文。",
                "标题、副标题、主题意象、最少量支持信息。",
            ),
            SlideLayout.TOC: (
                "为后文建立导航，让读者知道接下来会讲什么。",
                "2-5 个真实章节名，能映射后续内容页，不要空泛目录。",
            ),
            SlideLayout.CONTENT: (
                "推进整份 deck 的论述，补充新的事实、机制、案例或结论。",
                "标题、核心结论、支持要点，避免重复上一页的开场和结构。",
            ),
            SlideLayout.TWO_COLUMN: (
                "做并列比较、双线展开或观点拆分，强化差异与关系。",
                "左右两列要有明确分工，不要把同样内容拆成两栏重复摆放。",
            ),
            SlideLayout.CLOSING: (
                "回扣前文并形成收束，可以给总结、建议、启示或下一步。",
                "总论、结论或行动建议，不要只写谢谢观看。",
            ),
        }
        role_goal, role_payload = role_map.get(
            slide.layout,
            ("承担清晰页面职责。", "输出与本页主题一致的核心信息。"),
        )
        return self._render_skill_template(
            "visual-production",
            "slide_role_section.txt",
            {
                "role_goal": role_goal,
                "role_payload": role_payload,
            },
        )

    def _format_content_depth_section(
        self,
        slide: SlideOutline,
        layout_intent: SlideLayoutIntent,
        research: dict | None,
    ) -> str:
        bullet_points = list((research or {}).get("bullet_points") or [])
        key_data = list((research or {}).get("key_data") or [])
        archetype = layout_intent.archetype
        page_intent = layout_intent.page_intent
        evidence_mode = layout_intent.evidence_mode

        if page_intent.value == "compare_options" or archetype == "comparison-split":
            depth_rules = (
                "- 左右两侧必须各有明确标题或标签，不要只摆两堆散点。\n"
                "- 每侧至少 2 条具体差异点，可用数据、条件、优缺点或适用场景支撑。\n"
                "- 页面底部或中轴应有一句对比结论，告诉读者如何判断。"
            )
        elif page_intent.value == "show_process" or archetype == "timeline-flow":
            depth_rules = (
                "- 至少组织 4 个阶段/步骤节点，每个节点都要有标题和一句解释。\n"
                "- 节点之间必须体现顺序、因果或演进，不要只是平铺概念。\n"
                "- 最后加一句阶段总结或关键转折，避免流程画完没有结论。"
            )
        elif page_intent.value == "group_insights" or archetype == "card-grid-insight":
            depth_rules = (
                "- 做成 4-6 个信息卡片，每张卡片要有短标题和 1-2 行说明。\n"
                "- 卡片之间要么是分类关系，要么是并列能力/场景，不要内容重复。\n"
                "- 至少保留一个视觉主卡或角标，避免所有卡片平均用力。"
            )
        elif page_intent.value == "case_study" or archetype == "visual-hero-split":
            depth_rules = (
                "- 图片承担主视觉，正文区只保留 3-4 条最关键要点。\n"
                "- 文字不能再堆成长段，应以结论、标签、证据三层组织。\n"
                "- 需要有一句靠近正文底部的 takeaway，总结这张图为什么重要。"
            )
        elif page_intent.value == "synthesize" or archetype == "editorial-highlight":
            depth_rules = (
                "- 先给一句强结论或判断，再用 2-3 条支撑说明展开。\n"
                "- 用引述、标签或强调框承载最值得记住的一句话。\n"
                "- 留白必须服务于重点，不要因为空而空。"
            )
        elif page_intent.value == "present_evidence" or archetype == "stat-callout":
            depth_rules = (
                "- 必须有一个主数字/主指标作为视觉重心，并配清晰单位或含义。\n"
                "- 主数字旁边补 2-4 条解释，说明来源、变化、对比或影响。\n"
                "- 如果有多个数字，必须分主次，不能做成数字墙。"
            )
        elif page_intent.value in {"show_structure", "explain_mechanism"} or archetype == "diagram-focused":
            depth_rules = (
                "- 图示区必须占主导，正文区只负责引导读者理解结构。\n"
                "- 至少给出 3 个有标签的结构节点/步骤，不要只画抽象色块。\n"
                "- 图和文之间要互相对应，避免图示一套、文字一套。"
            )
        elif page_intent.value == "explain_concept":
            depth_rules = (
                "- 先讲清核心定义或判断，再给 2-4 条解释或例子支撑。\n"
                "- 每条说明都要帮助读者理解概念边界、作用或典型场景。\n"
                "- 页面结尾最好留一句 takeaway，告诉读者这页最该记住什么。"
            )
        else:
            depth_rules = (
                "- 组织成 3-5 条高信息密度要点，每条都尽量包含事实、机制、例子或结果。\n"
                "- 先给结论，再给支持信息，不要把页面写成松散笔记。\n"
                "- 如果研究里有数据、案例或机构名，优先吸收这些具体信息。"
            )

        source_hint = "无额外 research，请基于常识补足具体性。" if not bullet_points and not key_data else (
            f"优先吸收 {len(bullet_points)} 条 research 要点"
            + (f" 和 {len(key_data)} 条关键数据" if key_data else "")
            + "，不要只保留泛泛结论。"
        )
        return self._render_skill_template(
            "visual-production",
            "content_depth_section.txt",
            {
                "depth_rules": depth_rules + f"\n- 当前页面语义：`{page_intent.value}`；优先采用 `{evidence_mode.value}` 这种证据表达方式。",
                "source_hint": source_hint,
            },
        )

    def _format_page_intent_section(self, layout_intent: SlideLayoutIntent) -> str:
        intent = layout_intent.page_intent.value
        intent_rules = self._page_intent_rules.get(
            intent, "明确这一页要完成的表达任务。"
        )
        return self._render_skill_template(
            "visual-production",
            "page_intent_section.txt",
            {
                "page_intent": intent,
                "intent_rule": intent_rules,
            },
        )

    def _format_evidence_mode_section(self, layout_intent: SlideLayoutIntent) -> str:
        evidence = layout_intent.evidence_mode.value
        evidence_rules = self._evidence_mode_rules.get(
            evidence, "选择一种清晰的证据表达方式。"
        )
        return self._render_skill_template(
            "visual-production",
            "evidence_mode_section.txt",
            {
                "evidence_mode": evidence,
                "evidence_rule": evidence_rules,
            },
        )

    @cached_property
    def _page_intent_rules(self) -> dict:
        try:
            raw = self._skill_runtime.load_reference(
                "visual-production",
                "page_intent_rules.json",
            )
            data = json.loads(raw)
        except Exception:
            data = {}
        return data.get("page_intent_rules", {}) if isinstance(data, dict) else {}

    @cached_property
    def _evidence_mode_rules(self) -> dict:
        try:
            raw = self._skill_runtime.load_reference(
                "visual-production",
                "page_intent_rules.json",
            )
            data = json.loads(raw)
        except Exception:
            data = {}
        return data.get("evidence_mode_rules", {}) if isinstance(data, dict) else {}

    @staticmethod
    def _format_region_line(label: str, region) -> str:
        if not region:
            return ""
        return f"- {label}：x={region.x}, y={region.y}, w={region.width}, h={region.height}"

    def _render_template_string(self, template: str, variables: dict[str, str | int | float]) -> str:
        rendered = template
        for key, value in variables.items():
            rendered = rendered.replace(f"{{{key}}}", str(value))
        return rendered

    def _format_research_section(self, slide: SlideOutline, research: dict | None) -> str:
        if not research or slide.layout in {SlideLayout.COVER, SlideLayout.TOC, SlideLayout.CLOSING}:
            return ""
        summary = research.get("summary", "")
        bullets = research.get("bullet_points", [])
        key_data = research.get("key_data", [])
        summary_section = ""
        if summary:
            summary_section = self._render_template_string(
                self._load_skill_template("visual-production", "research_summary_block.txt"),
                {"summary": summary},
            )
        bullets_section = ""
        if bullets:
            bullet_lines = "\n".join(f"- {item}" for item in bullets)
            rendered = self._render_template_string(
                self._load_skill_template("visual-production", "research_bullets_block.txt"),
                {"bullet_lines": bullet_lines},
            )
            bullets_section = f"\n\n{rendered}" if summary_section else rendered
        key_data_section = ""
        if key_data:
            key_data_lines = "\n".join(f"- {item}" for item in key_data)
            prefix = "\n\n" if summary_section or bullets_section else ""
            rendered = self._render_template_string(
                self._load_skill_template("visual-production", "research_key_data_block.txt"),
                {"key_data_lines": key_data_lines},
            )
            key_data_section = f"{prefix}{rendered}"
        return self._render_skill_template(
            "visual-production",
            "research_section.txt",
            {
                "summary_section": summary_section,
                "bullets_section": bullets_section,
                "key_data_section": key_data_section,
            },
        ).strip()

    def _format_visual_inference_section(self, slide: SlideOutline, effective_visual_mode: VisualMode) -> str:
        if slide.visual_mode != VisualMode.AUTO or effective_visual_mode == VisualMode.AUTO:
            return ""
        return self._render_skill_template(
            "visual-production",
            "visual_inference.txt",
            {"effective_visual_mode": effective_visual_mode.value},
        )

    def _format_visual_strategy_section(self, effective_visual_mode: VisualMode) -> str:
        template_name = "visual_strategy_auto.txt"
        if effective_visual_mode == VisualMode.JS_DIAGRAM:
            template_name = "visual_strategy_js_diagram.txt"
        elif effective_visual_mode == VisualMode.GENERATED_IMAGE:
            template_name = "visual_strategy_generated_image.txt"
        return self._load_skill_template("visual-production", template_name)

    def _format_image_section(self, image_path: str | None) -> str:
        if image_path:
            return self._render_skill_template(
                "visual-production",
                "image_section_with_asset.txt",
                {"image_path": image_path},
            )
        return self._load_skill_template("visual-production", "image_section_without_asset.txt")

    def _format_user_requirements_section(self, content_requirements: str) -> str:
        extra = (content_requirements or "").strip()
        if not extra:
            return ""
        return self._render_skill_template(
            "visual-production",
            "user_requirements.txt",
            {"content_requirements": extra},
        )

    def _format_previous_layouts_section(self, prev_slides_summary: str) -> str:
        if not prev_slides_summary:
            return ""
        return self._render_skill_template(
            "visual-production",
            "previous_layouts.txt",
            {"prev_slides_summary": prev_slides_summary},
        )

    @staticmethod
    def _summarize_slide_for_continuity(slide: SlideOutline) -> str:
        role_hint = {
            "cover": "负责建立主题与整体基调",
            "toc": "负责列出后续主要章节并建立导航",
            "content": "负责推进论述，补充新的事实、机制、案例或结论",
            "two_column": "负责并列对比或双线展开，避免与上一页重复",
            "closing": "负责总结回扣与收束，不只是致谢",
        }.get(slide.layout.value, "负责推进整份 deck")
        objective = (slide.objective or "").strip() or "补足本页信息目标"
        visual_mode = slide.visual_mode.value
        return (
            f"第{slide.slide_index}页 [{slide.layout.value}] {slide.topic} | "
            f"目标：{objective} | 角色：{role_hint} | visual_mode={visual_mode}"
        )

    @staticmethod
    def _summarize_slide_for_layout_history(
        slide: SlideOutline,
        layout_intent: SlideLayoutIntent,
    ) -> str:
        return (
            f"{PlannerAgent._summarize_slide_for_continuity(slide)} | "
            f"page_intent={layout_intent.page_intent.value} | "
            f"evidence_mode={layout_intent.evidence_mode.value} | "
            f"archetype={layout_intent.archetype} | density={layout_intent.text_density}"
        )

    def _format_consistency_section(self, consistency_brief: str) -> str:
        if not consistency_brief:
            return ""
        return self._render_skill_template(
            "visual-production",
            "consistency_section.txt",
            {"consistency_brief": consistency_brief},
        )

    def _format_layout_rhythm_section(
        self,
        layout_intent: SlideLayoutIntent,
        recent_layout_intents: list[SlideLayoutIntent],
    ) -> str:
        if not recent_layout_intents:
            return ""

        lines = [
            f"- 本页默认骨架：`{layout_intent.archetype}`。",
            f"- 本页页面语义：`{layout_intent.page_intent.value}`；优先用 `{layout_intent.evidence_mode.value}` 组织证据。",
        ]
        last_intent = recent_layout_intents[-1]
        if last_intent.archetype == layout_intent.archetype:
            lines.append(
                f"- 上一页已经使用 `{layout_intent.archetype}`；本页必须显著改变信息组织，不要复用同样的卡片分组、主视觉位置或开场结构。"
            )

        same_family_count = sum(
            1 for item in recent_layout_intents[-2:] if self._layout_family(item.archetype) == self._layout_family(layout_intent.archetype)
        )
        if same_family_count >= 2:
            lines.append(
                "- 最近两页的布局家族已经比较接近；本页至少改变一项：主视觉位置、分栏方式、强调区位置或信息分组节奏。"
            )

        if layout_intent.fallback_archetypes:
            lines.append(
                "- 如果当前骨架看起来仍然重复，可优先借用这些备选节奏："
                + " / ".join(f"`{item}`" for item in layout_intent.fallback_archetypes[:2])
                + "。"
            )

        lines.append(
            f"- 相比上一页的 `{last_intent.archetype}`，本页要形成更明显的视觉节奏变化，避免连续几页像同一模板的微调。"
        )
        return self._render_skill_template(
            "visual-production",
            "layout_rhythm_section.txt",
            {"rhythm_rules": "\n".join(lines)},
        )

    @staticmethod
    def _layout_family(archetype: str) -> str:
        mapping = {
            "single-column-card": "column",
            "two-column-balanced": "column",
            "comparison-split": "column",
            "visual-hero-split": "column",
            "stat-callout": "stat",
            "card-grid-insight": "grid",
            "timeline-flow": "flow",
            "diagram-focused": "diagram",
            "editorial-highlight": "editorial",
            "cover-hero": "cover",
            "toc-list": "toc",
            "closing-statement": "closing",
        }
        return mapping.get(archetype, archetype)

    def _format_revision_feedback_section(self, revision_feedback: SlideEvalResult | None) -> str:
        if not revision_feedback:
            return ""
        focus_block = self._build_revision_focus_block(revision_feedback)
        issues_block = "".join(
            self._render_template_string(
                self._revision_issue_line_template,
                {"issue": item},
            ).rstrip() + "\n"
            for item in revision_feedback.issues
        )
        suggestions_block = "".join(
            self._render_template_string(
                self._revision_suggestion_line_template,
                {"suggestion": item},
            ).rstrip() + "\n"
            for item in revision_feedback.suggestions
        )
        return self._render_skill_template(
            "visual-production",
            "revision_feedback.txt",
            {
                "focus_block": focus_block,
                "issues_block": issues_block,
                "suggestions_block": suggestions_block,
            },
        ).rstrip()

    def _build_revision_focus_block(self, revision_feedback: SlideEvalResult) -> str:
        issue_types = self._classify_revision_issues(revision_feedback.issues)
        if not issue_types:
            return ""
        priority = "高"
        if revision_feedback.overall >= 3.2:
            priority = "中"
        if revision_feedback.overall >= 4.0:
            priority = "低"
        issue_type_text = "、".join(issue_types[:3])
        avoid_text = self._build_revision_avoidance_hint(issue_types)
        lines = [
            f"- 修复优先级：{priority}",
            f"- 本轮问题类型：{issue_type_text}",
        ]
        if avoid_text:
            lines.append(f"- 不要重犯：{avoid_text}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _classify_revision_issues(issues: list[str]) -> list[str]:
        scores = {
            "整份连贯性": 0,
            "内容表达": 0,
            "内容结构": 0,
            "布局排版": 0,
            "视觉设计": 0,
        }
        for issue in issues:
            text = str(issue)
            if any(token in text for token in ("目录页", "收束感不足", "叙事推进不足", "内容高度重复", "开头表述相同")):
                scores["整份连贯性"] += 3
            if any(token in text for token in ("主题/目标对齐不足", "placeholder", "占位符", "重点不够聚焦", "偏题")):
                scores["内容表达"] += 3
            if any(token in text for token in ("标题可能缺失", "文字过少", "信息密度过高", "内容为空")):
                scores["内容结构"] += 2
            if any(token in text for token in ("重叠", "裁切", "边距", "layout", "排版")):
                scores["布局排版"] += 2
            if any(token in text for token in ("低对比", "视觉重心", "配色", "设计", "design")):
                scores["视觉设计"] += 2

        ordered = sorted(
            (label for label, score in scores.items() if score > 0),
            key=lambda label: (scores[label], {
                "整份连贯性": 5,
                "内容表达": 4,
                "内容结构": 3,
                "布局排版": 2,
                "视觉设计": 1,
            }[label]),
            reverse=True,
        )
        return ordered

    @staticmethod
    def _build_revision_avoidance_hint(issue_types: list[str]) -> str:
        hints: list[str] = []
        if "内容结构" in issue_types:
            hints.append("不要只改样式而不补信息层级")
        if "整份连贯性" in issue_types:
            hints.append("不要重复上一页的开场、章节名或总结句")
        if "布局排版" in issue_types:
            hints.append("不要继续堆叠元素或压缩留白")
        if "视觉设计" in issue_types:
            hints.append("不要靠装饰堆砌掩盖信息弱点")
        if "内容表达" in issue_types:
            hints.append("不要保留空话、模板文案或偏题表述")
        return "；".join(hints[:2])

    def _format_retry_feedback_section(self, retry_feedback: list[str] | None) -> str:
        if not retry_feedback:
            return ""
        retry_feedback_block = "\n".join(f"- {item}" for item in retry_feedback)
        return self._render_skill_template(
            "visual-production",
            "retry_feedback.txt",
            {"retry_feedback_block": retry_feedback_block},
        )

    @staticmethod
    def _wrap_slide_code_for_syntax_check(code: str) -> str:
        wrapped = code.strip()
        if not wrapped.startswith("{"):
            wrapped = "{\n" + wrapped + "\n}"
        return "\n".join(
            [
                'const pptxgen = require("pptxgenjs");',
                "let pres = new pptxgen();",
                'pres.layout = "LAYOUT_WIDE";',
                wrapped,
            ]
        )

    def _check_slide_code_syntax(self, code: str) -> str:
        valid, stderr = check_js_syntax(self._wrap_slide_code_for_syntax_check(code))
        if valid:
            return ""
        excerpt = self._repair_orchestrator.extract_error_excerpt(stderr)
        return f"单页 JS 语法检查失败：{excerpt}"

    def _build_slide_generation_failure(
        self,
        *,
        slide_index: int,
        last_error: str,
        last_error_signature: str | None,
        last_raw: str,
    ) -> str:
        error_excerpt = self._condense_error_excerpt(last_error)
        response_excerpt = self._summarize_failed_llm_response(last_raw)
        parts = [f"第{slide_index}页生成失败，自动修复重试 {MAX_RETRIES} 次后仍未通过校验。"]
        if last_error_signature:
            parts.append(f"错误类型：{last_error_signature}。")
        if error_excerpt:
            parts.append(f"最后错误：{error_excerpt}")
        if response_excerpt:
            parts.append(f"最近一次模型输出摘要：{response_excerpt}")
        return " ".join(parts)

    @staticmethod
    def _condense_error_excerpt(error: str, limit: int = 220) -> str:
        cleaned = re.sub(r"\s+", " ", str(error or "")).strip()
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 1] + "…"

    @staticmethod
    def _summarize_failed_llm_response(raw: str, limit: int = 180) -> str:
        cleaned = str(raw or "").strip()
        if not cleaned:
            return ""
        cleaned = re.sub(r"```(?:javascript|js)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.replace("```", "")
        cleaned = cleaned.replace("<code>", "").replace("</code>", "")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 1] + "…"

    def _parse_outline_plan(self, data: dict, topic: str) -> OutlinePlan:
        slides = data.get("slides")
        if not isinstance(slides, list):
            raise ValueError("大纲 JSON 缺少 slides 数组")

        normalized_slides = []
        for idx, slide in enumerate(slides):
            if not isinstance(slide, dict):
                raise ValueError("slides 内元素必须是对象")
            normalized_slides.append(
                {
                    "slide_index": slide.get("slide_index", idx),
                    "layout": slide.get("layout") or "content",
                    "topic": slide.get("topic") or f"{topic} - 第{idx + 1}页",
                    "objective": slide.get("objective", ""),
                    "image_prompt": slide.get("image_prompt") or None,
                    "visual_mode": slide.get("visual_mode") or "auto",
                }
            )

        normalized = {
            "title": data.get("title") or topic,
            "topic": data.get("topic") or topic,
            "slides": normalized_slides,
        }
        outline = OutlinePlan.model_validate(normalized)
        self._validate_outline_structure(outline)
        return outline

    def _validate_outline_structure(self, outline: OutlinePlan) -> None:
        if not outline.slides:
            raise ValueError("大纲不能为空")
        first = outline.slides[0].layout.value
        second = outline.slides[1].layout.value if len(outline.slides) > 1 else None
        last = outline.slides[-1].layout.value
        if first != "cover":
            raise ValueError("第 0 页必须是 cover")
        if second != "toc":
            raise ValueError("第 1 页必须是 toc")
        if last != "closing":
            raise ValueError("最后一页必须是 closing")
        for idx, slide in enumerate(outline.slides):
            if slide.slide_index != idx:
                raise ValueError("slide_index 必须从 0 开始连续递增")
        middle_layouts = [s.layout for s in outline.slides[2:-1]]
        if middle_layouts and not any(layout in {SlideLayout.CONTENT, SlideLayout.TWO_COLUMN} for layout in middle_layouts):
            raise ValueError("中间页至少需要一个 content 或 two_column 布局")

    def _make_research_slide(
        self,
        slide: SlideOutline,
        *,
        total_slides: int,
        previous_topic: str = "",
        next_topic: str = "",
        outline_topics: list[str] | None = None,
    ):
        from backend.models.schemas import SlideSpec, TextElement

        outline_topics = outline_topics or []
        current_index = max(0, slide.slide_index)
        window_start = max(0, current_index - 2)
        window_end = min(len(outline_topics), current_index + 3)
        local_path = " -> ".join(item for item in outline_topics[window_start:window_end] if item)

        context_lines = [
            f"页面位置：第 {slide.slide_index + 1} 页 / 共 {total_slides} 页",
        ]
        if previous_topic:
            context_lines.append(f"上一页主题：{previous_topic}")
        if next_topic:
            context_lines.append(f"下一页主题：{next_topic}")
        if local_path:
            context_lines.append(f"邻近页面脉络：{local_path}")

        speaker_notes_parts = [slide.objective.strip()] if slide.objective and slide.objective.strip() else []
        speaker_notes_parts.extend(context_lines)
        speaker_notes = "\n".join(part for part in speaker_notes_parts if part)

        contextual_elements = [
            TextElement(
                type="body",
                content=line,
                x=0.8,
                y=1.35 + (offset * 0.34),
                width=11.2,
                height=0.3,
                font_size=15,
                color="#5D6B82",
            )
            for offset, line in enumerate(context_lines[:3], start=1 if slide.objective else 0)
        ]

        return SlideSpec(
            slide_index=slide.slide_index,
            layout=slide.layout,
            topic=slide.topic,
            speaker_notes=speaker_notes or None,
            elements=[
                TextElement(
                    type="title",
                    content=slide.topic,
                    x=0.5,
                    y=0.3,
                    width=12.0,
                    height=0.9,
                    font_size=32,
                    bold=True,
                    color="#1F3864",
                ),
                *(
                    [
                        TextElement(
                            type="body",
                            content=slide.objective,
                            x=0.8,
                            y=1.35,
                            width=11.2,
                            height=0.8,
                            font_size=18,
                            color="#445066",
                        )
                    ]
                    if slide.objective
                    else []
                ),
                *contextual_elements,
            ],
        )

    def _fix_js_quotes(self, code: str) -> str:
        """修复 JS 字符串中的内嵌引号，避免中英文混排文本打断字符串字面量。"""
        return self._escape_problematic_js_string_quotes(code)

    def _extract_code(self, raw: str) -> str:
        """从 LLM 响应中提取 <code>...</code> 或 ```javascript...``` 中的代码。"""
        m = re.search(r"<code>(.*?)</code>", raw, re.DOTALL)
        if m:
            return m.group(1).strip()

        m = re.search(r"<code>(.*)$", raw, re.DOTALL)
        if m:
            return m.group(1).strip()

        m = re.search(r"```(?:javascript|js)\s*(.*?)```", raw, re.DOTALL)
        if m:
            return m.group(1).strip()

        m = re.search(r"```(?:javascript|js)\s*(.*)$", raw, re.DOTALL)
        if m:
            return m.group(1).strip()

        m = re.search(r"```\s*(.*?)```", raw, re.DOTALL)
        if m:
            return m.group(1).strip()

        m = re.search(r"```\s*(.*)$", raw, re.DOTALL)
        if m:
            return m.group(1).strip()

        raise ValueError("LLM 响应中未找到 <code> 或 ```javascript 代码块")

    def _extract_json(self, raw: str):
        cleaned = str(raw or "").strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

        candidates: list[str] = []
        if cleaned:
            candidates.append(cleaned)

        extracted = self._extract_outer_json_blob(cleaned)
        if extracted and extracted not in candidates:
            candidates.append(extracted)

        sanitized_cleaned = self._sanitize_json_strings(cleaned)
        if sanitized_cleaned and sanitized_cleaned not in candidates:
            candidates.append(sanitized_cleaned)

        sanitized_extracted = self._extract_outer_json_blob(sanitized_cleaned)
        if sanitized_extracted and sanitized_extracted not in candidates:
            candidates.append(sanitized_extracted)

        repaired_candidates = [
            self._repair_missing_json_commas(sanitized_cleaned),
            self._repair_missing_json_commas(sanitized_extracted),
            self._close_truncated_json(sanitized_extracted),
            self._close_truncated_json(self._repair_missing_json_commas(sanitized_extracted)),
            self._close_truncated_json(self._repair_missing_json_commas(sanitized_cleaned)),
        ]
        for repaired in repaired_candidates:
            if repaired and repaired not in candidates:
                candidates.append(repaired)

        for candidate in candidates:
            try:
                return json.loads(candidate, strict=False)
            except json.JSONDecodeError:
                continue

        preview = (extracted or cleaned)[:200]
        raise ValueError(f"无法解析 JSON，原始内容前200字：{preview}")

    @staticmethod
    def _extract_outer_json_blob(text: str) -> str:
        positions = [(text.find("{"), "{"), (text.find("["), "[")]
        positions = [(pos, ch) for pos, ch in positions if pos >= 0]
        if not positions:
            return text

        start, opener = min(positions, key=lambda item: item[0])
        closing_map = {"{": "}", "[": "]"}
        stack = [closing_map[opener]]
        in_string = False
        escape = False

        for i in range(start + 1, len(text)):
            ch = text[i]

            if escape:
                escape = False
                continue

            if ch == "\\":
                escape = True
                continue

            if ch == '"':
                in_string = not in_string
                continue

            if in_string:
                continue

            if ch in closing_map:
                stack.append(closing_map[ch])
                continue

            if stack and ch == stack[-1]:
                stack.pop()
                if not stack:
                    return text[start:i + 1]

        return text[start:]

    @staticmethod
    def _sanitize_json_strings(text: str) -> str:
        quote_escape_map = {
            "\u201c": "\\u201c",
            "\u201d": "\\u201d",
            "\u2018": "\\u2018",
            "\u2019": "\\u2019",
            "\u300c": "\\u300c",
            "\u300d": "\\u300d",
            "\u300e": "\\u300e",
            "\u300f": "\\u300f",
        }

        result: list[str] = []
        in_string = False
        i = 0

        while i < len(text):
            ch = text[i]
            next_ch = text[i + 1] if i + 1 < len(text) else ""

            if ch == "\\" and in_string and next_ch:
                result.extend((ch, next_ch))
                i += 2
                continue

            if not in_string:
                result.append(ch)
                if ch == '"':
                    in_string = True
                i += 1
                continue

            if ch in quote_escape_map:
                result.append(quote_escape_map[ch])
                i += 1
                continue

            if ch == "\n":
                result.append("\\n")
                i += 1
                continue

            if ch == "\r":
                result.append("\\r")
                i += 1
                continue

            if ch == "\t":
                result.append("\\t")
                i += 1
                continue

            if ch == '"':
                if PlannerAgent._is_probable_json_string_end(text, i):
                    in_string = False
                    result.append(ch)
                else:
                    result.append("\\u0022")
                i += 1
                continue

            result.append(ch)
            i += 1

        sanitized = "".join(result)
        sanitized = re.sub(r",(\s*[}\]])", r"\1", sanitized)
        return sanitized

    @staticmethod
    def _consume_json_string(text: str, start: int) -> int:
        i = start + 1
        escape = False

        while i < len(text):
            ch = text[i]
            if escape:
                escape = False
                i += 1
                continue
            if ch == "\\":
                escape = True
                i += 1
                continue
            if ch == '"':
                return i + 1
            i += 1

        return len(text)

    @staticmethod
    def _consume_json_literal(text: str, start: int) -> int:
        i = start
        while i < len(text) and text[i] in "-+0123456789.eE":
            i += 1
        return i

    @staticmethod
    def _repair_missing_json_commas(text: str) -> str:
        if not text:
            return text

        result: list[str] = []
        stack: list[dict] = []
        i = 0

        def current():
            return stack[-1] if stack else None

        def maybe_insert_comma(next_char: str) -> None:
            ctx = current()
            if not ctx or ctx["state"] != "expect_comma_or_end":
                return
            if next_char.isspace() or next_char in ",}]":
                return
            result.append(",")
            ctx["state"] = "expect_key_or_end" if ctx["type"] == "object" else "expect_value_or_end"

        def mark_value_consumed() -> None:
            ctx = current()
            if not ctx:
                return
            if ctx["type"] == "object" and ctx["state"] == "expect_value":
                ctx["state"] = "expect_comma_or_end"
            elif ctx["type"] == "array" and ctx["state"] == "expect_value_or_end":
                ctx["state"] = "expect_comma_or_end"

        while i < len(text):
            ch = text[i]

            if ch.isspace():
                result.append(ch)
                i += 1
                continue

            maybe_insert_comma(ch)
            ctx = current()

            if ch == "{":
                was_value = bool(
                    ctx and (
                        (ctx["type"] == "object" and ctx["state"] == "expect_value")
                        or (ctx["type"] == "array" and ctx["state"] == "expect_value_or_end")
                    )
                )
                result.append(ch)
                stack.append({"type": "object", "state": "expect_key_or_end", "was_value": was_value})
                i += 1
                continue

            if ch == "[":
                was_value = bool(
                    ctx and (
                        (ctx["type"] == "object" and ctx["state"] == "expect_value")
                        or (ctx["type"] == "array" and ctx["state"] == "expect_value_or_end")
                    )
                )
                result.append(ch)
                stack.append({"type": "array", "state": "expect_value_or_end", "was_value": was_value})
                i += 1
                continue

            if ch == "}":
                result.append(ch)
                if stack and stack[-1]["type"] == "object":
                    popped = stack.pop()
                    if popped.get("was_value"):
                        mark_value_consumed()
                i += 1
                continue

            if ch == "]":
                result.append(ch)
                if stack and stack[-1]["type"] == "array":
                    popped = stack.pop()
                    if popped.get("was_value"):
                        mark_value_consumed()
                i += 1
                continue

            if ch == ",":
                result.append(ch)
                if ctx:
                    ctx["state"] = "expect_key_or_end" if ctx["type"] == "object" else "expect_value_or_end"
                i += 1
                continue

            if ch == ":":
                result.append(ch)
                if ctx and ctx["type"] == "object" and ctx["state"] == "expect_colon":
                    ctx["state"] = "expect_value"
                i += 1
                continue

            if ch == '"':
                end = PlannerAgent._consume_json_string(text, i)
                result.append(text[i:end])
                if ctx:
                    if ctx["type"] == "object":
                        if ctx["state"] == "expect_key_or_end":
                            ctx["state"] = "expect_colon"
                        elif ctx["state"] == "expect_value":
                            ctx["state"] = "expect_comma_or_end"
                    elif ctx["type"] == "array" and ctx["state"] == "expect_value_or_end":
                        ctx["state"] = "expect_comma_or_end"
                i = end
                continue

            literal_match = None
            for literal in ("true", "false", "null"):
                if text.startswith(literal, i):
                    literal_match = literal
                    break
            if literal_match:
                result.append(literal_match)
                mark_value_consumed()
                i += len(literal_match)
                continue

            if ch in "-0123456789":
                end = PlannerAgent._consume_json_literal(text, i)
                result.append(text[i:end])
                mark_value_consumed()
                i = end
                continue

            result.append(ch)
            i += 1

        repaired = "".join(result)
        repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)
        return repaired

    @staticmethod
    def _close_truncated_json(text: str) -> str:
        if not text:
            return text

        result: list[str] = []
        closers: list[str] = []
        in_string = False
        escape = False
        closing_map = {"{": "}", "[": "]"}

        for ch in text:
            result.append(ch)

            if escape:
                escape = False
                continue

            if ch == "\\":
                escape = True
                continue

            if ch == '"':
                in_string = not in_string
                continue

            if in_string:
                continue

            if ch in closing_map:
                closers.append(closing_map[ch])
            elif closers and ch == closers[-1]:
                closers.pop()

        if in_string:
            result.append('"')

        while closers:
            result.append(closers.pop())

        return "".join(result)

    @staticmethod
    def _is_probable_json_string_end(text: str, quote_index: int) -> bool:
        j = quote_index + 1
        while j < len(text) and text[j].isspace():
            j += 1
        return j >= len(text) or text[j] in "\",:}]"

    def _build_error_feedback(self, err_msg: str) -> list[str]:
        feedback = [err_msg[:500]]
        if "Missing/Invalid shape parameter" in err_msg:
            feedback.append(self._shape_parameter_fix_template.strip())
        return feedback

    @cached_property
    def _geometry_validation_policy(self) -> dict:
        try:
            raw = self._skill_runtime.load_reference(
                "visual-production",
                "geometry_validation_policy.json",
            )
            data = json.loads(raw)
        except Exception:
            data = {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _scan_balanced_segment(text: str, start_index: int, opener: str, closer: str) -> tuple[str, int] | None:
        depth = 0
        quote: str | None = None
        escape = False
        for index in range(start_index, len(text)):
            char = text[index]
            if quote:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == quote:
                    quote = None
                continue
            if char in ('"', "'", "`"):
                quote = char
                continue
            if char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    return text[start_index:index + 1], index + 1
        return None

    @staticmethod
    def _extract_shape_kind(call_body: str) -> str:
        match = re.match(r"\(\s*([^,]+)", call_body, flags=re.DOTALL)
        if not match:
            return ""
        first_arg = match.group(1).strip()
        lowered = first_arg.lower()
        if re.search(r"""["']line["']""", first_arg, flags=re.IGNORECASE):
            return "line"
        if lowered.endswith(".line") or lowered.endswith("shapes.line"):
            return "line"
        return lowered

    def _iter_geometry_option_blocks_with_ranges(self, code: str) -> list[tuple[str, str, str, int, int]]:
        call_specs = self._geometry_validation_policy.get("call_specs", [])
        blocks: list[tuple[str, str, str, int, int]] = []
        for spec in call_specs:
            call_name = str(spec.get("call_name", "")).strip()
            object_selector = str(spec.get("object_selector", "last")).strip().lower()
            if not call_name:
                continue
            pattern = re.compile(rf"\.{re.escape(call_name)}\s*\(")
            for match in pattern.finditer(code):
                open_paren = code.find("(", match.start())
                if open_paren < 0:
                    continue
                call_segment = self._scan_balanced_segment(code, open_paren, "(", ")")
                if not call_segment:
                    continue
                call_body, _ = call_segment
                object_literals: list[tuple[str, int, int]] = []
                cursor = 0
                while True:
                    brace_index = call_body.find("{", cursor)
                    if brace_index < 0:
                        break
                    block = self._scan_balanced_segment(call_body, brace_index, "{", "}")
                    if not block:
                        break
                    literal, next_index = block
                    absolute_start = open_paren + brace_index
                    object_literals.append((literal, absolute_start, absolute_start + len(literal)))
                    cursor = next_index
                if not object_literals:
                    continue
                options_literal, start_index, end_index = (
                    object_literals[0] if object_selector == "first" else object_literals[-1]
                )
                shape_kind = self._extract_shape_kind(call_body) if call_name == "addShape" else ""
                blocks.append((call_name, options_literal, shape_kind, start_index, end_index))
        return blocks

    def _iter_geometry_option_blocks(self, code: str) -> list[tuple[str, str, str]]:
        return [
            (call_name, options_literal, shape_kind)
            for call_name, options_literal, shape_kind, _, _ in self._iter_geometry_option_blocks_with_ranges(code)
        ]

    @staticmethod
    def _geometry_number_pattern() -> str:
        return r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"

    @classmethod
    def _extract_option_value(cls, options_literal: str, key: str) -> str | None:
        match = re.search(rf"\b{re.escape(key)}\s*:\s*([^,\n}}]+)", options_literal)
        if not match:
            return None
        return match.group(1).strip()

    @classmethod
    def _extract_numeric_option(cls, options_literal: str, key: str) -> float | None:
        raw_value = cls._extract_option_value(options_literal, key)
        if raw_value is None:
            return None
        match = re.fullmatch(cls._geometry_number_pattern(), raw_value)
        if not match:
            return None
        try:
            return float(raw_value)
        except ValueError:
            return None

    @staticmethod
    def _format_geometry_number(value: float) -> str:
        if abs(value) < 0.0005:
            value = 0.0
        return f"{value:.3f}".rstrip("0").rstrip(".")

    @staticmethod
    def _clamp_geometry_value(value: float, lower: float, upper: float) -> float:
        return min(max(value, lower), upper)

    @classmethod
    def _replace_numeric_option_values(cls, options_literal: str, updates: dict[str, float]) -> str:
        updated = options_literal
        for key, value in updates.items():
            pattern = re.compile(rf"(\b{re.escape(key)}\s*:\s*)({cls._geometry_number_pattern()})")
            updated = pattern.sub(
                lambda match: f"{match.group(1)}{cls._format_geometry_number(value)}",
                updated,
                count=1,
            )
        return updated

    def _normalize_geometry_constraints(self, code: str) -> str:
        policy = self._geometry_validation_policy
        if not policy:
            return code
        slide_width = float(policy.get("slide_width", config.SLIDE_WIDTH_INCH))
        slide_height = float(policy.get("slide_height", config.SLIDE_HEIGHT_INCH))
        min_size = float(policy.get("min_geometry_size", 0.05))
        coordinate_keys = policy.get("coordinate_keys", {"x": "x", "y": "y", "w": "w", "h": "h"})
        key_x = str(coordinate_keys.get("x", "x"))
        key_y = str(coordinate_keys.get("y", "y"))
        key_w = str(coordinate_keys.get("w", "w"))
        key_h = str(coordinate_keys.get("h", "h"))

        replacements: list[tuple[int, int, str]] = []
        for call_name, options, shape_kind, start_index, end_index in self._iter_geometry_option_blocks_with_ranges(code):
            x = self._extract_numeric_option(options, key_x)
            y = self._extract_numeric_option(options, key_y)
            w = self._extract_numeric_option(options, key_w)
            h = self._extract_numeric_option(options, key_h)
            if None in (x, y, w, h):
                continue

            assert x is not None and y is not None and w is not None and h is not None
            if call_name == "addShape" and shape_kind == "line":
                start_x = self._clamp_geometry_value(x, 0, slide_width)
                start_y = self._clamp_geometry_value(y, 0, slide_height)
                end_x = self._clamp_geometry_value(x + w, 0, slide_width)
                end_y = self._clamp_geometry_value(y + h, 0, slide_height)
                new_w = end_x - start_x
                new_h = end_y - start_y
                if abs(new_w) < 0.0005 and abs(new_h) < 0.0005:
                    if start_x <= slide_width - min_size:
                        new_w = min_size
                    elif start_x >= min_size:
                        new_w = -min_size
                    elif start_y <= slide_height - min_size:
                        new_h = min_size
                    else:
                        new_h = -min_size
                normalized = {key_x: start_x, key_y: start_y, key_w: new_w, key_h: new_h}
            else:
                new_x = x
                new_y = y
                new_w = max(w, min_size)
                new_h = max(h, min_size)
                if new_x < 0:
                    new_w += new_x
                    new_x = 0.0
                if new_y < 0:
                    new_h += new_y
                    new_y = 0.0
                new_x = self._clamp_geometry_value(new_x, 0, max(0, slide_width - min_size))
                new_y = self._clamp_geometry_value(new_y, 0, max(0, slide_height - min_size))
                new_w = max(new_w, min_size)
                new_h = max(new_h, min_size)
                if new_x + new_w > slide_width:
                    new_w = max(min_size, slide_width - new_x)
                if new_y + new_h > slide_height:
                    new_h = max(min_size, slide_height - new_y)
                normalized = {key_x: new_x, key_y: new_y, key_w: new_w, key_h: new_h}

            original = {key_x: x, key_y: y, key_w: w, key_h: h}
            changed = {
                key: value
                for key, value in normalized.items()
                if abs(value - original[key]) >= 0.0005
            }
            if changed:
                replacements.append((start_index, end_index, self._replace_numeric_option_values(options, changed)))

        for start_index, end_index, replacement in reversed(replacements):
            code = code[:start_index] + replacement + code[end_index:]
        return code

    def _validate_geometry_constraints(self, code: str) -> None:
        policy = self._geometry_validation_policy
        if not policy:
            return
        slide_width = float(policy.get("slide_width", config.SLIDE_WIDTH_INCH))
        slide_height = float(policy.get("slide_height", config.SLIDE_HEIGHT_INCH))
        tolerance = float(policy.get("overflow_tolerance", 0.02))
        coordinate_keys = policy.get("coordinate_keys", {"x": "x", "y": "y", "w": "w", "h": "h"})
        messages = policy.get("messages", {})

        for call_name, options, shape_kind in self._iter_geometry_option_blocks(code):
            key_x = str(coordinate_keys.get("x", "x"))
            key_y = str(coordinate_keys.get("y", "y"))
            key_w = str(coordinate_keys.get("w", "w"))
            key_h = str(coordinate_keys.get("h", "h"))
            raw_values = {
                key_x: self._extract_option_value(options, key_x),
                key_y: self._extract_option_value(options, key_y),
                key_w: self._extract_option_value(options, key_w),
                key_h: self._extract_option_value(options, key_h),
            }
            missing_keys = [key for key, value in raw_values.items() if value is None]
            if missing_keys:
                raise ValueError(
                    f"检测到 {call_name} 缺少几何参数：{', '.join(missing_keys)}。"
                    "请为位置相关元素显式提供数字字面量 x/y/w/h。"
                )
            numeric_values: dict[str, float] = {}
            dynamic_keys: list[str] = []
            for key, raw_value in raw_values.items():
                assert raw_value is not None
                value = self._extract_numeric_option(options, key)
                if value is None:
                    dynamic_keys.append(f"{key}={raw_value}")
                else:
                    numeric_values[key] = value
            if dynamic_keys:
                raise ValueError(
                    f"检测到 {call_name} 使用了动态几何参数：{', '.join(dynamic_keys)}。"
                    "x/y/w/h 必须使用可校验的数字字面量，不要使用变量、表达式或函数调用。"
                )
            x = numeric_values[key_x]
            y = numeric_values[key_y]
            w = numeric_values[key_w]
            h = numeric_values[key_h]

            # LINE shapes use delta coordinates, so one axis may legitimately be zero or negative.
            if call_name == "addShape" and shape_kind == "line":
                end_x = x + w
                end_y = y + h
                min_x = min(x, end_x)
                min_y = min(y, end_y)
                max_x = max(x, end_x)
                max_y = max(y, end_y)
                if w == 0 and h == 0:
                    template = str(messages.get("non_positive_size", "{call_name} 使用了非正尺寸"))
                    raise ValueError(template.format(call_name=call_name, x=x, y=y, w=w, h=h))
                if min_x < 0 or min_y < 0:
                    template = str(messages.get("negative_origin", "{call_name} 的坐标超出页面左上边界"))
                    raise ValueError(template.format(call_name=call_name, x=x, y=y, w=w, h=h))
                if max_x > slide_width + tolerance or max_y > slide_height + tolerance:
                    template = str(messages.get("overflow", "{call_name} 超出页面边界"))
                    raise ValueError(
                        template.format(
                            call_name=call_name,
                            x=x,
                            y=y,
                            w=w,
                            h=h,
                            slide_width=slide_width,
                            slide_height=slide_height,
                        )
                    )
                continue

            if w <= 0 or h <= 0:
                template = str(messages.get("non_positive_size", "{call_name} 使用了非正尺寸"))
                raise ValueError(template.format(call_name=call_name, x=x, y=y, w=w, h=h))
            if x < 0 or y < 0:
                template = str(messages.get("negative_origin", "{call_name} 的坐标超出页面左上边界"))
                raise ValueError(template.format(call_name=call_name, x=x, y=y, w=w, h=h))
            if x + w > slide_width + tolerance or y + h > slide_height + tolerance:
                template = str(messages.get("overflow", "{call_name} 超出页面边界"))
                raise ValueError(
                    template.format(
                        call_name=call_name,
                        x=x,
                        y=y,
                        w=w,
                        h=h,
                        slide_width=slide_width,
                        slide_height=slide_height,
                    )
                )

    def _validate_generated_slide_code(self, code: str, image_path: str | None) -> None:
        """
        约束图片使用策略：
        - 无图片资产时，禁止 addImage / 远程 URL / preencoded 占位图
        - 有图片资产时，只允许引用该本地图片，不允许远程 URL 或伪图片
        """
        forbidden_markers = [
            "https://",
            "http://",
            "data:image",
            "images.unsplash.com",
            "preencoded.png",
            ".svg",
        ]
        lowered = code.lower()

        if image_path is None:
            if "addimage(" in lowered:
                raise ValueError(self._no_image_addimage_error_template.strip())
            for marker in forbidden_markers:
                if marker in lowered:
                    raise ValueError(
                        self._no_image_resource_error_template.format(marker=marker).strip()
                    )
            self._validate_geometry_constraints(code)
            return

        for marker in forbidden_markers:
            if marker in lowered:
                raise ValueError(
                    self._illegal_image_reference_error_template.format(marker=marker).strip()
                )

        path_literals = re.findall(r'addImage\s*\(\s*\{[^}]*?\bpath\s*:\s*["\']([^"\']+)["\']', code, re.DOTALL)
        invalid = [p for p in path_literals if p != image_path]
        if invalid:
            raise ValueError(
                self._unauthorized_image_path_error_template.format(image_path=invalid[0]).strip()
            )

        self._validate_geometry_constraints(code)

    def _sanitize_generated_code(self, code: str) -> str:
        def shape_literal(token: str) -> str | None:
            normalized = re.sub(r"[^A-Za-z0-9]+", "", token).lower()
            value = SHAPE_VALUE_MAP.get(normalized)
            return f'"{value}"' if value else None

        def replace_shape_member(match: re.Match) -> str:
            token = match.group(1)
            return shape_literal(token) or match.group(0)

        def replace_addshape_string(match: re.Match) -> str:
            prefix, quote, token, suffix = match.groups()
            return f"{prefix}{shape_literal(token) or f'{quote}{token}{quote}'}{suffix}"

        code = re.sub(
            r"\b[\w$]+\.(?:ShapeType|shapes)\.([A-Za-z0-9_]+)\b",
            replace_shape_member,
            code,
        )
        code = re.sub(
            r"(addShape\(\s*)([\"'])([A-Za-z0-9_]+)\2(\s*,)",
            replace_addshape_string,
            code,
        )
        code = self._strip_stray_js_bareword_lines(code)
        code = self._escape_problematic_js_string_quotes(code)
        return self._normalize_geometry_constraints(code)

    @staticmethod
    def _strip_stray_js_bareword_lines(code: str) -> str:
        lines = code.splitlines()
        if not lines:
            return code

        result: list[str] = []
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]{0,15}", stripped or ""):
                result.append(line)
                continue

            prev_nonempty = ""
            for prev_index in range(index - 1, -1, -1):
                candidate = lines[prev_index].strip()
                if candidate:
                    prev_nonempty = candidate
                    break

            next_nonempty = ""
            for next_index in range(index + 1, len(lines)):
                candidate = lines[next_index].strip()
                if candidate:
                    next_nonempty = candidate
                    break

            if prev_nonempty in {"{", ""} and (
                next_nonempty == "{"
                or next_nonempty.startswith("let slide = pres.addSlide(")
            ):
                continue

            result.append(line)

        sanitized = "\n".join(result)
        if code.endswith("\n"):
            sanitized += "\n"
        return sanitized

    def _enforce_theme_fonts(self, code: str, theme: dict) -> str:
        target_font = theme.get("body_font") or theme.get("header_font")
        if not target_font:
            return code

        def replace_fontface(match: re.Match) -> str:
            prefix, quote, _font_name, suffix = match.groups()
            return f"{prefix}{quote}{target_font}{suffix}"

        code = re.sub(
            r'(\bfontFace\s*:\s*)(["\'])([^"\']+)(\2)',
            replace_fontface,
            code,
        )
        return code

    def _is_probable_js_string_end(self, code: str, quote_index: int) -> bool:
        """
        判断字符串中的当前引号是否更像字面量结束，而不是正文里的引用符号。

        这里故意保守：只有在后面紧跟明显的 JS 分隔符时才视为闭合，
        其余情况统一转义，优先保证生成代码可执行。
        """
        j = quote_index + 1
        while j < len(code) and code[j].isspace():
            j += 1

        if j >= len(code):
            return True

        if code.startswith("//", j) or code.startswith("/*", j):
            return True

        return code[j] in ",:;)}]+-*/%?&|<=>"

    @staticmethod
    def _looks_like_missing_js_comma_after_string(code: str, quote_index: int) -> bool:
        j = quote_index + 1
        saw_newline = False

        while j < len(code) and code[j].isspace():
            if code[j] in "\r\n":
                saw_newline = True
            j += 1

        if not saw_newline or j >= len(code):
            return False

        if not (code[j].isalpha() or code[j] in "_$"):
            return False

        k = j + 1
        while k < len(code) and (code[k].isalnum() or code[k] in "_$"):
            k += 1
        while k < len(code) and code[k].isspace():
            k += 1

        return k < len(code) and code[k] == ":"

    def _escape_problematic_js_string_quotes(self, code: str) -> str:
        """
        逐字符扫描 JS 代码，修复字符串字面量中的问题引号。

        主要处理两类问题：
        1. 中文/日文弯引号直接出现在字符串里，统一转成 \\uXXXX
        2. 字符串内部裸露的 ASCII 单/双引号，若看起来不像字符串结束，则转成 \\u0027 / \\u0022
        """
        quote_escape_map = {
            "\u201c": "\\u201c",
            "\u201d": "\\u201d",
            "\u2018": "\\u2018",
            "\u2019": "\\u2019",
            "\u300c": "\\u300c",
            "\u300d": "\\u300d",
            "\u300e": "\\u300e",
            "\u300f": "\\u300f",
        }

        result: list[str] = []
        state = "normal"
        quote_char = None
        i = 0

        while i < len(code):
            ch = code[i]
            next_ch = code[i + 1] if i + 1 < len(code) else ""

            if state == "normal":
                if ch == "/" and next_ch == "/":
                    result.extend((ch, next_ch))
                    state = "line_comment"
                    i += 2
                    continue
                if ch == "/" and next_ch == "*":
                    result.extend((ch, next_ch))
                    state = "block_comment"
                    i += 2
                    continue
                if ch in ('"', "'"):
                    result.append(ch)
                    state = "string"
                    quote_char = ch
                    i += 1
                    continue
                if ch == "`":
                    result.append(ch)
                    state = "template"
                    i += 1
                    continue

                result.append(ch)
                i += 1
                continue

            if state == "line_comment":
                result.append(ch)
                i += 1
                if ch == "\n":
                    state = "normal"
                continue

            if state == "block_comment":
                if ch == "*" and next_ch == "/":
                    result.extend((ch, next_ch))
                    state = "normal"
                    i += 2
                    continue
                result.append(ch)
                i += 1
                continue

            if state == "template":
                if ch == "\\" and next_ch:
                    result.extend((ch, next_ch))
                    i += 2
                    continue
                result.append(ch)
                i += 1
                if ch == "`":
                    state = "normal"
                continue

            if ch == "\\" and next_ch:
                result.extend((ch, next_ch))
                i += 2
                continue

            if ch in quote_escape_map:
                result.append(quote_escape_map[ch])
                i += 1
                continue

            if ch == quote_char:
                if self._is_probable_js_string_end(code, i):
                    result.append(ch)
                    state = "normal"
                    quote_char = None
                elif self._looks_like_missing_js_comma_after_string(code, i):
                    result.append(ch)
                    result.append(",")
                    state = "normal"
                    quote_char = None
                else:
                    result.append("\\u0022" if quote_char == '"' else "\\u0027")
                i += 1
                continue

            result.append(ch)
            i += 1

        return "".join(result)

    def _inject_output_path(self, code: str, output_path: str) -> str:
        """确保代码中的 writeFile 使用正确的 output_path。"""
        safe_path = output_path.replace("\\", "/")

        if "writeFile" in code or "writeToFile" in code:
            code = re.sub(
                r'fileName\s*:\s*["\'][^"\']*["\']',
                f'fileName: "{safe_path}"',
                code
            )
            return code

        code += f'\npres.writeFile({{ fileName: "{safe_path}" }});\n'
        return code
