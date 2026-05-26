import html
import json
import logging
import math
import os
import re
import sys
import uuid
from collections.abc import Callable, Iterable
from functools import cached_property
from pathlib import Path
from typing import Any

import config
from backend.harness.agents.layout_planner import LayoutPlanner
from backend.harness.runtime import (
    HarnessTrace,
    PromptComposer,
    PromptSection,
    RepairOrchestrator,
    SkillContext,
    get_audience_aliases,
    get_audience_profiles,
    get_shape_value_map,
    get_supported_audiences,
    get_supported_styles,
    merge_prompt_sections,
)
from backend.models.schemas import (
    OutlinePlan,
    SlideEvalResult,
    SlideLayout,
    SlideLayoutIntent,
    SlideOutline,
    VisualMode,
    resolve_visual_mode,
)
from backend.tools.openai_compat import build_chat_completion_kwargs, stream_chat_completion_text
from backend.tools.pptx_skill import assert_skill_present, check_js_syntax, run_js
from openai import OpenAI

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
            timeout=config.PLANNER_REQUEST_TIMEOUT,
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
            "`\"rect\"`、`\"ellipse\"`、`\"line\"`、`\"roundRect\"`；"
            "不要使用 `pres.shapes.RECTANGLE`、`pptx.ShapeType.rect` 等常量写法。",
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

    @staticmethod
    def _style_requests_blue_white(style: str | None) -> bool:
        text = style or ""
        if not text:
            return False
        markers = (
            "蓝白",
            "#2563EB",
            "2563EB",
            "白底或淡蓝底",
            "不允许每章换主色",
            "固定使用同一套蓝白配色",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _blue_white_teaching_theme() -> dict:
        return {
            "primary_color": "2563EB",
            "secondary_color": "EFF6FF",
            "accent_color": "14B8A6",
            "header_font": "PingFang SC",
            "body_font": "PingFang SC",
            "motif_description": "蓝白教师课堂课件风格：白底或淡蓝底、教学蓝标题栏、白色内容卡片、浅蓝分隔线、青蓝关键节点标记",
            "pres_init_code": 'pres.layout = "LAYOUT_WIDE";',
        }

    def _stabilize_theme(self, theme: dict, outline: OutlinePlan, language: str, style: str = "") -> dict:
        normalized = dict(theme or {})
        if self._style_requests_blue_white(style):
            blue_white = self._blue_white_teaching_theme()
            normalized.update(
                {
                    "primary_color": blue_white["primary_color"],
                    "secondary_color": blue_white["secondary_color"],
                    "accent_color": blue_white["accent_color"],
                    "pres_init_code": normalized.get("pres_init_code") or blue_white["pres_init_code"],
                }
            )
            motif = str(normalized.get("motif_description") or "")
            if not motif or "深色" in motif or "大面积深底" in motif:
                normalized["motif_description"] = blue_white["motif_description"]
            normalized["palette_strategy_note"] = (
                "风格包要求全书固定蓝白配色：主色 #2563EB，白底或淡蓝底为主，"
                "避免大面积深底、橙色/紫色主视觉和强烈渐变。"
            )
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
        palette_note = theme.get("palette_strategy_note", "")
        return self._skill_runtime.render_template(
            "visual-production",
            "consistency_brief.txt",
            {
                "motif_line": f"- 当前视觉母题：{motif}" if motif else "",
                "font_line": "\n".join(
                    line
                    for line in (
                        f"- 字体策略：{note}" if note else "",
                        f"- 配色策略：{palette_note}" if palette_note else "",
                    )
                    if line
                ),
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
            try:
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
                retry_feedback = self._build_outline_retry_feedback(
                    err_msg,
                    min_slides=min_slides,
                    max_slides=max_slides,
                )
                continue
            self.last_reasoning = reasoning_text
            last_raw = raw_content

            try:
                data = self._extract_json(last_raw)
                outline = self._parse_outline_plan(data, topic)
                self._validate_outline_slide_count(
                    outline,
                    min_slides=min_slides,
                    max_slides=max_slides,
                )
                self._validate_outline_study_focus_required_pages(outline, content_requirements)
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
                retry_feedback = self._build_outline_retry_feedback(
                    err_msg,
                    min_slides=min_slides,
                    max_slides=max_slides,
                )

        if last_error:
            raise RuntimeError(f"页级大纲规划失败，最后错误：{last_error}；最后响应前500字：{last_raw[:500]}")
        raise RuntimeError(f"页级大纲规划失败，最后响应前500字：{last_raw[:500]}")

    @staticmethod
    def _outline_max_tokens(*, min_slides: int, max_slides: int) -> int:
        requested = max(min_slides, max_slides)
        if requested >= 48:
            return min(config.MAX_TOKENS_PLANNER, 12288)
        if requested >= 30:
            return min(config.MAX_TOKENS_PLANNER, 8192)
        return 4096

    def _build_outline_retry_feedback(self, err_msg: str, *, min_slides: int, max_slides: int) -> str:
        feedback = self._skill_runtime.render_template(
            "outline-planning",
            "outline_retry_feedback.txt",
            {
                "error_excerpt": err_msg[:220],
                "requested_slides": str(max(min_slides, max_slides)),
                "compact_mode": "开启" if max(min_slides, max_slides) >= 24 else "关闭",
            },
        )
        if self._is_provider_safety_filter_error(err_msg):
            feedback += (
                "\n\n安全误伤修复：这是大学管理研究方法课程的大纲规划。"
                "涉及访谈、个人经验、隐私、保密、伦理或投射技术时，只使用中性、合法、学术化的研究方法表述；"
                "不要生成真实个人身份、私密细节、攻击性内容或不适合作为课堂展示的案例。"
            )
        return feedback

    @staticmethod
    def _is_provider_safety_filter_error(error: str) -> bool:
        lowered = str(error or "").lower()
        markers = ("new_sensitive", "sensitive", "content policy", "safety", "安全")
        return any(marker in lowered for marker in markers)

    @staticmethod
    def _is_provider_transient_error(error: str) -> bool:
        lowered = str(error or "").lower()
        markers = (
            "connection error",
            "peer closed",
            "incomplete chunked read",
            "timeout",
            "timed out",
            "server disconnected",
            "connection reset",
            "api connection",
            "read error",
        )
        return any(marker in lowered for marker in markers)

    @staticmethod
    def _is_book_retryable_format_regression(error: str, error_signature: str | None) -> bool:
        signature = (error_signature or "").strip()
        lowered = str(error or "").lower()
        if signature in {
            "geometry_missing_coordinate",
            "js_syntax_generic",
            "js_syntax_quote_or_token",
            "missing_code_block",
            "slide_code_truncated",
        }:
            return True
        markers = (
            "缺少几何参数",
            "单页 js 语法检查失败",
            "missing code block",
            "slide code truncated",
            "incomplete code",
        )
        return any(marker in lowered for marker in markers)

    @classmethod
    def _layout_qa_enabled(cls) -> bool:
        raw = os.getenv("DIRECTIONAI_PPT_LAYOUT_QA", "").strip().lower()
        if raw in {"0", "false", "no", "off"}:
            return False
        if raw in {"1", "true", "yes", "on"}:
            return True
        return True

    @classmethod
    def _layout_qa_error_prefix(cls) -> str:
        return "电子书可读性校验失败" if cls._book_readability_qa_enabled() else "Layout QA校验失败"

    @classmethod
    def _is_layout_qa_validation_error(cls, error: str) -> bool:
        if not cls._layout_qa_enabled():
            return False
        text = str(error or "")
        return "Layout QA校验失败" in text or "电子书可读性校验失败" in text

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
                    data = self._stabilize_theme(data, outline, language, style=style)
                    print(f"[Planner] 视觉母题：{data.get('motif_description', '')}")
                    return data
                raise ValueError("返回结果不是预期的主题 JSON 对象")
            except Exception as e:
                last_error = e
                retry_instruction = self._theme_decision_retry_template
                logger.warning(f"[Planner] decide_visual_theme 第 {attempt} 次失败: {e}")

        if last_error is not None:
            logger.warning(f"[Planner] decide_visual_theme 失败，使用默认: {last_error}")
        fallback_theme = (
            self._blue_white_teaching_theme()
            if self._style_requests_blue_white(style)
            else {
                "primary_color": "1F3864",
                "secondary_color": "2E75B6",
                "accent_color": "FFFFFF",
                "header_font": "Arial Black",
                "body_font": "Calibri",
                "motif_description": "深色封面 + 浅色内容页 + 左侧色带装饰",
                "pres_init_code": 'pres.layout = "LAYOUT_WIDE";',
            }
        )
        return self._stabilize_theme(fallback_theme, outline, language, style=style)

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
        degraded_validation_code: str | None = None
        degraded_validation_error = ""
        degraded_validation_signature: str | None = None
        consecutive_degradable_validation_failures = 0
        recent_book_readability_error = ""
        max_attempts = MAX_RETRIES + (1 if self._book_ppt_qa_enabled() else 0)
        for attempt in range(1, max_attempts + 1):
            current_degraded_candidate: tuple[str, str, str | None] | None = None
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
                code = self._ensure_book_visible_example_anchors(code, slide, content_requirements)
                code = self._redact_unsupported_book_visible_expressions(
                    code,
                    slide,
                    content_requirements,
                )
                try:
                    self._validate_generated_slide_code(
                        code,
                        image_path=image_path,
                        slide=slide,
                        content_requirements=content_requirements,
                    )
                except Exception as validation_error:
                    validation_message = str(validation_error)
                    validation_signature = self._repair_orchestrator.classify_error(
                        validation_message,
                        stage=trigger_stage,
                        image_path=image_path,
                    )
                    degraded_code = self._degraded_validation_candidate(
                        code,
                        error=validation_message,
                        error_signature=validation_signature,
                    )
                    if degraded_code:
                        current_degraded_candidate = (
                            degraded_code,
                            validation_message,
                            validation_signature,
                        )
                    raise

                code = self._normalize_slide_code_block(code)
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
                transient_provider_error = self._is_provider_transient_error(err_msg)
                book_readability_error = self._is_book_readability_validation_error(err_msg)
                if book_readability_error:
                    recent_book_readability_error = err_msg
                if current_degraded_candidate:
                    (
                        degraded_validation_code,
                        degraded_validation_error,
                        degraded_validation_signature,
                    ) = current_degraded_candidate
                    consecutive_degradable_validation_failures += 1
                else:
                    consecutive_degradable_validation_failures = 0
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
                retry_feedback.extend(self._book_provider_transient_retry_guidance(err_msg))
                retry_feedback.extend(self._book_provider_safety_retry_guidance(err_msg))
                retry_feedback.extend(self._layout_qa_retry_guidance(err_msg))
                retry_feedback.extend(self._book_readability_retry_guidance(err_msg, attempt))
                retry_feedback.extend(self._book_strict_retry_guidance(err_msg))
                retry_book_format_regression = (
                    attempt >= MAX_RETRIES
                    and attempt < max_attempts
                    and recent_book_readability_error
                    and self._is_book_retryable_format_regression(err_msg, last_error_signature)
                )
                if retry_book_format_regression:
                    retry_feedback.append(
                        "电子书返修退化保护：上一轮已有明确版式问题，当前输出又退化成几何参数或代码完整性错误。"
                        "请保留教材内容和重排方向，重新输出完整可执行代码；所有 addText/addShape/addImage/addChart 都必须显式写数字 x/y/w/h，"
                        "同时继续解决上一轮版式问题。"
                    )
                    retry_feedback.extend(self._book_readability_retry_guidance(recent_book_readability_error, attempt))
                if attempt >= MAX_RETRIES and not transient_provider_error and not retry_book_format_regression:
                    break

        if (
            degraded_validation_code
            and consecutive_degradable_validation_failures >= MAX_RETRIES
            and self._is_required_image_asset_usage_error(degraded_validation_error)
        ):
            warning = (
                f"[Planner] 第 {slide.slide_index} 页连续 {MAX_RETRIES} 次未采用本地图片资产，"
                "已放行最后一次可执行页面代码继续生成 PPT。"
            )
            if degraded_validation_error:
                warning += f" 最后校验错误：{self._condense_error_excerpt(degraded_validation_error)}"
            print(warning)
            logger.warning(warning)
            return degraded_validation_code

        if degraded_validation_code and self._is_layout_qa_validation_error(degraded_validation_error):
            warning = (
                f"[Planner] 第 {slide.slide_index} 页连续 {MAX_RETRIES} 次未完全通过 Layout QA，"
                "已放行最后一次可执行页面代码继续生成 PPT。"
            )
            if degraded_validation_error:
                warning += f" 最后校验错误：{self._condense_error_excerpt(degraded_validation_error)}"
            print(warning)
            logger.warning(warning)
            return degraded_validation_code

        if (
            degraded_validation_code
            and self._is_book_readability_validation_error(degraded_validation_error)
            and self._book_readability_validation_degrade_enabled()
        ):
            prefix = (
                f"[Planner] 第 {slide.slide_index} 页连续 {MAX_RETRIES} 次未完全通过电子书可读性校验，"
                if consecutive_degradable_validation_failures >= MAX_RETRIES
                else f"[Planner] 第 {slide.slide_index} 页第 {MAX_RETRIES} 次仍未完全通过电子书可读性校验，"
            )
            warning = prefix + "已放行最后一次可执行页面代码继续生成 PPT。"
            if degraded_validation_error:
                warning += f" 最后校验错误：{self._condense_error_excerpt(degraded_validation_error)}"
            print(warning)
            logger.warning(warning)
            return degraded_validation_code

        if (
            degraded_validation_code
            and consecutive_degradable_validation_failures >= MAX_RETRIES
            and self._validation_degrade_enabled()
        ):
            warning = (
                f"[Planner] 第 {slide.slide_index} 页连续 {MAX_RETRIES} 次可降级校验失败，"
                "已放行最后一次可执行代码继续生成 PPT。"
            )
            if degraded_validation_signature:
                warning += f" 错误类型：{degraded_validation_signature}。"
            if degraded_validation_error:
                warning += f" 最后校验错误：{self._condense_error_excerpt(degraded_validation_error)}"
            print(warning)
            logger.warning(warning)
            return degraded_validation_code

        if self._validation_degrade_enabled() and not self._is_book_readability_validation_error(last_error):
            fallback_code = self._build_safe_fallback_slide_code(
                slide=slide,
                last_error=last_error,
                content_requirements=content_requirements,
            )
            warning = (
                f"[Planner] 第 {slide.slide_index} 页连续 {MAX_RETRIES} 次未生成可用页面，"
                "已使用稳定兜底页继续生成 PPT。"
            )
            if last_error_signature:
                warning += f" 错误类型：{last_error_signature}。"
            if last_error:
                warning += f" 最后错误：{self._condense_error_excerpt(last_error)}"
            print(warning)
            logger.warning(warning)
            return fallback_code

        raise RuntimeError(
            self._build_slide_generation_failure(
                slide_index=slide.slide_index,
                last_error=last_error,
                last_error_signature=last_error_signature,
                last_raw=last_raw,
            )
        )

    def _build_safe_fallback_slide_code(
        self,
        *,
        slide: SlideOutline,
        last_error: str = "",
        content_requirements: str = "",
    ) -> str:
        title = self._fallback_text(slide.topic or f"第 {slide.slide_index + 1} 页", 44)
        objective = self._fallback_text(
            slide.objective or "梳理本页主题的关键概念、教材依据与课堂讨论方向。",
            120,
        )
        source_hint = self._extract_fallback_source_hint(content_requirements)
        review_hint = "结合教材原文，先确认核心概念，再补充课堂讲解或练习。"
        body_lines = [objective]
        if source_hint:
            body_lines.append(source_hint)
        body_lines.append(review_hint)
        body = "\n".join(f"• {line}" for line in body_lines if line)

        return "\n".join(
            [
                "{",
                "let slide = pres.addSlide();",
                'slide.background = { color: "F8FAFC" };',
                'slide.addShape("rect", { x: 0, y: 0, w: 13.333, h: 0.28, fill: { color: "2563EB" }, line: { color: "2563EB" } });',
                'slide.addText('
                + json.dumps(title, ensure_ascii=False)
                + ', { x: 0.75, y: 0.62, w: 11.8, h: 0.72, fontFace: "Microsoft YaHei", fontSize: 28, bold: true, color: "0F172A", margin: 0.02, breakLine: false, fit: "shrink" });',
                'slide.addShape("rect", { x: 0.78, y: 1.65, w: 11.78, h: 3.95, fill: { color: "FFFFFF", transparency: 0 }, line: { color: "CBD5E1", transparency: 15 } });',
                'slide.addText("本页要点", { x: 1.08, y: 1.95, w: 3.2, h: 0.42, fontFace: "Microsoft YaHei", fontSize: 18, bold: true, color: "2563EB", margin: 0.02, lineSpacingMultiple: 1.25 });',
                'slide.addText('
                + json.dumps(body, ensure_ascii=False)
                + ', { x: 1.08, y: 2.55, w: 11.0, h: 2.35, fontFace: "Microsoft YaHei", fontSize: 18, color: "1E293B", breakLine: false, fit: "shrink", valign: "mid", margin: 0.04, lineSpacingMultiple: 1.25, paraSpaceAfterPt: 8 });',
                'slide.addShape("line", { x: 1.08, y: 5.88, w: 11.0, h: 0, line: { color: "CBD5E1", width: 1 } });',
                'slide.addText("课堂提示：围绕本页主题提炼 1 个关键判断，并让学生说明依据。", { x: 1.08, y: 6.15, w: 11.0, h: 0.48, fontFace: "Microsoft YaHei", fontSize: 14, color: "475569", margin: 0.02, fit: "shrink", lineSpacingMultiple: 1.25 });',
                "}",
            ]
        )

    @classmethod
    def _extract_fallback_source_hint(cls, content_requirements: str) -> str:
        markers = ("教材页码", "教材依据", "原文依据", "source_pages", "source pages", "页码范围")
        for raw_line in str(content_requirements or "").splitlines():
            line = raw_line.strip().lstrip("-").strip()
            if not line or not any(marker in line for marker in markers):
                continue
            line = re.sub(r"\s+", " ", line)
            return cls._fallback_text(line, 130)
        return ""

    @staticmethod
    def _fallback_text(value: str, limit: int) -> str:
        cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: max(1, limit - 1)] + "…"

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
            identifier="slide_generation:book_chapter_context",
            content=self._format_book_chapter_context_section(slide),
            group="core",
            required=True,
            reason="book chapter identity",
        )
        append_section(
            identifier="slide_generation:book_slide_blueprint",
            content=self._format_book_slide_blueprint_section(slide, content_requirements),
            group="core",
            required=True,
            reason="book slide section and study-focus labels",
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
            identifier="slide_generation:layout_qa_generation",
            content=self._format_layout_qa_generation_section(layout_intent),
            group="layout",
            required=True,
            reason="layout qa",
        )
        append_section(
            identifier="slide_generation:book_readability_generation",
            content=self._format_book_readability_generation_section(layout_intent, slide, content_requirements),
            group="layout",
            required=True,
            reason="ebook readability budget",
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
        assembly_attempt = 0
        while True:
            try:
                run_js(full_code, output_path)
                break
            except RuntimeError as exc:
                assembly_attempt += 1
                if assembly_attempt > 3:
                    raise
                repaired_codes = self._repair_failed_assembly(slide_codes, theme, str(exc))
                if repaired_codes is None:
                    raise
                slide_codes[:] = repaired_codes
                full_code = self._compose_full_code(slide_codes, output_path, theme)
                self._export_generated_js(slide_codes, full_code, output_path)
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
        full_code = self._sanitize_generated_code(
            full_code,
            apply_book_readability_normalization=False,
        )
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

        if failing_index is None:
            runtime_line = self._extract_js_runtime_line(error)
            if runtime_line is not None:
                failing_index = self._slide_index_for_full_code_line(slide_codes, theme, runtime_line)
                if failing_index is not None:
                    logger.warning(
                        "[Planner] 组装运行时报错定位到第 %s 个代码片段（JS line %s）",
                        failing_index,
                        runtime_line,
                    )

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

    @staticmethod
    def _extract_js_runtime_line(error: str) -> int | None:
        matches = re.findall(r"\.js:(\d+):\d+", error or "")
        if not matches:
            return None
        try:
            return int(matches[0])
        except ValueError:
            return None

    def _slide_index_for_full_code_line(
        self,
        slide_codes: list[str],
        theme: dict,
        line_no: int,
    ) -> int | None:
        if line_no <= 0:
            return None
        full_code = self._compose_full_code(slide_codes, "__assembly_probe__.pptx", theme)
        starts: list[tuple[int, int]] = []
        for idx, line in enumerate(full_code.splitlines(), start=1):
            match = re.match(r"// ===== 第 (\d+) 页 =====", line)
            if match:
                starts.append((idx, int(match.group(1))))
        if not starts:
            return None
        for position, (start_line, slide_index) in enumerate(starts):
            next_start = starts[position + 1][0] if position + 1 < len(starts) else 10**9
            if start_line <= line_no < next_start:
                return slide_index
        return None

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
        visible_slide_index = slide.slide_index + 1 if self._book_ppt_qa_enabled() else slide.slide_index
        return self._render_template_string(
            self._page_info_section_template,
            {
                "slide_index": visible_slide_index,
                "layout": slide.layout.value,
                "topic": slide.topic,
                "objective": slide.objective,
                "visual_mode": effective_visual_mode.value,
                "image_prompt_line": f"- 图片描述：{slide.image_prompt}" if slide.image_prompt else "",
            },
        ).strip()

    def _format_book_chapter_context_section(self, slide: SlideOutline) -> str:
        if not self._book_ppt_qa_enabled():
            return ""
        chapter_number = (os.getenv("DIRECTIONAI_BOOK_CHAPTER_NUMBER") or "").strip()
        if not chapter_number:
            return ""
        title = (os.getenv("DIRECTIONAI_BOOK_CHAPTER_TITLE") or "").strip()
        labels = sorted(self._expected_chapter_labels(chapter_number))
        label_text = " / ".join(labels)
        lines = [
            "## 电子书章节身份（硬性约束）",
            f"- 当前章节号：{label_text}",
            f"- 当前章节标题：{title or slide.topic}",
            "- 本页只能使用当前章节号，禁止写成其他章节号。",
            "- PPT 页序只用于生成顺序，不能当成教材页码；严禁把“PPT第 N 页”写成“p.N”。",
            "- 页面可见教材页码只能来自本页蓝图里的“教材页码/教材依据”，严禁凭当前 PPT 页序自造页码。",
            "- 页面可见页码严禁显示“第0页”。",
        ]
        if slide.slide_index == 0:
            lines.append(f"- 封面页必须显式出现章节号：{label_text}。")
        return "\n".join(lines)

    def _format_book_slide_blueprint_section(self, slide: SlideOutline, content_requirements: str) -> str:
        if not self._book_study_focus_metadata_enabled(content_requirements):
            return ""
        section = self._book_slide_requirement_section(slide, content_requirements)
        section_label, focus_levels = self._book_slide_metadata_labels(slide, content_requirements)
        if not section and not section_label:
            return ""
        lines = [
            "## 本页教材/考纲参考提示",
        ]
        if section:
            lines.extend(
                [
                    "下面是本页在页级要求中的蓝图，只用于当前页生成：",
                    section.strip()[:1600],
                ]
            )
        else:
            lines.append("本页根据当前 runtime 页面主题、教材目录和本课考纲层次映射自动匹配标注。")
        if section_label:
            lines.append(f"- 优先在标题、副标题或正文结构中自然体现教材目录标题：{section_label}")
        if focus_levels:
            lines.append(f"- 本页命中考纲层级，优先在内容组织中体现：{' / '.join(focus_levels)}")
        elif section and ("考纲层级：无" in section or "考纲层级: 无" in section):
            lines.append("- 本页未命中具体考纲层级，不必添加考纲层级标签。")
        elif not focus_levels:
            lines.append("- 本页未明确命中具体考纲层级，不必添加考纲层级标签。")
        return "\n".join(lines)

    def _format_layout_qa_generation_section(self, layout_intent: SlideLayoutIntent) -> str:
        if not self._layout_qa_enabled():
            return ""
        content_budget = self._layout_qa_content_budget_rules(layout_intent)
        return "\n".join(
            [
                "## 通用 Layout QA（硬性版式约束）",
                "- 可见文字最低 14pt；正文、说明、流程节点说明、表格单元格使用 1.25 倍行距。",
                "- 生成时必须按 14pt/16pt + 1.25 行距预留真实空间；不要用 0.2-0.3 英寸高的小文本框承载正文。",
                "- 所有卡片、节点、圆形、色块必须完整包住内部文字，并保留安全内边距；扩大文字框时同步扩大外层形状和相邻间距。",
                "- 多个卡片/节点之间必须留出明确间隔，不能相互重叠、贴边堆叠或压住相邻连线。",
                "- 背景形状先画，文字后画；不要让后绘制的填充色块盖住标题、正文、标签或页脚。",
                "- 框图/流程图连线必须从节点边缘出发，避开所有文字；复杂关系优先改成水平/垂直分段线、表格或上下分区。",
                "- 中文正文表格不要使用 `slide.addTable`；需要表格时用 `slide.addShape` 画单元格，再用 `slide.addText` 写中文，避免渲染成方框问号。",
                "- 如果页面内容放不下，优先改用表格、分栏、上下结构或下一页承接；不要靠小字号、单倍行距、裁切或元素重叠解决。",
                content_budget,
            ]
        )

    @staticmethod
    def _layout_qa_content_budget_rules(layout_intent: SlideLayoutIntent) -> str:
        page_intent = layout_intent.page_intent.value
        archetype = layout_intent.archetype
        if page_intent in {"show_process", "show_structure", "explain_mechanism"} or archetype in {"diagram-focused", "timeline-flow"}:
            return "- 流程/结构页优先 3-5 个节点；节点超过 5 个或每个节点有说明文字时，改成表格、分栏或分段流程。"
        if page_intent == "compare_options" or archetype == "comparison-split":
            return "- 对比页优先全宽形状网格、左右等高分区或上下分区；不要把多个比较维度拆成许多小卡片或漂浮标签。"
        if page_intent == "group_insights" or archetype == "card-grid-insight":
            return "- 卡片页优先 3-4 张主卡；超过 4 项时改成表格或分组列表，避免卡片墙。"
        if page_intent == "synthesize":
            return "- 总结页最多 3 个回顾点或 1 句核心结论；总结条不要挤压主体内容。"
        return "- 概念/讲解页只放一个主结构；定义、例子、结论不要同时堆成多套卡片。"

    def _format_book_readability_generation_section(
        self,
        layout_intent: SlideLayoutIntent,
        slide: SlideOutline | None = None,
        content_requirements: str = "",
    ) -> str:
        if not self._book_readability_qa_enabled():
            return ""
        budget_rules = self._book_content_budget_rules(layout_intent)
        page_specific_rules = self._book_readability_page_specific_rules(
            slide,
            study_focus_enabled=self._book_study_focus_metadata_enabled(content_requirements),
        )
        return "\n".join(
            [
                "## 电子书课件可读性预算（硬性约束）",
                "- 如果本节规则与 layout_intent、visual_mode、archetype 或视觉母题中的“卡片/图示”建议冲突，必须以本节可读性规则为准。",
                "- 字号分层优先落到代码：一级模块/分区标题 20pt；二级卡片/节点标题 18pt；正文、说明、公式解释、流程节点说明优先 16pt。正文使用 14pt 时必须给足文本框和外层卡片高度，不能靠小框裁切。",
                "- 多行正文必须显式写 `lineSpacingMultiple: 1.25`，并按真实行数预留高度：2 行正文 h>=0.62，3 行正文 h>=0.92，4 行正文 h>=1.22。",
                "- 如果文字放在卡片、色块、圆角矩形或节点框里，外层形状必须完整包住文字框，并预留至少 0.08 英寸内边距；不要只扩大 addText 而不扩大外层形状。",
                "- 每页只能保留一个主视觉结构；不要把流程图、卡片墙、总结条、侧边竖牌和多个小图同时堆在一页。",
                "- “核心结论 / 记忆要点 / 注意 / 选择依据”等总结语如果超过一行，必须作为表格/分区中的一行或右侧等高栏呈现；不要放成底部大色块、相邻小卡片或悬浮提示条来挤压主体内容。",
                "- 3 个以上并列分类、决策层级、方法清单或带说明节点，优先改成表格/两列分组/上下分区；不要用阶梯卡片、斜向串联卡片或小圆点压住长说明。",
                "- 如果你打算使用“左侧说明 + 右侧多卡片/多节点图”，必须先确认右侧每个节点都有足够宽高；一旦有 3 个以上带说明节点，直接改成全宽表格或上下分区，不要继续做右侧卡片图。",
                "- 如果 generated_image、visual-hero-split 或主视觉区域会挤占表格/分区所需空间，图片必须降级为辅助缩略图、角落图或背景氛围，不占正文主面积；不要为了展示图片把教材内容压成窄栏小卡片或斜向节点。",
                "- 电子书正文页默认不要使用圆形章节编号、悬浮徽章、大号装饰标签或覆盖式色块；章节编号、教材标题和可选参考标签优先放成副标题文本、左侧窄条或表格栏位。",
                "- 装饰圆、章节徽章、可选参考标签、页码徽章不能覆盖标题或正文；内容拥挤时删掉装饰，不要删可读性。",
                "- 连接线只用于节点之间，必须从节点边缘出发并避开所有文字；复杂关系改成横向/纵向分段线或表格，不用长斜线。",
                "- 含 3 步以上的箭头链或流程句时，不要写成一条长文本塞进卡片；改成编号行、纵向步骤表或上下分段，每行只放一个动作和一句短说明。",
                "- 首稿就按稳定版式生成，不要期待后续返修：如果一个设计需要把卡片、节点、结论条都压缩到很小才能放下，必须改用更宽的分栏/表格/上下结构，保留教材知识点，不删关键内容。",
                budget_rules,
                page_specific_rules,
            ]
        )

    @staticmethod
    def _book_readability_page_specific_rules(
        slide: SlideOutline | None,
        *,
        study_focus_enabled: bool = False,
    ) -> str:
        if slide is None:
            return ""
        blob = f"{slide.topic or ''} {slide.objective or ''} {getattr(slide, 'image_prompt', '') or ''}"
        rules: list[str] = []
        case_structure = re.search(r"(案例|示例|情境|场景|任务|练习)", blob) and re.search(
            r"(类型|分类|层级|层次|步骤|流程|原则|条件|因素|方法|设计|方案|决策|路径)", blob
        )
        if case_structure:
            rules.append(
                "- 当前页同时包含案例/示例和结构化知识点：优先使用全宽表格或上下分区作为主结构，表头可用“类别或步骤 / 含义 / 案例对应”；"
                "即使 visual_mode 是 generated_image 或 archetype 是 visual-hero-split，也不能把正文压到左侧窄栏；图片最多作为小幅辅助图。"
                "禁止分叉箭头、阶梯卡片、右侧多卡片图、独立案例大卡片或斜向连接线；案例放入表格单元格或一行短注即可。"
            )
        definition_structure = re.search(r"(定义|含义|特征|边界|来源|形成|本质|属性|要素|抽象|归类)", blob)
        if definition_structure:
            rules.append(
                "- 当前页是定义/特征/边界类讲解：优先使用“术语或对象 / 准确定义 / 特征或例子”的表格、左右分区或上下分区；"
                "不要使用节点关系图、Venn 图、卡片墙、流程箭头或圆形编号徽章来承载长文本；短结论放入表格末行或独立短注。"
            )
        measurement_structure = re.search(r"(操作|测量|可观察|可检验|指标|规则|编码|记录|处理水平|变量)", blob) and re.search(
            r"(转化|定义|设计|控制|比较|关系|步骤|条件|标准)", blob
        )
        if measurement_structure:
            rules.append(
                "- 当前页包含操作/测量/规则转化结构：必须用全宽表格、左右对照或上下分区承载，推荐列为“对象或规则 / 含义 / 使用条件 / 教材例子”；"
                "不要用多个小卡片、对象流程箭头、转换链或底部大结论条来挤占空间；核心结论放入表格末行或独立短注。"
            )
        if study_focus_enabled and ("考纲" in blob or "识记" in blob or "理解" in blob or "应用" in blob):
            rules.append(
                "- 当前页涉及考纲层次：必须用表格呈现“层次 / 考纲原文要求 / 教材对应内容”，不要用金字塔、阶梯图、多色层级卡片、圆形编号或流程步骤承载长句；"
                "不要在考纲对照页展开研究流程图，只做层次对照和教材对应内容。"
            )
        relation_structure = re.search(r"(关系|对照|比较|区别|取舍|选择依据|vs|VS)", blob) and re.search(
            r"(类型|方法|方案|设计|变量|条件|水平|组)", blob
        )
        if relation_structure:
            rules.append(
                "- 当前页含关系/对照/取舍类结构信号：优先使用全宽形状网格、左右等高分区或上下分区承载，推荐列为“对象或类型 / 含义 / 判断依据 / 结论边界”；"
                "不要使用关系节点图、相邻说明卡片、教材要点卡、核心结论底条、双向箭头穿插长文本或多个小圆点承载长说明。"
            )
        taxonomy_structure = re.search(r"(类型|分类|体系|清单|方法|因素|威胁|方案|设计|原则|条件|步骤|流程)", blob) and re.search(
            r"(多种|多个|几类|四类|三类|两类|比较|总结|概览|汇总|区分|选择|权衡)", blob
        )
        if taxonomy_structure:
            rules.append(
                "- 当前页包含类型/分类/清单/因素体系：优先用全宽表格或左右两列分组承载，推荐列为“类别 / 含义 / 适用条件或教材例子”；"
                "禁止大椭圆/Venn 图、长斜线、漂浮标签和多张小卡片；章节编号或参考标签放在独立副标题行，不要与主标题硬挤在同一行。"
            )
        formula_structure = re.search(r"(公式|=|−|-|＋|\+|×|÷|→|←|↔|vs|VS)", blob) and re.search(
            r"(计算|效应|差异|对比|组|前测|后测|结果|判断|选择)", blob
        )
        if formula_structure:
            rules.append(
                "- 当前页包含公式/符号/箭头推理结构：把公式或符号放入独立全宽公式区，解释文字放在下方表格或左右分区；"
                "不要把公式、说明、记忆点分别做成相邻小卡片，也不要让公式和说明共用同一行。"
            )
        if any(token in blob for token in ("核心结论", "记忆要点", "注意", "选择依据", "口诀")):
            rules.append(
                "- 当前页包含总结/记忆/选择提示：总结语只能作为表格末行、右侧等高栏或独立短注；不要做底部大色块、悬浮标签或相邻小卡片来挤压主体内容。"
            )
        if not rules:
            return ""
        return "页面结构触发的稳定版式规则：\n" + "\n".join(rules)

    @staticmethod
    def _book_content_budget_rules(layout_intent: SlideLayoutIntent) -> str:
        page_intent = layout_intent.page_intent.value
        archetype = layout_intent.archetype
        if page_intent in {"show_process", "show_structure", "explain_mechanism"} or archetype in {"diagram-focused", "timeline-flow"}:
            return (
                "- 本页是流程/结构页：优先 3-4 个节点，绝不超过 4 个带说明节点；每个节点只保留“18pt 短标题 + 14-16pt 一行说明”；"
                "节点框之间至少留 0.12 英寸间距；需要承载更多内容时改成表格/分栏说明，不能删教材步骤。"
            )
        if page_intent == "group_insights" or archetype == "card-grid-insight":
            return (
                "- 本页是卡片页：优先 3 张主卡，最多 4 张；每张卡片至少 h>=1.15，最多“18pt 标题 + 14-16pt 两行说明”；"
                "超过 4 项时改成表格或分组列表，保留关键考点，不能再加底部长总结条挤占空间。"
            )
        if page_intent == "compare_options" or archetype == "comparison-split":
            return (
                "- 本页已被规划为对比页：必须把主体内容做成一个全宽表格式网格（用 `slide.addShape` 画单元格、用 `slide.addText` 写文字），"
                "或上下两个等高分区；不要使用 `slide.addTable` 承载中文正文。禁止把多个比较维度拆成独立小卡片、漂浮标签、圆形徽章或底部大结论条。"
                "表格/分区内正文 14-16pt + 1.25 行距，行高不足时增加行高或拆页承接，不能删教材关键点。"
            )
        if page_intent == "synthesize":
            return (
                "- 本页是总结页：最多 3 个回顾点或 1 句核心结论二选一为主；不要同时做大流程、变量关系和多卡片总结。"
            )
        return (
            "- 本页是概念/讲解页：只展示 1 个核心定义、最多 2 条边界或例子、可选 1 句短结论；"
            "教材长句可以教学化改写为短句，但不能丢失定义、条件、关系等关键考点；不要为了完整性生成 4 个以上互相挤压的正文块。"
        )

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
        elif page_intent == "case_study" or archetype == "visual-hero-split":
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
        if self._book_readability_qa_enabled():
            depth_rules = self._format_book_readability_content_depth_rules(layout_intent)
        return self._render_skill_template(
            "visual-production",
            "content_depth_section.txt",
            {
                "depth_rules": depth_rules + f"\n- 当前页面语义：`{page_intent.value}`；优先采用 `{evidence_mode.value}` 这种证据表达方式。",
                "source_hint": source_hint,
            },
        )

    @staticmethod
    def _format_book_readability_content_depth_rules(layout_intent: SlideLayoutIntent) -> str:
        page_intent = layout_intent.page_intent.value
        archetype = layout_intent.archetype
        shared = (
            "- 电子书初稿按低密度课件生成：页面主体最多 1 个主视觉结构，最多 5 个主要文字块；"
            "教材关键知识点必须保留在 PPT 可见内容中，不能用删内容换版面。\n"
            "- 正文可用 14-16pt，但必须配 1.25 行距和足够外层卡片/节点空间；不要靠后续重试修拥挤版式。\n"
            "- “核心结论 / 记忆要点 / 注意 / 选择依据”等总结语不能做成底部大色块或相邻小卡片来挤压主体内容；需要呈现时放入同一表格/分区的末行、右侧等高栏或独立短注。\n"
            "- 当本页有 3 个以上分类/层级/清单项且每项带说明时，优先用表格、两列分组或上下分区；禁止阶梯式卡片、斜向连接卡片或多个小圆点承载长文本。"
        )
        if page_intent == "compare_options" or archetype == "comparison-split":
            specific = (
                "- 对比页必须使用一个全宽表格式网格作为主体（用 `slide.addShape` 画单元格、用 `slide.addText` 写文字），或上下两个等高分区；不要使用 `slide.addTable` 承载中文正文，也不要继续生成多张小卡片、漂浮标签、圆形徽章或底部大结论条。\n"
                "- 表格行高必须能容纳 14-16pt + 1.25 行距；内容过多时增加行高、合并短句或拆页承接，不能删教材关键点。"
            )
        elif page_intent == "show_process" or archetype == "timeline-flow":
            specific = (
                "- 流程页优先 3-4 个节点，节点只写短标题和一行说明；5 个以上阶段必须合并为阶段组、改成表格或由下一页承接。\n"
                "- 连接线只在节点外缘之间水平/垂直连接，不使用长斜线、穿字线或悬浮箭头。"
            )
        elif page_intent == "group_insights" or archetype == "card-grid-insight":
            specific = (
                "- 卡片页优先 3 张主卡，最多 4 张；每张卡只写短标题 + 最多两行说明。\n"
                "- 如果卡片撑开后会贴近或重叠，首稿就改成两列分组、表格或上下分区，不要压缩卡片高度，也不要丢失关键点。"
            )
        elif page_intent in {"show_structure", "explain_mechanism"} or archetype == "diagram-focused":
            specific = (
                "- 结构图最多 3 个核心节点 + 1 个短结论；节点说明过长时改成旁侧两条短要点。\n"
                "- 圆形/Venn/阶梯图只承载短标签；长标签改成外置卡片或直接用表格。"
            )
        elif page_intent == "case_study" or archetype == "visual-hero-split":
            specific = (
                "- 案例/图片页让图片或案例场景承担视觉，正文保留必要案例事实与结论；不要再叠加多卡片网格。\n"
                "- 图片旁的文字必须是结论式短句；案例过程、决策层级或分类较长时用编号流程或表格，不用小卡片、阶梯卡片或斜线硬塞。"
            )
        elif page_intent == "synthesize" or archetype == "editorial-highlight":
            specific = (
                "- 总结页只保留 2-3 个回顾点或 1 个强结论，避免同时出现流程、卡片墙和总结条。\n"
                "- 结论长于两行时改成分栏或两行重点句，不要放进底部长条裁切。"
            )
        elif page_intent == "present_evidence" or archetype == "stat-callout":
            specific = (
                "- 证据页只突出 1 个主事实/主判断，最多 2 条解释；不要做数字墙或列表墙。\n"
                "- 证据标签、来源、页码放页脚或短标签，不占用主体卡片空间。"
            )
        else:
            specific = (
                "- 概念/讲解页只讲 1 个核心定义或判断，最多 2 条边界/例子；不要把教材段落完整搬上屏。\n"
                "- 如果教材内容必须覆盖更多点，用下一页、表格或分栏承接，不在本页继续加互相挤压的小卡片。"
            )
        return f"{shared}\n{specific}"

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
        strict_book_ppt = self._strict_book_ppt_qa_enabled()
        if strict_book_ppt:
            score_issue_types = self._classify_revision_scores(revision_feedback)
            issue_types = list(dict.fromkeys([*issue_types, *score_issue_types]))
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
        if strict_book_ppt:
            lines.extend(
                [
                    (
                        "- 当前评分："
                        f"layout={revision_feedback.layout_score:.1f}, "
                        f"content={revision_feedback.content_score:.1f}, "
                        f"design={revision_feedback.design_score:.1f}, "
                        f"overall={revision_feedback.overall:.1f}"
                    ),
                    "- 返修策略：不要只微调局部元素；请重新组织本页为稳定、高对比、低密度的版式。",
                    "- 高对比硬约束：深色填充上只能使用白色或接近白色文字；浅色填充上只能使用深色文字；禁止深底灰字、浅底浅字、透明正文、文字压在色块/图片/线条上。",
                    "- 稳定重排硬约束：保留清晰标题区、主视觉区、结论/任务区；每个文字块留足内边距；复杂表格、树图或装饰条导致低分时，改成 3-5 个短标签的对照表、流程图或练习板。",
                ]
            )
        if avoid_text:
            lines.append(f"- 不要重犯：{avoid_text}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _classify_revision_scores(revision_feedback: SlideEvalResult) -> list[str]:
        issue_types: list[str] = []
        if revision_feedback.layout_score < 3.8:
            issue_types.append("布局排版")
        if revision_feedback.content_score < 3.8:
            issue_types.append("内容结构")
        if revision_feedback.design_score < 3.8:
            issue_types.append("视觉设计")
        return issue_types

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
            if any(token in text for token in ("重叠", "裁切", "边距", "layout", "排版", "布局", "布局单项")):
                scores["布局排版"] += 2
            if any(token in text for token in ("低对比", "视觉重心", "配色", "设计", "design", "设计单项")):
                scores["视觉设计"] += 2
            if "内容单项" in text:
                scores["内容结构"] += 2

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
        if hasattr(self, "_repair_orchestrator"):
            excerpt = self._repair_orchestrator.extract_error_excerpt(stderr)
        else:
            excerpt = self._condense_error_excerpt(stderr)
        return f"单页 JS 语法检查失败：{excerpt}"

    @staticmethod
    def _normalize_slide_code_block(code: str) -> str:
        normalized = str(code or "").strip()
        if not normalized.startswith("{"):
            normalized = "{\n" + normalized + "\n}"
        return normalized

    def _degraded_validation_candidate(
        self,
        code: str,
        *,
        error: str,
        error_signature: str | None,
    ) -> str | None:
        if self._is_required_image_asset_usage_error(error):
            candidate = self._normalize_slide_code_block(code)
            if self._check_slide_code_syntax(candidate):
                return None
            return candidate
        if self._is_book_readability_validation_error(error):
            if not self._book_readability_validation_degrade_enabled():
                return None
            candidate = self._normalize_slide_code_block(code)
            if self._check_slide_code_syntax(candidate):
                return None
            return candidate
        if self._is_layout_qa_validation_error(error):
            candidate = self._normalize_slide_code_block(code)
            if self._check_slide_code_syntax(candidate):
                return None
            return candidate
        if not self._validation_degrade_enabled():
            return None
        if not self._is_degradable_slide_validation_error(error, error_signature):
            return None
        candidate = self._normalize_slide_code_block(code)
        if self._check_slide_code_syntax(candidate):
            return None
        return candidate

    @classmethod
    def _is_book_readability_validation_error(cls, error: str) -> bool:
        return cls._book_readability_qa_enabled() and "电子书可读性校验失败" in str(error or "")

    @classmethod
    def _book_readability_validation_degrade_enabled(cls) -> bool:
        raw = os.getenv("DIRECTIONAI_BOOK_PPT_ALLOW_READABILITY_DEGRADE", "").strip().lower()
        return raw in {"1", "true", "yes", "on"}

    @classmethod
    def _validation_degrade_enabled(cls) -> bool:
        raw = os.getenv("DIRECTIONAI_PPT_ALLOW_VALIDATION_DEGRADE", "").strip().lower()
        if raw in {"0", "false", "no", "off"}:
            return False
        if raw in {"1", "true", "yes", "on"}:
            return True
        return cls._book_ppt_qa_enabled()

    @staticmethod
    def _is_degradable_slide_validation_error(error: str, error_signature: str | None) -> bool:
        signature = (error_signature or "").strip()
        lowered = str(error or "").lower()
        non_degradable_signatures = {
            "forbidden_addimage_without_asset",
            "forbidden_remote_image",
            "invalid_shape_parameter",
            "js_syntax_generic",
            "js_syntax_quote_or_token",
            "missing_code_block",
            "slide_code_truncated",
            "unauthorized_image_path",
        }
        if signature in non_degradable_signatures:
            return False
        fatal_markers = [
            "addimage",
            "cannot access",
            "is not defined",
            "missing/invalid shape parameter",
            "referenceerror",
            "rich text",
            "syntaxerror",
            "未授权图片路径",
            "无图片模式",
            "单页 js 语法检查失败",
            "图片引用",
            "富文本数组",
            "非法图片",
        ]
        if any(marker in lowered for marker in fatal_markers):
            return False

        degradable_signatures = {
            "content_layout_underfilled",
            "generic_retry",
            "geometry_dynamic_coordinate",
            "geometry_negative_origin",
            "geometry_overflow",
            "visual_low_score_layout",
        }
        if signature in degradable_signatures:
            return True
        degradable_markers = [
            "内容页文字过少",
            "原文锚点",
            "可降级校验",
            "坐标超出页面",
            "教材页码漂移",
            "章节号漂移",
            "超出页面边界",
            "起始坐标越过",
            "页面标题可能缺失",
            "电子书课件",
        ]
        return any(marker in str(error or "") for marker in degradable_markers)

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

    def _build_safe_fallback_slide_code(
        self,
        *,
        slide: SlideOutline,
        last_error: str = "",
        content_requirements: str = "",
    ) -> str:
        title = self._fallback_text(slide.topic or f"第 {slide.slide_index + 1} 页", 44)
        objective = self._fallback_text(
            slide.objective or "梳理本页主题的关键概念、依据与课堂讨论方向。",
            120,
        )
        source_hint = self._extract_fallback_source_hint(content_requirements)
        review_hint = "先确认核心概念，再补充课堂讲解或练习。"
        body_lines = [objective]
        if source_hint:
            body_lines.append(source_hint)
        body_lines.append(review_hint)
        body = "\n".join(f"- {line}" for line in body_lines if line)

        return "\n".join(
            [
                "{",
                "let slide = pres.addSlide();",
                'slide.background = { color: "F8FAFC" };',
                'slide.addShape("rect", { x: 0, y: 0, w: 13.333, h: 0.28, fill: { color: "2563EB" }, line: { color: "2563EB" } });',
                'slide.addText('
                + json.dumps(title, ensure_ascii=False)
                + ', { x: 0.75, y: 0.62, w: 11.8, h: 0.72, fontFace: "Microsoft YaHei", fontSize: 28, bold: true, color: "0F172A", margin: 0.02, breakLine: false, fit: "shrink" });',
                'slide.addShape("rect", { x: 0.78, y: 1.65, w: 11.78, h: 3.95, fill: { color: "FFFFFF", transparency: 0 }, line: { color: "CBD5E1", transparency: 15 } });',
                'slide.addText("本页要点", { x: 1.08, y: 1.95, w: 3.2, h: 0.42, fontFace: "Microsoft YaHei", fontSize: 18, bold: true, color: "2563EB", margin: 0.02, lineSpacingMultiple: 1.25 });',
                'slide.addText('
                + json.dumps(body, ensure_ascii=False)
                + ', { x: 1.08, y: 2.55, w: 11.0, h: 2.35, fontFace: "Microsoft YaHei", fontSize: 18, color: "1E293B", breakLine: false, fit: "shrink", valign: "mid", margin: 0.04, lineSpacingMultiple: 1.25, paraSpaceAfterPt: 8 });',
                'slide.addShape("line", { x: 1.08, y: 5.88, w: 11.0, h: 0, line: { color: "CBD5E1", width: 1 } });',
                'slide.addText("课堂提示：围绕本页主题提炼 1 个关键判断，并说明依据。", { x: 1.08, y: 6.15, w: 11.0, h: 0.48, fontFace: "Microsoft YaHei", fontSize: 14, color: "475569", margin: 0.02, fit: "shrink", lineSpacingMultiple: 1.25 });',
                "}",
            ]
        )

    @classmethod
    def _extract_fallback_source_hint(cls, content_requirements: str) -> str:
        markers = ("教材页码", "教材依据", "原文依据", "source_pages", "source pages", "页码范围")
        for raw_line in str(content_requirements or "").splitlines():
            line = raw_line.strip().lstrip("-").strip()
            if not line or not any(marker in line for marker in markers):
                continue
            line = re.sub(r"\s+", " ", line)
            return cls._fallback_text(line, 130)
        return ""

    @staticmethod
    def _fallback_text(value: str, limit: int) -> str:
        cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: max(1, limit - 3)] + "..."

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
        if normalized_slides:
            last_slide = normalized_slides[-1]
            if last_slide.get("layout") != "closing" and self._looks_like_closing_outline_slide(last_slide):
                last_slide["layout"] = "closing"

        normalized = {
            "title": data.get("title") or topic,
            "topic": data.get("topic") or topic,
            "slides": normalized_slides,
        }
        outline = OutlinePlan.model_validate(normalized)
        self._validate_outline_structure(outline)
        return outline

    @staticmethod
    def _looks_like_closing_outline_slide(slide: dict[str, Any]) -> bool:
        text = f"{slide.get('topic') or ''}\n{slide.get('objective') or ''}"
        return any(marker in text for marker in ("小结", "总结", "回顾", "收束", "下节", "下一课", "预告", "结语"))

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

    @staticmethod
    def _validate_outline_slide_count(outline: OutlinePlan, *, min_slides: int, max_slides: int) -> None:
        count = len(outline.slides)
        if count < min_slides or count > max_slides:
            if min_slides == max_slides:
                raise ValueError(f"大纲页数必须等于 {max_slides} 页，当前 {count} 页")
            raise ValueError(f"大纲页数必须在 {min_slides}-{max_slides} 页之间，当前 {count} 页")

    @classmethod
    def _validate_outline_study_focus_required_pages(
        cls,
        outline: OutlinePlan,
        content_requirements: str,
    ) -> None:
        required_topics = cls._required_study_focus_outline_topics(content_requirements)
        if not required_topics:
            return
        slide_focus_texts = [
            " ".join(
                part
                for part in (
                    str(slide.topic or ""),
                    str(slide.objective or ""),
                )
                if part
            )
            for slide in outline.slides
        ]
        missing: list[str] = []
        for topic in required_topics:
            topic_key = cls._normalize_book_metadata_text(topic)
            if not topic_key:
                continue
            if not any(
                "考纲" in text and topic_key in cls._normalize_book_metadata_text(text)
                for text in slide_focus_texts
            ):
                missing.append(topic)
        if missing:
            raise ValueError(
                "大纲缺少优先呈现的考纲对照页："
                + "、".join(missing)
                + "。请在“考纲要求/考纲对照”页的 topic 或 objective 中覆盖这些知识点；"
                + "页数紧张时允许一个考纲对照页合并多个知识点，但必须写出知识点名称。"
            )

    @classmethod
    def _required_study_focus_outline_topics(cls, content_requirements: str) -> list[str]:
        section = cls._markdown_section(content_requirements, "本课必须显式呈现的考纲对照页")
        topics: list[str] = []
        for raw_line in section.splitlines():
            match = re.match(r"页面标题建议\s*[:：]\s*(?:考纲要求|考纲对照)\s*[:：]\s*(.+)$", raw_line.strip())
            if not match:
                continue
            topic = re.sub(r"[（(].*?[）)]", "", match.group(1)).strip()
            if topic and topic not in topics:
                topics.append(topic)
        return topics

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
            return self._coerce_extracted_code(m.group(1))

        m = re.search(r"<code>(.*)$", raw, re.DOTALL)
        if m:
            return self._coerce_extracted_code(m.group(1))

        m = re.search(r"```[ \t]*(?:javascript|js)(?:[ \t\r\n]+)(.*?)```", raw, re.DOTALL | re.IGNORECASE)
        if m:
            return self._coerce_extracted_code(m.group(1))

        m = re.search(r"```[ \t]*(?:javascript|js)(?:[ \t\r\n]+)(.*)$", raw, re.DOTALL | re.IGNORECASE)
        if m:
            return self._coerce_extracted_code(m.group(1))

        m = re.search(r"```\s*(.*?)```", raw, re.DOTALL)
        if m:
            return self._coerce_extracted_code(m.group(1))

        m = re.search(r"```\s*(.*)$", raw, re.DOTALL)
        if m:
            return self._coerce_extracted_code(m.group(1))

        raise ValueError("LLM 响应中未找到 <code> 或 ```javascript 代码块")

    @classmethod
    def _coerce_extracted_code(cls, value: str) -> str:
        code = str(value or "").strip()
        if not code:
            return code

        lines = code.splitlines()
        if lines and lines[0].strip().lower() in {"json", "javascript", "js"}:
            code = "\n".join(lines[1:]).strip()

        json_code = cls._code_from_json_payload(code)
        if json_code:
            return json_code

        quoted_lines = cls._code_from_quoted_line_collection(code)
        if quoted_lines:
            return quoted_lines

        return code

    @classmethod
    def _code_from_json_payload(cls, code: str) -> str:
        try:
            payload = json.loads(code)
        except Exception:
            return ""
        extracted = cls._extract_code_value_from_json(payload)
        return extracted.strip() if extracted else ""

    @classmethod
    def _extract_code_value_from_json(cls, payload) -> str:
        if isinstance(payload, str):
            return payload
        if isinstance(payload, list) and all(isinstance(item, str) for item in payload):
            return "\n".join(payload)
        if isinstance(payload, dict):
            for key in (
                "code",
                "js",
                "javascript",
                "slide_code",
                "pptx_code",
                "content",
            ):
                if key in payload:
                    value = cls._extract_code_value_from_json(payload[key])
                    if value:
                        return value
        return ""

    @classmethod
    def _code_from_quoted_line_collection(cls, code: str) -> str:
        cleaned = code.strip()
        if len(cleaned) < 4 or cleaned[0] not in "{[" or cleaned[-1] not in "}]":
            return ""
        inner = cleaned[1:-1].strip()
        if not inner:
            return ""

        lines: list[str] = []
        for raw_line in inner.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.endswith(","):
                line = line[:-1].rstrip()
            if not line:
                continue
            if not (
                (line.startswith('"') and line.endswith('"'))
                or (line.startswith("'") and line.endswith("'"))
            ):
                return ""
            decoded = cls._decode_quoted_code_line(line)
            if decoded is None:
                return ""
            lines.append(decoded)

        joined = "\n".join(lines).strip()
        if not joined:
            return ""
        if not any(token in joined for token in ("slide.", "pres.", "pptxgen", "addText", "addShape")):
            return ""
        return joined

    @staticmethod
    def _decode_quoted_code_line(line: str) -> str | None:
        if line.startswith('"'):
            try:
                return json.loads(line)
            except Exception:
                return None
        if line.startswith("'"):
            inner = line[1:-1]
            return (
                inner.replace(r"\\", "\\")
                .replace(r"\'", "'")
                .replace(r'\"', '"')
                .replace(r"\n", "\n")
                .replace(r"\t", "\t")
            )
        return None

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

    @classmethod
    def _set_or_insert_numeric_option(cls, options_literal: str, key: str, value: float) -> str:
        pattern = re.compile(rf"(\b{re.escape(key)}\s*:\s*)({cls._geometry_number_pattern()})")
        if pattern.search(options_literal):
            return pattern.sub(
                lambda match: f"{match.group(1)}{cls._format_geometry_number(value)}",
                options_literal,
                count=1,
            )
        close_index = options_literal.rfind("}")
        if close_index < 0:
            return options_literal
        before = options_literal[:close_index].rstrip()
        after = options_literal[close_index:]
        separator = "" if before.endswith("{") else ","
        return f"{before}{separator} {key}: {cls._format_geometry_number(value)}{after}"

    @classmethod
    def _set_or_replace_numeric_option_expression(cls, options_literal: str, key: str, value: float) -> str:
        pattern = re.compile(rf"(\b{re.escape(key)}\s*:\s*)([^,\n}}]+)")
        if pattern.search(options_literal):
            return pattern.sub(
                lambda match: f"{match.group(1)}{cls._format_geometry_number(value)}",
                options_literal,
                count=1,
            )
        return cls._set_or_insert_numeric_option(options_literal, key, value)

    @classmethod
    def _iter_addtext_style_blocks_with_ranges(cls, code: str) -> list[tuple[str, int, int, bool]]:
        blocks: list[tuple[str, int, int, bool]] = []
        for match in re.finditer(r"\.addText\s*\(", code):
            open_paren = code.find("(", match.start())
            if open_paren < 0:
                continue
            call_segment = cls._scan_balanced_segment(code, open_paren, "(", ")")
            if not call_segment:
                continue
            call_body, _ = call_segment
            object_literals: list[tuple[str, int, int]] = []
            cursor = 0
            while True:
                brace_index = call_body.find("{", cursor)
                if brace_index < 0:
                    break
                block = cls._scan_balanced_segment(call_body, brace_index, "{", "}")
                if not block:
                    break
                literal, next_index = block
                absolute_start = open_paren + brace_index
                object_literals.append((literal, absolute_start, absolute_start + len(literal)))
                cursor = next_index
            if not object_literals:
                continue
            options_literal, start_index, end_index = object_literals[-1]
            blocks.append((options_literal, start_index, end_index, True))
            for literal, literal_start, literal_end in object_literals[:-1]:
                if re.search(r"\bfontSize\s*:", literal):
                    blocks.append((literal, literal_start, literal_end, False))
        return blocks

    def _normalize_book_readability_styles(self, code: str) -> str:
        if not self._layout_qa_enabled():
            return code
        replacements: list[tuple[int, int, str]] = []
        for options, start_index, end_index, is_text_options in self._iter_addtext_style_blocks_with_ranges(code):
            updated = options
            font_size = self._extract_numeric_option(updated, "fontSize")
            if font_size is not None and font_size < 14:
                updated = self._set_or_insert_numeric_option(updated, "fontSize", 14)
                font_size = 14
            if is_text_options and font_size is not None and font_size <= 18:
                updated = self._set_or_insert_numeric_option(updated, "lineSpacingMultiple", 1.25)
            if updated != options:
                replacements.append((start_index, end_index, updated))
        for start_index, end_index, replacement in sorted(replacements, reverse=True):
            code = code[:start_index] + replacement + code[end_index:]
        return code

    def _normalize_book_readability_geometry(self, code: str) -> str:
        if not self._layout_qa_enabled():
            return code
        for _ in range(4):
            before = code
            code = self._normalize_book_readability_shape_draw_order(code)
            code = self._repair_book_readability_undefined_geometry_expressions(code)
            code = self._normalize_book_readability_dynamic_geometry_constraints(code)
            code = self._normalize_book_readability_text_box_heights(code)
            code = self._normalize_book_readability_bottom_safe_geometry(code)
            code = self._normalize_book_readability_wrapper_geometry(code)
            code = self._normalize_book_readability_wrapper_spacing_geometry(code)
            code = self._normalize_book_readability_text_overlap_geometry(code)
            code = self._normalize_book_readability_connector_geometry(code)
            code = self._normalize_book_readability_decorative_geometry(code)
            if code == before:
                break
        return self._normalize_geometry_constraints(code)

    def _normalize_book_readability_shape_draw_order(self, code: str) -> str:
        constants = self._readability_numeric_constants(code)
        text_records: list[dict[str, Any]] = []
        for text, options, start_index, end_index in self._iter_addtext_readability_blocks_with_ranges(code):
            if self._is_readability_decorative_text(text) or self._is_book_readability_auxiliary_text(text, options, constants):
                continue
            text_statement = self._statement_range_around_call(code, start_index)
            if text_statement is None:
                continue
            x = self._extract_readability_numeric_option(options, "x", constants)
            y = self._extract_readability_numeric_option(options, "y", constants)
            w = self._extract_readability_numeric_option(options, "w", constants)
            h = self._extract_readability_numeric_option(options, "h", constants)
            metrics = self._readability_required_text_height(text, options, constants)
            if None in (x, y, w, h) or metrics is None:
                continue
            assert x is not None and y is not None and w is not None and h is not None
            _font_size, _estimated_lines, required_height = metrics
            text_records.append(
                {
                    "start": text_statement[0],
                    "box": (x, y, w, max(h, required_height)),
                }
            )
        if not text_records:
            return code

        moves: list[tuple[int, int, int]] = []
        for call_name, options, shape_kind, options_start, _options_end in self._iter_geometry_option_blocks_with_ranges(code):
            if call_name != "addShape":
                continue
            kind = self._normalized_shape_kind(shape_kind)
            if not kind or kind == "line" or kind.endswith("line"):
                continue
            if self._extract_fill_color_option(options) is None:
                continue
            x = self._extract_readability_numeric_option(options, "x", constants)
            y = self._extract_readability_numeric_option(options, "y", constants)
            w = self._extract_readability_numeric_option(options, "w", constants)
            h = self._extract_readability_numeric_option(options, "h", constants)
            if None in (x, y, w, h):
                continue
            assert x is not None and y is not None and w is not None and h is not None
            shape_statement = self._statement_range_around_call(code, options_start)
            if shape_statement is None:
                continue
            shape_start, shape_end = shape_statement
            shape_box = (x, y, w, h)
            overlapping_text_starts = [
                int(record["start"])
                for record in text_records
                if int(record["start"]) < shape_start
                and self._readability_box_overlap_ratio(record["box"], shape_box) >= 0.18
            ]
            if overlapping_text_starts:
                moves.append((shape_start, shape_end, min(overlapping_text_starts)))
        if not moves:
            return code

        for shape_start, shape_end, insert_start in sorted(moves, reverse=True):
            statement = code[shape_start:shape_end]
            code = code[:shape_start] + code[shape_end:]
            code = code[:insert_start] + statement.rstrip() + "\n" + code[insert_start:]
        return code

    @classmethod
    def _statement_range_around_call(cls, code: str, options_start: int) -> tuple[int, int] | None:
        call_start = code.rfind("slide.", 0, options_start)
        if call_start < 0:
            return None
        open_paren = code.find("(", call_start)
        if open_paren < 0 or open_paren > options_start:
            return None
        segment = cls._scan_balanced_segment(code, open_paren, "(", ")")
        if not segment:
            return None
        _call_body, close_index = segment
        statement_end = close_index
        while statement_end < len(code) and code[statement_end].isspace() and code[statement_end] != "\n":
            statement_end += 1
        if statement_end < len(code) and code[statement_end] == ";":
            statement_end += 1
        if statement_end < len(code) and code[statement_end] == "\n":
            statement_end += 1
        line_start = code.rfind("\n", 0, call_start) + 1
        return line_start, statement_end

    def _normalize_book_readability_text_box_heights(self, code: str) -> str:
        constants = self._readability_numeric_constants(code)
        slide_height = float(config.SLIDE_HEIGHT_INCH)
        slide_bottom = slide_height - 0.46
        replacements: list[tuple[int, int, str]] = []
        for text, options, start_index, end_index in self._iter_addtext_readability_blocks_with_ranges(code):
            if self._is_readability_decorative_text(text) or self._is_book_readability_auxiliary_text(text, options, constants):
                continue
            y = self._extract_readability_numeric_option(options, "y", constants)
            current_height = self._extract_readability_numeric_option(options, "h", constants)
            metrics = self._readability_required_text_height(text, options, constants)
            if current_height is None or metrics is None:
                continue
            _font_size, _estimated_lines, required_height = metrics
            target_height = max(required_height + 0.035, 0.3)
            excessive_height = (
                current_height > target_height + 1.5
                and current_height > target_height * 2.2
            )
            safe_area_overflow = y is not None and y + current_height > slide_bottom + 0.015
            if current_height > slide_height * 1.2 or (safe_area_overflow and excessive_height):
                updated = self._set_or_replace_numeric_option_expression(
                    options,
                    "h",
                    min(target_height, max(0.3, slide_height - 0.65)),
                )
                if updated != options:
                    replacements.append((start_index, end_index, updated))
                continue
            if current_height >= target_height - 0.015:
                continue
            updated = self._set_or_replace_numeric_option_expression(options, "h", target_height)
            if updated != options:
                replacements.append((start_index, end_index, updated))
        for start_index, end_index, replacement in sorted(replacements, reverse=True):
            code = code[:start_index] + replacement + code[end_index:]
        return code

    def _normalize_book_readability_bottom_safe_geometry(self, code: str) -> str:
        constants = self._readability_numeric_constants(code)
        slide_bottom = float(config.SLIDE_HEIGHT_INCH) - 0.46
        replacements: list[tuple[int, int, str]] = []
        for text, options, start_index, end_index in self._iter_addtext_readability_blocks_with_ranges(code):
            if self._is_readability_decorative_text(text) or self._is_book_readability_auxiliary_text(text, options, constants):
                continue
            x = self._extract_readability_numeric_option(options, "x", constants)
            y = self._extract_readability_numeric_option(options, "y", constants)
            h = self._extract_readability_numeric_option(options, "h", constants)
            metrics = self._readability_required_text_height(text, options, constants)
            if None in (x, y, h) or metrics is None:
                continue
            assert y is not None and h is not None
            _font_size, _estimated_lines, required_height = metrics
            effective_h = max(h, required_height)
            overflow = y + effective_h - slide_bottom
            if overflow <= 0.015:
                continue
            target_y = max(0.55, y - overflow - 0.03)
            if abs(target_y - y) <= 0.01:
                continue
            updated = self._set_or_replace_numeric_option_expression(options, "y", target_y)
            if updated != options:
                replacements.append((start_index, end_index, updated))
        for start_index, end_index, replacement in sorted(replacements, reverse=True):
            code = code[:start_index] + replacement + code[end_index:]
        return code

    def _normalize_book_readability_wrapper_geometry(self, code: str) -> str:
        constants = self._readability_numeric_constants(code)
        wrapper_shapes = self._book_readability_wrapper_shapes_with_ranges(code, constants)
        if not wrapper_shapes:
            return code
        shape_updates: dict[int, tuple[float, float, float, float]] = {}
        for text, options, _start_index, _end_index in self._iter_addtext_readability_blocks_with_ranges(code):
            if self._is_book_readability_auxiliary_text(text, options, constants):
                continue
            x = self._extract_readability_numeric_option(options, "x", constants)
            y = self._extract_readability_numeric_option(options, "y", constants)
            w = self._extract_readability_numeric_option(options, "w", constants)
            h = self._extract_readability_numeric_option(options, "h", constants)
            metrics = self._readability_required_text_height(text, options, constants)
            if None in (x, y, w, h) or metrics is None:
                continue
            assert x is not None and y is not None and w is not None and h is not None
            if w <= 0.05 or h <= 0.05:
                continue
            _font_size, _estimated_lines, required_height = metrics
            effective_h = max(h, required_height)
            text_box = (x, y, w, effective_h)
            text_center = (x + w / 2.0, y + effective_h / 2.0)
            candidates: list[tuple[float, int, tuple[float, float, float, float]]] = []
            for shape_index, (shape_box, _kind, _options_literal, _shape_start, _shape_end) in enumerate(wrapper_shapes):
                sx, sy, sw, sh = shape_box
                center_inside = sx - 0.03 <= text_center[0] <= sx + sw + 0.03 and sy - 0.03 <= text_center[1] <= sy + sh + 0.03
                overlap_ratio = self._readability_box_overlap_ratio(text_box, (sx, sy, sw, sh))
                if center_inside or overlap_ratio >= 0.35:
                    candidates.append((sw * sh, shape_index, (sx, sy, sw, sh)))
            if not candidates:
                continue
            _area, shape_index, original_shape_box = min(candidates, key=lambda item: item[0])
            sx, sy, sw, sh = shape_updates.get(shape_index, original_shape_box)
            pad = 0.08
            target_left = min(sx, x - pad)
            target_top = min(sy, y - pad)
            target_right = max(sx + sw, x + w + pad)
            target_bottom = max(sy + sh, y + effective_h + pad)
            slide_width = float(config.SLIDE_WIDTH_INCH)
            slide_height = float(config.SLIDE_HEIGHT_INCH)
            target_left = self._clamp_geometry_value(target_left, 0.0, max(0.0, slide_width - 0.1))
            target_top = self._clamp_geometry_value(target_top, 0.0, max(0.0, slide_height - 0.1))
            target_right = self._clamp_geometry_value(target_right, target_left + 0.1, slide_width)
            target_bottom = self._clamp_geometry_value(target_bottom, target_top + 0.1, slide_height)
            if (
                abs(target_left - sx) > 0.01
                or abs(target_top - sy) > 0.01
                or abs((target_right - target_left) - sw) > 0.01
                or abs((target_bottom - target_top) - sh) > 0.01
            ):
                shape_updates[shape_index] = (
                    target_left,
                    target_top,
                    target_right - target_left,
                    target_bottom - target_top,
                )
        replacements: list[tuple[int, int, str]] = []
        for shape_index, box in shape_updates.items():
            _shape_box, _kind, options_literal, start_index, end_index = wrapper_shapes[shape_index]
            updated = options_literal
            for key, value in {"x": box[0], "y": box[1], "w": box[2], "h": box[3]}.items():
                updated = self._set_or_replace_numeric_option_expression(updated, key, value)
            if updated != options_literal:
                replacements.append((start_index, end_index, updated))
        for start_index, end_index, replacement in sorted(replacements, reverse=True):
            code = code[:start_index] + replacement + code[end_index:]
        return code

    def _normalize_book_readability_wrapper_spacing_geometry(self, code: str) -> str:
        constants = self._readability_numeric_constants(code)
        wrapper_shapes = self._book_readability_wrapper_shapes_with_ranges(code, constants)
        if not wrapper_shapes:
            return code
        records: list[dict[str, Any]] = []
        for shape_index, (shape_box, kind, options_literal, start_index, end_index) in enumerate(wrapper_shapes):
            if any(token in kind for token in ("ellipse", "oval")):
                continue
            x, y, w, h = shape_box
            if w * h <= 0.18:
                continue
            records.append(
                {
                    "shape_index": shape_index,
                    "box": shape_box,
                    "kind": kind,
                    "options": options_literal,
                    "start": start_index,
                    "end": end_index,
                    "texts": [],
                }
            )
        if len(records) < 2:
            return code

        for text, options, start_index, end_index in self._iter_addtext_readability_blocks_with_ranges(code):
            if self._is_book_readability_auxiliary_text(text, options, constants):
                continue
            x = self._extract_readability_numeric_option(options, "x", constants)
            y = self._extract_readability_numeric_option(options, "y", constants)
            w = self._extract_readability_numeric_option(options, "w", constants)
            h = self._extract_readability_numeric_option(options, "h", constants)
            metrics = self._readability_required_text_height(text, options, constants)
            if None in (x, y, w, h) or metrics is None:
                continue
            assert x is not None and y is not None and w is not None and h is not None
            _font_size, _estimated_lines, required_height = metrics
            effective_h = max(h, required_height)
            text_box = (x, y, w, effective_h)
            text_center = (x + w / 2.0, y + effective_h / 2.0)
            candidates: list[tuple[float, dict[str, Any]]] = []
            for record in records:
                sx, sy, sw, sh = record["box"]
                center_inside = sx - 0.03 <= text_center[0] <= sx + sw + 0.03 and sy - 0.03 <= text_center[1] <= sy + sh + 0.03
                overlap_ratio = self._readability_box_overlap_ratio(text_box, record["box"])
                if center_inside or overlap_ratio >= 0.35:
                    candidates.append((sw * sh, record))
            if not candidates:
                continue
            _area, record = min(candidates, key=lambda item: item[0])
            record["texts"].append((options, start_index, end_index))

        used_records = [record for record in records if record["texts"]]
        if len(used_records) < 2:
            return code

        min_gap = 0.09
        slide_left = 0.18
        slide_right = float(config.SLIDE_WIDTH_INCH) - 0.18
        slide_bottom = float(config.SLIDE_HEIGHT_INCH) - 0.28
        shifts: dict[int, tuple[float, float]] = {}

        def shifted_box(record: dict[str, Any]) -> tuple[float, float, float, float]:
            shape_index = int(record["shape_index"])
            dx, dy = shifts.get(shape_index, (0.0, 0.0))
            x, y, w, h = record["box"]
            return x + dx, y + dy, w, h

        def add_shift(record: dict[str, Any], dx: float, dy: float) -> None:
            if abs(dx) <= 0.002 and abs(dy) <= 0.002:
                return
            shape_index = int(record["shape_index"])
            old_dx, old_dy = shifts.get(shape_index, (0.0, 0.0))
            shifts[shape_index] = (old_dx + dx, old_dy + dy)

        def mostly_same_row(left_box: tuple[float, float, float, float], right_box: tuple[float, float, float, float]) -> bool:
            lx, ly, lw, lh = left_box
            rx, ry, rw, rh = right_box
            overlap_h = min(ly + lh, ry + rh) - max(ly, ry)
            return overlap_h / max(0.01, min(lh, rh)) >= 0.35

        def mostly_same_column(left_box: tuple[float, float, float, float], right_box: tuple[float, float, float, float]) -> bool:
            lx, ly, lw, lh = left_box
            rx, ry, rw, rh = right_box
            overlap_w = min(lx + lw, rx + rw) - max(lx, rx)
            return overlap_w / max(0.01, min(lw, rw)) >= 0.35

        for _pass in range(8):
            changed = False
            ordered_records = sorted(used_records, key=lambda item: (shifted_box(item)[1], shifted_box(item)[0]))
            for left_index, left_record in enumerate(ordered_records):
                lx, ly, lw, lh = shifted_box(left_record)
                left_box = (lx, ly, lw, lh)
                for right_record in ordered_records[left_index + 1 :]:
                    rx, ry, rw, rh = shifted_box(right_record)
                    right_box = (rx, ry, rw, rh)
                    overlap_w = min(lx + lw, rx + rw) - max(lx, rx)
                    overlap_h = min(ly + lh, ry + rh) - max(ly, ry)
                    horizontal_gap = max(rx - (lx + lw), lx - (rx + rw), 0.0)
                    vertical_gap = max(ry - (ly + lh), ly - (ry + rh), 0.0)
                    collides = overlap_w > 0.03 and overlap_h > 0.03
                    too_close_h = mostly_same_row(left_box, right_box) and 0 < horizontal_gap < min_gap
                    too_close_v = mostly_same_column(left_box, right_box) and 0 < vertical_gap < min_gap
                    if not (collides or too_close_h or too_close_v):
                        continue

                    dx_needed = 0.0
                    dy_needed = 0.0
                    if mostly_same_row(left_box, right_box):
                        if rx >= lx:
                            dx_needed = lx + lw + min_gap - rx
                        else:
                            dx_needed = rx + rw + min_gap - lx
                    if mostly_same_column(left_box, right_box):
                        if ry >= ly:
                            dy_needed = ly + lh + min_gap - ry
                        else:
                            dy_needed = ry + rh + min_gap - ly
                    if collides:
                        if dx_needed <= 0:
                            dx_needed = overlap_w + min_gap
                        if dy_needed <= 0:
                            dy_needed = overlap_h + min_gap

                    candidate = right_record if (rx >= lx or ry >= ly) else left_record
                    cx, cy, cw, ch = shifted_box(candidate)
                    moved = False
                    if dx_needed > 0 and cx + dx_needed + cw <= slide_right:
                        add_shift(candidate, dx_needed, 0.0)
                        moved = True
                    elif dy_needed > 0 and cy + dy_needed + ch <= slide_bottom:
                        add_shift(candidate, 0.0, dy_needed)
                        moved = True
                    elif dx_needed > 0 and candidate is right_record and lx - dx_needed >= slide_left:
                        add_shift(left_record, -dx_needed, 0.0)
                        moved = True
                    elif dy_needed > 0 and candidate is right_record and ly - dy_needed >= 0.5:
                        add_shift(left_record, 0.0, -dy_needed)
                        moved = True
                    if moved:
                        changed = True
                        break
                if changed:
                    break
            if not changed:
                break

        if not shifts:
            return code

        text_shifts: dict[tuple[int, int], tuple[str, float, float]] = {}
        shape_replacements: list[tuple[int, int, str]] = []
        for record in used_records:
            shape_index = int(record["shape_index"])
            delta_x, delta_y = shifts.get(shape_index, (0.0, 0.0))
            if abs(delta_x) <= 0.002 and abs(delta_y) <= 0.002:
                continue
            sx, sy, _sw, _sh = record["box"]
            updated_options = record["options"]
            if abs(delta_x) > 0.002:
                updated_options = self._set_or_replace_numeric_option_expression(updated_options, "x", sx + delta_x)
            if abs(delta_y) > 0.002:
                updated_options = self._set_or_replace_numeric_option_expression(updated_options, "y", sy + delta_y)
            if updated_options != record["options"]:
                shape_replacements.append((record["start"], record["end"], updated_options))
            for text_options, text_start, text_end in record["texts"]:
                old_options, old_dx, old_dy = text_shifts.get((text_start, text_end), (text_options, 0.0, 0.0))
                text_shifts[(text_start, text_end)] = (old_options, old_dx + delta_x, old_dy + delta_y)

        replacements: list[tuple[int, int, str]] = list(shape_replacements)
        for (text_start, text_end), (text_options, delta_x, delta_y) in text_shifts.items():
            text_y = self._extract_readability_numeric_option(text_options, "y", constants)
            text_x = self._extract_readability_numeric_option(text_options, "x", constants)
            if text_y is None or text_x is None:
                continue
            if abs(delta_x) <= 0.002 and abs(delta_y) <= 0.002:
                continue
            updated_options = text_options
            if abs(delta_x) > 0.002:
                updated_options = self._set_or_replace_numeric_option_expression(updated_options, "x", text_x + delta_x)
            if abs(delta_y) > 0.002:
                updated_options = self._set_or_replace_numeric_option_expression(updated_options, "y", text_y + delta_y)
            if updated_options != text_options:
                replacements.append((text_start, text_end, updated_options))
        for start_index, end_index, replacement in sorted(replacements, reverse=True):
            code = code[:start_index] + replacement + code[end_index:]
        return code

    def _normalize_book_readability_connector_geometry(self, code: str) -> str:
        constants = self._readability_numeric_constants(code)
        text_boxes = self._book_readability_visible_text_boxes(code, constants)
        if not text_boxes:
            return code
        removals: list[tuple[int, int]] = []
        for call_name, options, shape_kind, options_start, _options_end in self._iter_geometry_option_blocks_with_ranges(code):
            if call_name != "addShape" or self._normalized_shape_kind(shape_kind) != "line":
                continue
            x = self._extract_readability_numeric_option(options, "x", constants)
            y = self._extract_readability_numeric_option(options, "y", constants)
            w = self._extract_readability_numeric_option(options, "w", constants)
            h = self._extract_readability_numeric_option(options, "h", constants)
            if None in (x, y, w, h):
                continue
            assert x is not None and y is not None and w is not None and h is not None
            if math.hypot(w, h) < 0.35:
                continue
            long_diagonal = abs(w) >= 0.35 and abs(h) >= 0.35 and math.hypot(w, h) >= 0.75
            crosses_text = any(
                self._segment_intersects_box_interior(x, y, x + w, y + h, box)
                for box, _sample in text_boxes
            )
            if not long_diagonal and not crosses_text:
                continue
            statement = self._statement_range_around_call(code, options_start)
            if statement is not None:
                removals.append(statement)
        for start_index, end_index in sorted(set(removals), reverse=True):
            code = code[:start_index] + code[end_index:]
        return code

    def _normalize_book_readability_decorative_geometry(self, code: str) -> str:
        constants = self._readability_numeric_constants(code)
        removals: list[tuple[int, int]] = []
        for call_name, options, shape_kind, options_start, _options_end in self._iter_geometry_option_blocks_with_ranges(code):
            if call_name != "addShape":
                continue
            kind = self._normalized_shape_kind(shape_kind)
            if not kind or kind == "line" or kind.endswith("line"):
                continue
            x = self._extract_readability_numeric_option(options, "x", constants)
            y = self._extract_readability_numeric_option(options, "y", constants)
            w = self._extract_readability_numeric_option(options, "w", constants)
            h = self._extract_readability_numeric_option(options, "h", constants)
            if None in (x, y, w, h):
                continue
            assert x is not None and y is not None and w is not None and h is not None
            if w <= 0.05 or h <= 0.05 or w >= 10.5 or h >= 6.8:
                continue
            is_badge = any(token in kind for token in ("ellipse", "oval")) and w <= 1.25 and h <= 1.25
            is_small_label = any(token in kind for token in ("rect", "roundrect")) and w <= 2.2 and h <= 0.75
            if not (is_badge or is_small_label):
                continue
            shape_box = (x, y, w, h)
            shape_area = w * h
            for text, text_options, _text_start in self._iter_addtext_readability_blocks(code):
                if self._is_book_readability_auxiliary_text(text, text_options, constants):
                    continue
                tx = self._extract_readability_numeric_option(text_options, "x", constants)
                ty = self._extract_readability_numeric_option(text_options, "y", constants)
                tw = self._extract_readability_numeric_option(text_options, "w", constants)
                th = self._extract_readability_numeric_option(text_options, "h", constants)
                if None in (tx, ty, tw, th):
                    continue
                assert tx is not None and ty is not None and tw is not None and th is not None
                if tw <= 0.05 or th <= 0.05:
                    continue
                text_center_x = tx + tw / 2.0
                text_center_y = ty + th / 2.0
                if x - 0.03 <= text_center_x <= x + w + 0.03 and y - 0.03 <= text_center_y <= y + h + 0.03:
                    continue
                overlap_w, overlap_h, overlap_area = self._book_readability_box_overlap(shape_box, (tx, ty, tw, th))
                if overlap_area <= 0:
                    continue
                overlap_ratio = overlap_area / min(shape_area, tw * th)
                if overlap_ratio < 0.12 or overlap_w < 0.04 or overlap_h < 0.04:
                    continue
                statement = self._statement_range_around_call(code, options_start)
                if statement is not None:
                    removals.append(statement)
                break
        for start_index, end_index in sorted(set(removals), reverse=True):
            code = code[:start_index] + code[end_index:]
        return code

    def _normalize_book_readability_text_overlap_geometry(self, code: str) -> str:
        constants = self._readability_numeric_constants(code)
        records: list[dict[str, Any]] = []
        slide_bottom = float(config.SLIDE_HEIGHT_INCH) - 0.38
        for text, options, start_index, end_index in self._iter_addtext_readability_blocks_with_ranges(code):
            if self._is_readability_decorative_text(text):
                continue
            x = self._extract_readability_numeric_option(options, "x", constants)
            y = self._extract_readability_numeric_option(options, "y", constants)
            w = self._extract_readability_numeric_option(options, "w", constants)
            h = self._extract_readability_numeric_option(options, "h", constants)
            compact = re.sub(r"\s+", "", text or "")
            lowered = compact.lower()
            if y is not None and (
                y >= 6.65
                or "来源" in compact
                or "source" in lowered
                or "教材" in compact
                or re.search(r"\bp\.?\d", lowered)
            ):
                continue
            metrics = self._readability_required_text_height(text, options, constants)
            if None in (x, y, w, h) or metrics is None:
                continue
            assert x is not None and y is not None and w is not None and h is not None
            _font_size, _estimated_lines, required_height = metrics
            effective_h = max(h, required_height)
            if w <= 0.05 or effective_h <= 0.05:
                continue
            records.append(
                {
                    "text": text,
                    "options": options,
                    "start": start_index,
                    "end": end_index,
                    "box": (x, y, w, effective_h),
                }
            )
        if len(records) < 2:
            return code

        shifts: dict[int, float] = {}
        min_gap = 0.08
        ordered_indices = sorted(range(len(records)), key=lambda idx: (records[idx]["box"][1], records[idx]["box"][0]))
        for pos, first_index in enumerate(ordered_indices):
            ax, ay, aw, ah = records[first_index]["box"]
            ay += shifts.get(first_index, 0.0)
            for second_index in ordered_indices[pos + 1:]:
                bx, by, bw, bh = records[second_index]["box"]
                by += shifts.get(second_index, 0.0)
                horizontal_overlap = min(ax + aw, bx + bw) - max(ax, bx)
                vertical_overlap = min(ay + ah, by + bh) - max(ay, by)
                if horizontal_overlap <= 0 or vertical_overlap <= 0.015:
                    continue
                horizontal_ratio = horizontal_overlap / max(0.01, min(aw, bw))
                if horizontal_ratio < 0.22:
                    continue
                move_index = second_index if by >= ay else first_index
                mx, my, mw, mh = records[move_index]["box"]
                current_shift = shifts.get(move_index, 0.0)
                target_top = (ay + ah + min_gap) if move_index == second_index else (by + bh + min_gap)
                delta = max(0.0, target_top - (my + current_shift))
                if delta <= 0.0:
                    continue
                if my + current_shift + delta + mh <= slide_bottom:
                    shifts[move_index] = current_shift + delta
                    if move_index == first_index:
                        ay += delta

        if not shifts:
            return code
        replacements: list[tuple[int, int, str]] = []
        for index, delta in shifts.items():
            if delta <= 0:
                continue
            record = records[index]
            _x, y, _w, _h = record["box"]
            updated_options = self._set_or_replace_numeric_option_expression(record["options"], "y", y + delta)
            if updated_options != record["options"]:
                replacements.append((record["start"], record["end"], updated_options))
        for start_index, end_index, replacement in sorted(replacements, reverse=True):
            code = code[:start_index] + replacement + code[end_index:]
        return code

    @classmethod
    def _iter_addtext_readability_blocks_with_ranges(cls, code: str) -> list[tuple[str, str, int, int]]:
        blocks: list[tuple[str, str, int, int]] = []
        for match in re.finditer(r"\.addText\s*\(", code):
            open_paren = code.find("(", match.start())
            if open_paren < 0:
                continue
            call_segment = cls._scan_balanced_segment(code, open_paren, "(", ")")
            if not call_segment:
                continue
            call_body, _ = call_segment
            object_literals: list[tuple[str, int, int]] = []
            cursor = 0
            while True:
                brace_index = call_body.find("{", cursor)
                if brace_index < 0:
                    break
                block = cls._scan_balanced_segment(call_body, brace_index, "{", "}")
                if not block:
                    break
                literal, next_index = block
                absolute_start = open_paren + brace_index
                object_literals.append((literal, absolute_start, absolute_start + len(literal)))
                cursor = next_index
            if not object_literals:
                continue
            text = cls._extract_addtext_readability_text(call_body)
            if not text.strip():
                continue
            options_literal, start_index, _ = object_literals[-1]
            blocks.append((text, options_literal, start_index, object_literals[-1][2]))
        return blocks

    @classmethod
    def _iter_addtext_readability_blocks(cls, code: str) -> list[tuple[str, str, int]]:
        return [
            (text, options_literal, start_index)
            for text, options_literal, start_index, _end_index in cls._iter_addtext_readability_blocks_with_ranges(code)
        ]

    @classmethod
    def _extract_addtext_readability_text(cls, call_body: str) -> str:
        literal_match = re.match(
            r"\(\s*([\"'`])((?:\\.|(?!\1).)*?)\1",
            call_body,
            flags=re.DOTALL,
        )
        if literal_match:
            return cls._decode_js_string_fragment(literal_match.group(2))

        array_index = call_body.find("[")
        if array_index >= 0:
            array_segment = cls._scan_balanced_segment(call_body, array_index, "[", "]")
            if array_segment:
                array_literal, _ = array_segment
                values: list[str] = []
                cursor = 0
                while True:
                    brace_index = array_literal.find("{", cursor)
                    if brace_index < 0:
                        break
                    block = cls._scan_balanced_segment(array_literal, brace_index, "{", "}")
                    if not block:
                        break
                    object_literal, next_index = block
                    text_match = re.search(
                        r"\btext\s*:\s*([\"'`])((?:\\.|(?!\1).)*?)\1",
                        object_literal,
                        flags=re.DOTALL,
                    )
                    if text_match:
                        values.append(cls._decode_js_string_fragment(text_match.group(2)))
                        if re.search(r"\bbreakLine\s*:\s*true\b", object_literal):
                            values.append("\n")
                    cursor = next_index
                if values:
                    return "".join(values)

        values: list[str] = []
        for _quote, raw in re.findall(
            r"\btext\s*:\s*([\"'`])((?:\\.|(?!\1).)*?)\1",
            call_body,
            flags=re.DOTALL,
        ):
            values.append(cls._decode_js_string_fragment(raw))
        return "".join(values)

    @staticmethod
    def _readability_text_units(text: str) -> float:
        units = 0.0
        for char in text:
            if char in "\r\n":
                continue
            if char.isspace():
                units += 0.15
            elif "\u4e00" <= char <= "\u9fff" or "\u3040" <= char <= "\u30ff" or "\uac00" <= char <= "\ud7af":
                units += 1.0
            elif ord(char) > 127:
                units += 0.9
            elif char.isalnum():
                units += 0.55
            else:
                units += 0.35
        return units

    @classmethod
    def _is_readability_decorative_text(cls, text: str) -> bool:
        compact = re.sub(r"\s+", "", text or "")
        if not compact:
            return True
        if compact.isdigit() and len(compact) <= 2:
            return True
        has_readable_word = any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in compact)
        if has_readable_word:
            return False
        return len(compact) <= 3 or cls._readability_text_units(compact) <= 2.0

    @classmethod
    def _estimate_readability_line_count(cls, text: str, width: float | None, font_size: float) -> int:
        raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        blank_lines = sum(1 for line in raw_lines if not line.strip())
        lines = [line for line in raw_lines if line.strip()]
        if not lines:
            return max(1, blank_lines)
        if width is None or width <= 0.05:
            estimated = len(lines) + blank_lines
            total_units = sum(cls._readability_text_units(line) for line in lines)
            if total_units > 18:
                estimated = max(estimated, 2)
            if total_units > 34:
                estimated = max(estimated, 3)
            return estimated

        units_per_line = max(3.0, width * 72.0 / max(font_size * 0.95, 1.0))
        estimated = blank_lines
        for line in lines:
            units = cls._readability_text_units(line)
            estimated += max(1, math.ceil(units / units_per_line))
        return max(1, estimated)

    def _validate_book_readability_layout(self, code: str) -> None:
        if not self._layout_qa_enabled():
            return
        constants = self._readability_numeric_constants(code)
        self._validate_book_readability_table_rendering(code)
        self._validate_book_readability_text_capacity(code, constants)
        self._validate_book_readability_bottom_safe_area(code, constants)
        self._validate_book_readability_wrapping_shapes(code, constants)
        self._validate_book_readability_decorations(code, constants)
        self._validate_book_readability_shape_occlusion(code, constants)
        self._validate_book_readability_text_overlaps(code, constants)
        self._validate_book_readability_connectors(code, constants)

    @classmethod
    def _validate_book_readability_table_rendering(cls, code: str) -> None:
        if "addTable" not in str(code or ""):
            return
        normalized = cls._decode_unicode_escape_literals(code)
        if not re.search(r"[\u4e00-\u9fff]", normalized):
            return
        prefix = cls._layout_qa_error_prefix()
        raise ValueError(
            f"{prefix}：中文正文不要使用 slide.addTable。"
            "当前渲染链路中原生表格可能导致中文字体回退成方框问号；"
            "请改用 slide.addShape 绘制表格式单元格，再用 slide.addText 写入中文，"
            "并显式设置中文字体、14-16pt 字号和 1.25 行距。"
        )

    @classmethod
    def _readability_numeric_constants(cls, code: str) -> dict[str, float]:
        constants: dict[str, float] = {}
        for match in re.finditer(
            r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*([^;\n]+)",
            code,
        ):
            value = cls._evaluate_readability_numeric_expression(match.group(2), constants)
            if value is not None:
                constants[match.group(1)] = value
        return constants

    @classmethod
    def _evaluate_readability_numeric_expression(
        cls,
        expression: str | None,
        constants: dict[str, float],
    ) -> float | None:
        if expression is None:
            return None
        expression = expression.strip()
        if not expression:
            return None
        numeric = re.fullmatch(cls._geometry_number_pattern(), expression)
        if numeric:
            try:
                return float(expression)
            except ValueError:
                return None
        if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*|[-+*/().\sA-Za-z_$0-9]+", expression):
            return None
        if "__" in expression:
            return None

        missing = False

        def replace_identifier(match: re.Match) -> str:
            nonlocal missing
            name = match.group(0)
            if name not in constants:
                missing = True
                return "0"
            return repr(constants[name])

        safe_expression = re.sub(r"\b[A-Za-z_$][A-Za-z0-9_$]*\b", replace_identifier, expression)
        if missing:
            return None
        try:
            value = eval(safe_expression, {"__builtins__": {}}, {})
        except Exception:
            return None
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return float(value)
        return None

    def _extract_readability_numeric_option(
        self,
        options_literal: str,
        key: str,
        constants: dict[str, float],
    ) -> float | None:
        value = self._extract_numeric_option(options_literal, key)
        if value is not None:
            return value
        return self._evaluate_readability_numeric_expression(
            self._extract_option_value(options_literal, key),
            constants,
        )

    @classmethod
    def _extract_string_option(cls, options_literal: str, key: str) -> str | None:
        match = re.search(
            rf"\b{re.escape(key)}\s*:\s*([\"'`])((?:\\.|(?!\1).)*?)\1",
            options_literal,
            flags=re.DOTALL,
        )
        if not match:
            return None
        return cls._decode_js_string_fragment(match.group(2)).strip().lower()

    @classmethod
    def _extract_fill_color_option(cls, options_literal: str) -> str | None:
        match = re.search(
            r"\bfill\s*:\s*\{[^{}]*\bcolor\s*:\s*([\"'`])((?:\\.|(?!\1).)*?)\1",
            options_literal,
            flags=re.DOTALL,
        )
        if not match:
            return None
        return cls._decode_js_string_fragment(match.group(2)).strip().lower().lstrip("#")

    @classmethod
    def _is_book_readability_non_wrapper_fill_shape(
        cls,
        kind: str,
        options_literal: str,
        box: tuple[float, float, float, float],
    ) -> bool:
        if kind != "rect":
            return False
        _x, _y, w, h = box
        fill_color = cls._extract_fill_color_option(options_literal)
        if fill_color not in {"2563eb", "14b8a6", "0f172a", "1d4ed8"}:
            return False
        return h <= 0.72 and w >= 0.55

    def _is_book_readability_auxiliary_text(
        self,
        text: str,
        options_literal: str,
        constants: dict[str, float],
    ) -> bool:
        compact = re.sub(r"\s+", "", text or "")
        if self._is_readability_decorative_text(compact):
            return True
        if self._readability_text_units(compact) <= 6.0:
            return True
        lowered = compact.lower()
        if "考纲" in compact and self._readability_text_units(compact) <= 12.0:
            return True
        if "来源" in compact or "source" in lowered:
            return True
        if re.fullmatch(r"\d+(?:\.\d+){1,3}[\u4e00-\u9fffA-Za-z0-9·：:、_\-\s]+", compact):
            return self._readability_text_units(compact) <= 24.0
        if "教材" in compact or re.search(r"\bp\.?\d", lowered):
            return True
        if re.fullmatch(r"(第)?\d{1,2}(页)?", compact):
            return True
        if re.fullmatch(r"\d+(?:\.\d+){0,3}", compact):
            return True
        y = self._extract_readability_numeric_option(options_literal, "y", constants)
        if y is not None and y >= 6.65:
            return True
        return False

    def _validate_book_readability_text_density(self, code: str, constants: dict[str, float]) -> None:
        significant_blocks = 0
        total_units = 0.0
        for text, options, _start_index in self._iter_addtext_readability_blocks(code):
            if self._is_book_readability_auxiliary_text(text, options, constants):
                continue
            units = self._readability_text_units(text)
            if units < 8.0:
                continue
            significant_blocks += 1
            total_units += units
        if significant_blocks >= 18 or total_units >= 230:
            prefix = self._layout_qa_error_prefix()
            raise ValueError(
                f"{prefix}：页面文字密度过高。"
                f"检测到 {significant_blocks} 个主要文字块、约 {total_units:.0f} 个文字单位。"
                "请改用更稳的表格/分栏/上下分区或拆页承接，保留教材关键点；不要在一页同时堆流程图、卡片墙和长总结条。"
            )

    def _readability_required_text_height(
        self,
        text: str,
        options_literal: str,
        constants: dict[str, float],
    ) -> tuple[float, int, float] | None:
        font_size = self._extract_readability_numeric_option(options_literal, "fontSize", constants)
        if font_size is None or font_size < 14:
            return None
        width = self._extract_readability_numeric_option(options_literal, "w", constants)
        line_spacing = self._extract_readability_numeric_option(options_literal, "lineSpacingMultiple", constants)
        if line_spacing is None:
            line_spacing = 1.25 if font_size <= 18 else 1.08
        margin = self._extract_readability_numeric_option(options_literal, "margin", constants)
        vertical_padding = 0.04 if margin is None else max(0.02, min(0.12, margin * 2.0))
        estimated_lines = self._estimate_readability_line_count(text, width, font_size)
        required_height = estimated_lines * (font_size * line_spacing / 64.0) + vertical_padding
        return font_size, estimated_lines, required_height

    def _validate_book_readability_text_capacity(self, code: str, constants: dict[str, float]) -> None:
        for text, options, _start_index in self._iter_addtext_readability_blocks(code):
            if self._is_readability_decorative_text(text) or self._is_book_readability_auxiliary_text(text, options, constants):
                continue
            height = self._extract_readability_numeric_option(options, "h", constants)
            metrics = self._readability_required_text_height(text, options, constants)
            if height is None or metrics is None:
                continue
            font_size, estimated_lines, required_height = metrics
            tolerance = 0.045
            if height < required_height - tolerance:
                sample = re.sub(r"\s+", " ", text).strip()[:36]
                prefix = self._layout_qa_error_prefix()
                raise ValueError(
                    f"{prefix}：文本框高度不足。"
                    f"文本“{sample}”使用 fontSize={font_size:g}、预计 {estimated_lines} 行、"
                    f"h={height:g}，至少需要约 {required_height:.2f}。"
                    "请扩大文本框/卡片高度，改用分栏/表格或拆成多页承接；不要依赖小字号、单倍行距或裁切来容纳内容，也不要删关键考点。"
                )

    def _validate_book_readability_bottom_safe_area(self, code: str, constants: dict[str, float]) -> None:
        slide_bottom = float(config.SLIDE_HEIGHT_INCH) - 0.42
        for text, options, _start_index in self._iter_addtext_readability_blocks(code):
            if self._is_readability_decorative_text(text) or self._is_book_readability_auxiliary_text(text, options, constants):
                continue
            y = self._extract_readability_numeric_option(options, "y", constants)
            h = self._extract_readability_numeric_option(options, "h", constants)
            metrics = self._readability_required_text_height(text, options, constants)
            if y is None or h is None or metrics is None:
                continue
            _font_size, _estimated_lines, required_height = metrics
            effective_h = max(h, required_height)
            if y + effective_h <= slide_bottom + 0.02:
                continue
            sample = re.sub(r"\s+", " ", text).strip()[:36]
            prefix = self._layout_qa_error_prefix()
            raise ValueError(
                f"{prefix}：正文侵入页脚安全区。"
                f"文本“{sample}”预计底部 y≈{y + effective_h:.2f}，已接近或超过页脚区域。"
                "请上移内容、改成表格/分栏或拆页承接；页脚页码和教材来源必须保持独立可读。"
            )

    def _book_readability_wrapper_shapes(
        self,
        code: str,
        constants: dict[str, float],
    ) -> list[tuple[tuple[float, float, float, float], str]]:
        return [
            (shape_box, kind)
            for shape_box, kind, _options_literal, _start_index, _end_index in self._book_readability_wrapper_shapes_with_ranges(code, constants)
        ]

    def _book_readability_wrapper_shapes_with_ranges(
        self,
        code: str,
        constants: dict[str, float],
    ) -> list[tuple[tuple[float, float, float, float], str, str, int, int]]:
        shapes: list[tuple[tuple[float, float, float, float], str, str, int, int]] = []
        for call_name, options, shape_kind, _start_index, _end_index in self._iter_geometry_option_blocks_with_ranges(code):
            if call_name != "addShape":
                continue
            kind = self._normalized_shape_kind(shape_kind)
            if not kind or kind.endswith("line") or "line" == kind:
                continue
            if not any(token in kind for token in ("rect", "ellipse", "oval")):
                continue
            x = self._extract_readability_numeric_option(options, "x", constants)
            y = self._extract_readability_numeric_option(options, "y", constants)
            w = self._extract_readability_numeric_option(options, "w", constants)
            h = self._extract_readability_numeric_option(options, "h", constants)
            if None in (x, y, w, h):
                continue
            assert x is not None and y is not None and w is not None and h is not None
            if w <= 0.15 or h <= 0.15:
                continue
            if w * h < 0.12:
                continue
            if w >= 12.6 and h >= 6.8:
                continue
            if (w >= 12.0 and h <= 0.35) or (h >= 6.8 and w <= 0.35):
                continue
            if h >= 6.8 and w <= 2.8 and x <= 0.3:
                continue
            if w >= 10.5 and h <= 1.05 and y <= 1.2:
                continue
            if w >= 10.5 and h <= 0.45 and y >= 6.6:
                continue
            if self._is_book_readability_non_wrapper_fill_shape(kind, options, (x, y, w, h)):
                continue
            shapes.append(((x, y, w, h), kind, options, _start_index, _end_index))
        return shapes

    @staticmethod
    def _readability_box_overlap_ratio(
        text_box: tuple[float, float, float, float],
        shape_box: tuple[float, float, float, float],
    ) -> float:
        tx, ty, tw, th = text_box
        sx, sy, sw, sh = shape_box
        text_area = tw * th
        if text_area <= 0:
            return 0.0
        overlap_w = min(tx + tw, sx + sw) - max(tx, sx)
        overlap_h = min(ty + th, sy + sh) - max(ty, sy)
        if overlap_w <= 0 or overlap_h <= 0:
            return 0.0
        return (overlap_w * overlap_h) / text_area

    def _validate_book_readability_wrapping_shapes(self, code: str, constants: dict[str, float]) -> None:
        wrapper_shapes = self._book_readability_wrapper_shapes(code, constants)
        if not wrapper_shapes:
            return
        used_wrappers: dict[tuple[float, float, float, float, str], str] = {}
        for text, options, _start_index in self._iter_addtext_readability_blocks(code):
            if self._is_book_readability_auxiliary_text(text, options, constants):
                continue
            x = self._extract_readability_numeric_option(options, "x", constants)
            y = self._extract_readability_numeric_option(options, "y", constants)
            w = self._extract_readability_numeric_option(options, "w", constants)
            h = self._extract_readability_numeric_option(options, "h", constants)
            metrics = self._readability_required_text_height(text, options, constants)
            if None in (x, y, w, h) or metrics is None:
                continue
            assert x is not None and y is not None and w is not None and h is not None
            if w <= 0.05 or h <= 0.05:
                continue
            _font_size, _estimated_lines, required_height = metrics
            effective_h = max(h, required_height)
            text_box = (x, y, w, effective_h)
            text_center = (x + w / 2.0, y + effective_h / 2.0)
            candidates: list[tuple[float, tuple[float, float, float, float], str]] = []
            for shape_box, kind in wrapper_shapes:
                sx, sy, sw, sh = shape_box
                center_inside = sx - 0.03 <= text_center[0] <= sx + sw + 0.03 and sy - 0.03 <= text_center[1] <= sy + sh + 0.03
                overlap_ratio = self._readability_box_overlap_ratio(text_box, shape_box)
                if center_inside or overlap_ratio >= 0.35:
                    candidates.append((sw * sh, shape_box, kind))
            if not candidates:
                continue
            _area, shape_box, _kind = min(candidates, key=lambda item: item[0])
            sx, sy, sw, sh = shape_box
            tolerance = 0.045
            if x < sx - tolerance or y < sy - tolerance or x + w > sx + sw + tolerance or y + effective_h > sy + sh + tolerance:
                sample = re.sub(r"\s+", " ", text).strip()[:36]
                prefix = self._layout_qa_error_prefix()
                raise ValueError(
                    f"{prefix}：外层形状未包住文字。"
                    f"文本“{sample}”的文字框/预计行高超出所在卡片或节点形状："
                    f"文字框 x={x:g}, y={y:g}, w={w:g}, h≈{effective_h:.2f}；"
                    f"外层形状 x={sx:g}, y={sy:g}, w={sw:g}, h={sh:g}。"
                    "请同步扩大外层卡片/圆形/节点，或改用更宽的表格/分栏/下一页承接；不要只扩大 addText 而不扩大背景形状，也不要删关键考点。"
                )
            shape_key = (
                round(sx, 3),
                round(sy, 3),
                round(sw, 3),
                round(sh, 3),
                _kind,
            )
            used_wrappers.setdefault(shape_key, re.sub(r"\s+", " ", text).strip()[:24])
        self._validate_book_readability_wrapper_spacing(used_wrappers)
        self._validate_book_readability_wrapper_count(used_wrappers)

    @staticmethod
    def _book_readability_box_overlap(
        left: tuple[float, float, float, float],
        right: tuple[float, float, float, float],
    ) -> tuple[float, float, float]:
        lx, ly, lw, lh = left
        rx, ry, rw, rh = right
        overlap_w = min(lx + lw, rx + rw) - max(lx, rx)
        overlap_h = min(ly + lh, ry + rh) - max(ly, ry)
        if overlap_w <= 0 or overlap_h <= 0:
            return 0.0, 0.0, 0.0
        return overlap_w, overlap_h, overlap_w * overlap_h

    @staticmethod
    def _box_contains_readability_shape(
        outer: tuple[float, float, float, float],
        inner: tuple[float, float, float, float],
    ) -> bool:
        ox, oy, ow, oh = outer
        ix, iy, iw, ih = inner
        tolerance = 0.04
        return (
            ix >= ox - tolerance
            and iy >= oy - tolerance
            and ix + iw <= ox + ow + tolerance
            and iy + ih <= oy + oh + tolerance
        )

    def _validate_book_readability_wrapper_spacing(
        self,
        used_wrappers: dict[tuple[float, float, float, float, str], str],
    ) -> None:
        items = list(used_wrappers.items())
        min_gap = 0.08
        for index, (left_key, left_sample) in enumerate(items):
            lx, ly, lw, lh, left_kind = left_key
            left_box = (lx, ly, lw, lh)
            if lw * lh <= 0.12:
                continue
            for right_key, right_sample in items[index + 1 :]:
                rx, ry, rw, rh, right_kind = right_key
                right_box = (rx, ry, rw, rh)
                if rw * rh <= 0.12:
                    continue
                if self._box_contains_readability_shape(left_box, right_box) or self._box_contains_readability_shape(right_box, left_box):
                    continue
                both_ellipses = any(token in left_kind for token in ("ellipse", "oval")) and any(
                    token in right_kind for token in ("ellipse", "oval")
                )
                overlap_w = min(lx + lw, rx + rw) - max(lx, rx)
                overlap_h = min(ly + lh, ry + rh) - max(ly, ry)
                if overlap_w > 0.03 and overlap_h > 0.03:
                    if min(overlap_w, overlap_h) <= 0.08:
                        continue
                    if both_ellipses:
                        continue
                    prefix = self._layout_qa_error_prefix()
                    raise ValueError(
                        f"{prefix}：外层卡片/节点相互重叠。"
                        f"形状内文本“{left_sample}”与“{right_sample}”所在卡片发生碰撞。"
                        "请改成两列/分段布局、表格或拆页承接；扩大外层形状时也要保留卡片之间的间隔，不能删关键考点。"
                    )
                horizontal_gap = max(rx - (lx + lw), lx - (rx + rw), 0.0)
                vertical_gap = max(ry - (ly + lh), ly - (ry + rh), 0.0)
                if overlap_w > min(lw, rw) * 0.35 and 0 < vertical_gap < min_gap:
                    prefix = self._layout_qa_error_prefix()
                    raise ValueError(
                        f"{prefix}：外层卡片/节点间距过小。"
                        f"形状内文本“{left_sample}”与“{right_sample}”上下间距仅约 {vertical_gap:.2f}。"
                        "请拉开卡片距离，改用表格/分栏或拆成多页承接，避免放映时看起来贴在一起。"
                    )
                if overlap_h > min(lh, rh) * 0.35 and 0 < horizontal_gap < min_gap:
                    prefix = self._layout_qa_error_prefix()
                    raise ValueError(
                        f"{prefix}：外层卡片/节点间距过小。"
                        f"形状内文本“{left_sample}”与“{right_sample}”左右间距仅约 {horizontal_gap:.2f}。"
                        "请拉开卡片距离，改用表格/分栏或拆成多页承接，避免放映时看起来贴在一起。"
                    )

    def _validate_book_readability_wrapper_count(
        self,
        used_wrappers: dict[tuple[float, float, float, float, str], str],
    ) -> None:
        content_cards = []
        for key, sample in used_wrappers.items():
            _x, y, w, h, kind = key
            area = w * h
            if y <= 1.1 and w >= 8:
                continue
            if y >= 6.55 and w >= 8:
                continue
            if area <= 0.18:
                continue
            if any(token in kind for token in ("ellipse", "oval")) and area <= 0.7:
                continue
            content_cards.append((key, sample))
        compact_cards = [item for item in content_cards if item[0][2] * item[0][3] <= 1.15]
        if len(content_cards) >= 10 or len(compact_cards) >= 7:
            samples = "、".join(sample for _key, sample in content_cards[:4])
            prefix = self._layout_qa_error_prefix()
            raise ValueError(
                f"{prefix}：同页卡片/节点数量过多。"
                f"检测到 {len(content_cards)} 个带正文的卡片/节点（如：{samples}）。"
                "请保留教材内容，但改用表格、分栏或上下分区承载；不要把大量知识点压成小卡片墙。"
            )

    def _book_readability_decorative_shapes(
        self,
        code: str,
        constants: dict[str, float],
    ) -> list[tuple[tuple[float, float, float, float], str]]:
        shapes: list[tuple[tuple[float, float, float, float], str]] = []
        for call_name, options, shape_kind, _start_index, _end_index in self._iter_geometry_option_blocks_with_ranges(code):
            if call_name != "addShape":
                continue
            kind = self._normalized_shape_kind(shape_kind)
            if not kind or kind == "line" or kind.endswith("line"):
                continue
            x = self._extract_readability_numeric_option(options, "x", constants)
            y = self._extract_readability_numeric_option(options, "y", constants)
            w = self._extract_readability_numeric_option(options, "w", constants)
            h = self._extract_readability_numeric_option(options, "h", constants)
            if None in (x, y, w, h):
                continue
            assert x is not None and y is not None and w is not None and h is not None
            if w <= 0.05 or h <= 0.05:
                continue
            if w >= 10.5 or h >= 6.8:
                continue
            is_badge = any(token in kind for token in ("ellipse", "oval")) and w <= 1.25 and h <= 1.25
            is_small_label = any(token in kind for token in ("rect", "roundrect")) and w <= 2.2 and h <= 0.75
            if is_badge or is_small_label:
                shapes.append(((x, y, w, h), kind))
        return shapes

    def _validate_book_readability_decorations(self, code: str, constants: dict[str, float]) -> None:
        decorative_shapes = self._book_readability_decorative_shapes(code, constants)
        if not decorative_shapes:
            return
        for shape_box, _kind in decorative_shapes:
            sx, sy, sw, sh = shape_box
            shape_area = sw * sh
            if shape_area <= 0:
                continue
            for text, options, _start_index in self._iter_addtext_readability_blocks(code):
                if self._is_book_readability_auxiliary_text(text, options, constants):
                    continue
                x = self._extract_readability_numeric_option(options, "x", constants)
                y = self._extract_readability_numeric_option(options, "y", constants)
                w = self._extract_readability_numeric_option(options, "w", constants)
                h = self._extract_readability_numeric_option(options, "h", constants)
                if None in (x, y, w, h):
                    continue
                assert x is not None and y is not None and w is not None and h is not None
                if w <= 0.05 or h <= 0.05:
                    continue
                text_box = (x, y, w, h)
                overlap_w, overlap_h, overlap_area = self._book_readability_box_overlap(shape_box, text_box)
                if overlap_area <= 0:
                    continue
                text_center_x = x + w / 2.0
                text_center_y = y + h / 2.0
                if sx - 0.03 <= text_center_x <= sx + sw + 0.03 and sy - 0.03 <= text_center_y <= sy + sh + 0.03:
                    continue
                overlap_ratio = overlap_area / min(shape_area, w * h)
                if overlap_ratio < 0.12 or overlap_w < 0.04 or overlap_h < 0.04:
                    continue
                sample = re.sub(r"\s+", " ", text).strip()[:32]
                prefix = self._layout_qa_error_prefix()
                raise ValueError(
                    f"{prefix}：装饰形状遮挡正文或标题。"
                    f"装饰圆/徽章/小标签与文本“{sample}”发生重叠。"
                    "请把章节圆点、编号徽章、可选参考标签移出标题和正文安全区，或改成左侧窄色条/独立标签行。"
                )

    @classmethod
    def _readability_shape_has_visible_fill(cls, options_literal: str) -> bool:
        if not re.search(r"\bfill\s*:", options_literal):
            return False
        transparency = cls._extract_numeric_option(options_literal, "transparency")
        if transparency is not None and transparency >= 70:
            return False
        return True

    def _validate_book_readability_shape_occlusion(self, code: str, constants: dict[str, float]) -> None:
        text_boxes: list[tuple[tuple[float, float, float, float], str, int]] = []
        for text, options, start_index, _end_index in self._iter_addtext_readability_blocks_with_ranges(code):
            if self._is_book_readability_auxiliary_text(text, options, constants):
                continue
            x = self._extract_readability_numeric_option(options, "x", constants)
            y = self._extract_readability_numeric_option(options, "y", constants)
            w = self._extract_readability_numeric_option(options, "w", constants)
            h = self._extract_readability_numeric_option(options, "h", constants)
            if None in (x, y, w, h):
                continue
            assert x is not None and y is not None and w is not None and h is not None
            if w <= 0.05 or h <= 0.05:
                continue
            metrics = self._readability_required_text_height(text, options, constants)
            effective_h = max(h, metrics[2]) if metrics else h
            sample = re.sub(r"\s+", " ", text).strip()[:28]
            text_boxes.append(((x, y, w, effective_h), sample, start_index))
        if not text_boxes:
            return

        for call_name, options, shape_kind, start_index, _end_index in self._iter_geometry_option_blocks_with_ranges(code):
            if call_name != "addShape":
                continue
            kind = self._normalized_shape_kind(shape_kind)
            if not kind or kind == "line" or kind.endswith("line"):
                continue
            if not self._readability_shape_has_visible_fill(options):
                continue
            x = self._extract_readability_numeric_option(options, "x", constants)
            y = self._extract_readability_numeric_option(options, "y", constants)
            w = self._extract_readability_numeric_option(options, "w", constants)
            h = self._extract_readability_numeric_option(options, "h", constants)
            if None in (x, y, w, h):
                continue
            assert x is not None and y is not None and w is not None and h is not None
            if w <= 0.05 or h <= 0.05 or w * h <= 0.08:
                continue
            if w >= 12.6 and h >= 6.8:
                continue
            if (w >= 12.0 and h <= 0.35) or (h >= 6.8 and w <= 0.35):
                continue
            if h >= 6.8 and w <= 2.8 and x <= 0.3:
                continue
            if w >= 10.5 and h <= 1.05 and y <= 1.2:
                continue
            if w >= 10.5 and h <= 0.45 and y >= 6.6:
                continue
            shape_box = (x, y, w, h)
            shape_area = w * h
            for text_box, sample, text_start in text_boxes:
                if start_index <= text_start:
                    continue
                overlap_w, overlap_h, overlap_area = self._book_readability_box_overlap(shape_box, text_box)
                if overlap_area <= 0 or overlap_w < 0.05 or overlap_h < 0.05:
                    continue
                text_area = text_box[2] * text_box[3]
                overlap_ratio = overlap_area / max(0.01, min(shape_area, text_area))
                if overlap_ratio < 0.18:
                    continue
                prefix = self._layout_qa_error_prefix()
                raise ValueError(
                    f"{prefix}：形状绘制顺序遮挡文字。"
                    f"后绘制的填充形状覆盖了文本“{sample}”。"
                    "请先绘制背景形状再绘制文字，或改成表格/分栏/上下分区；不要用大色块、椭圆或标签盖住正文。"
                )

    @staticmethod
    def _normalized_shape_kind(shape_kind: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", shape_kind.lower())

    def _book_readability_visible_text_boxes(
        self,
        code: str,
        constants: dict[str, float],
    ) -> list[tuple[tuple[float, float, float, float], str]]:
        text_boxes: list[tuple[tuple[float, float, float, float], str]] = []
        for text, options, _start_index in self._iter_addtext_readability_blocks(code):
            if self._readability_text_units(text) <= 2.0:
                continue
            x = self._extract_readability_numeric_option(options, "x", constants)
            y = self._extract_readability_numeric_option(options, "y", constants)
            w = self._extract_readability_numeric_option(options, "w", constants)
            h = self._extract_readability_numeric_option(options, "h", constants)
            if None in (x, y, w, h):
                continue
            assert x is not None and y is not None and w is not None and h is not None
            if w <= 0.05 or h <= 0.05:
                continue
            font_size = self._extract_readability_numeric_option(options, "fontSize", constants) or 14
            line_spacing = self._extract_readability_numeric_option(options, "lineSpacingMultiple", constants)
            if line_spacing is None:
                line_spacing = 1.25 if font_size <= 18 else 1.08
            lines = [line for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()]
            max_units = max((self._readability_text_units(line) for line in lines), default=1.0)
            estimated_lines = self._estimate_readability_line_count(text, w, font_size)
            natural_w = max(0.05, max_units * font_size * 0.95 / 72.0 + 0.04)
            natural_h = max(0.05, estimated_lines * (font_size * line_spacing / 72.0) + 0.04)
            visual_w = min(w, natural_w)
            visual_h = min(h, natural_h)

            align = self._extract_string_option(options, "align") or "left"
            valign = self._extract_string_option(options, "valign") or "top"
            visual_x = x
            if align in {"center", "middle"}:
                visual_x = x + max(0.0, (w - visual_w) / 2.0)
            elif align == "right":
                visual_x = x + max(0.0, w - visual_w)
            visual_y = y
            if valign in {"mid", "middle", "center"}:
                visual_y = y + max(0.0, (h - visual_h) / 2.0)
            elif valign in {"bottom", "bot"}:
                visual_y = y + max(0.0, h - visual_h)

            text_boxes.append(((visual_x, visual_y, visual_w, visual_h), re.sub(r"\s+", " ", text).strip()[:28]))
        return text_boxes

    def _validate_book_readability_text_overlaps(self, code: str, constants: dict[str, float]) -> None:
        text_boxes = self._book_readability_visible_text_boxes(code, constants)
        for index, (left_box, left_sample) in enumerate(text_boxes):
            lx, ly, lw, lh = left_box
            left_area = lw * lh
            if left_area <= 0:
                continue
            for right_box, right_sample in text_boxes[index + 1 :]:
                rx, ry, rw, rh = right_box
                right_area = rw * rh
                if right_area <= 0:
                    continue
                intersect_w = min(lx + lw, rx + rw) - max(lx, rx)
                intersect_h = min(ly + lh, ry + rh) - max(ly, ry)
                if intersect_w <= 0 or intersect_h <= 0:
                    continue
                overlap_ratio = (intersect_w * intersect_h) / min(left_area, right_area)
                if overlap_ratio >= 0.15:
                    prefix = self._layout_qa_error_prefix()
                    raise ValueError(
                        f"{prefix}：文本框相互重叠。"
                        f"重叠文本“{left_sample}”与“{right_sample}”。"
                        "请重新分配形状和标签位置，圆形/Venn 图的长标签应移到圆外或改成卡片/表格。"
                    )

    @staticmethod
    def _segment_intersects_box_interior(
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        box: tuple[float, float, float, float],
    ) -> bool:
        bx, by, bw, bh = box
        inset_x = min(0.06, max(0.01, bw * 0.08))
        inset_y = min(0.06, max(0.01, bh * 0.08))
        left = bx + inset_x
        right = bx + bw - inset_x
        top = by + inset_y
        bottom = by + bh - inset_y
        if left >= right or top >= bottom:
            return False
        for step in range(1, 24):
            ratio = step / 24.0
            px = x1 + (x2 - x1) * ratio
            py = y1 + (y2 - y1) * ratio
            if left < px < right and top < py < bottom:
                return True
        return False

    def _validate_book_readability_connectors(self, code: str, constants: dict[str, float]) -> None:
        text_boxes = self._book_readability_visible_text_boxes(code, constants)
        if not text_boxes:
            return

        for call_name, options, shape_kind, _start_index, _end_index in self._iter_geometry_option_blocks_with_ranges(code):
            if call_name != "addShape" or self._normalized_shape_kind(shape_kind) != "line":
                continue
            x = self._extract_readability_numeric_option(options, "x", constants)
            y = self._extract_readability_numeric_option(options, "y", constants)
            w = self._extract_readability_numeric_option(options, "w", constants)
            h = self._extract_readability_numeric_option(options, "h", constants)
            if None in (x, y, w, h):
                continue
            assert x is not None and y is not None and w is not None and h is not None
            if math.hypot(w, h) < 0.35:
                continue
            if abs(w) >= 0.35 and abs(h) >= 0.35 and math.hypot(w, h) >= 0.75:
                prefix = self._layout_qa_error_prefix()
                raise ValueError(
                    f"{prefix}：检测到较长斜向连接线。"
                    "请改用水平/垂直分段线，或重新摆放节点后从节点外缘连接，避免线条穿过圆圈、标签或正文。"
                )
            x2 = x + w
            y2 = y + h
            for box, sample in text_boxes:
                if self._segment_intersects_box_interior(x, y, x2, y2, box):
                    prefix = self._layout_qa_error_prefix()
                    raise ValueError(
                        f"{prefix}：连接线穿过文字区域。"
                        f"受影响文本“{sample}”。请让箭头从节点外缘出发，使用水平/垂直分段线或重新摆放节点，"
                        "不要让线条压住标签、正文或编号。"
                    )

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

    def _normalize_book_readability_table_geometry(self, code: str) -> str:
        if not self._layout_qa_enabled():
            return code
        constants = self._readability_numeric_constants(code)
        replacements: list[tuple[int, int, str]] = []
        slide_height = float(config.SLIDE_HEIGHT_INCH)
        for call_name, options, _shape_kind, start_index, end_index in self._iter_geometry_option_blocks_with_ranges(code):
            if call_name != "addTable":
                continue
            if self._extract_option_value(options, "h") is not None:
                continue
            y = self._extract_readability_numeric_option(options, "y", constants)
            if y is None:
                y = 1.4
            inferred_h = max(0.9, min(5.8, slide_height - y - 0.55))
            updated = self._set_or_insert_numeric_option(options, "h", inferred_h)
            if updated != options:
                replacements.append((start_index, end_index, updated))
        for start_index, end_index, replacement in sorted(replacements, reverse=True):
            code = code[:start_index] + replacement + code[end_index:]
        return code

    def _repair_book_readability_undefined_geometry_expressions(self, code: str) -> str:
        if not self._layout_qa_enabled():
            return code
        constants = self._readability_numeric_constants(code)
        declaration_positions = self._js_declaration_positions(code)
        replacements: list[tuple[int, int, str]] = []
        for call_name, options, _shape_kind, start_index, end_index in self._iter_geometry_option_blocks_with_ranges(code):
            updated = options
            scoped_parameters = self._js_function_parameters_in_scope(code, start_index)
            for key in ("x", "y", "w", "h"):
                raw_value = self._extract_option_value(updated, key)
                if raw_value is None or self._extract_numeric_option(updated, key) is not None:
                    continue
                missing_identifiers = []
                declared_late = False
                for identifier in self._js_identifiers_in_expression(raw_value):
                    if identifier in scoped_parameters or identifier in constants:
                        positions = declaration_positions.get(identifier, [])
                        if positions and not any(position < start_index for position in positions):
                            declared_late = True
                        else:
                            continue
                    positions = declaration_positions.get(identifier, [])
                    if any(position < start_index for position in positions):
                        continue
                    missing_identifiers.append(identifier)
                if not missing_identifiers and not declared_late:
                    if self._evaluate_readability_numeric_expression(raw_value, constants) is not None:
                        continue
                if not missing_identifiers and not declared_late:
                    continue
                updated = self._set_or_replace_numeric_option_expression(
                    updated,
                    key,
                    self._book_readability_geometry_fallback_value(call_name, key, raw_value),
                )
            if updated != options:
                replacements.append((start_index, end_index, updated))
        for start_index, end_index, replacement in sorted(replacements, reverse=True):
            code = code[:start_index] + replacement + code[end_index:]
        return code

    @staticmethod
    def _book_readability_geometry_fallback_value(call_name: str, key: str, raw_value: str) -> float:
        lowered = (raw_value or "").lower()
        if key == "x":
            if "right" in lowered:
                return 7.0
            if any(token in lowered for token in ("mid", "center")):
                return 4.7
            return 1.0
        if key == "y":
            if any(token in lowered for token in ("bottom", "footer")):
                return 6.2
            if any(token in lowered for token in ("mid", "center")):
                return 3.2
            return 1.4
        if key == "w":
            return 3.4 if call_name == "addText" else 3.0
        if key == "h":
            return 0.7 if call_name == "addText" else 0.9
        return 1.0

    def _normalize_book_readability_dynamic_geometry_constraints(self, code: str) -> str:
        if not self._layout_qa_enabled():
            return code
        constants = self._readability_numeric_constants(code)
        slide_width = float(config.SLIDE_WIDTH_INCH)
        slide_height = float(config.SLIDE_HEIGHT_INCH)
        min_size = 0.05
        replacements: list[tuple[int, int, str]] = []
        for call_name, options, shape_kind, start_index, end_index in self._iter_geometry_option_blocks_with_ranges(code):
            if call_name == "addShape" and shape_kind == "line":
                continue
            x = self._extract_readability_numeric_option(options, "x", constants)
            y = self._extract_readability_numeric_option(options, "y", constants)
            w = self._extract_readability_numeric_option(options, "w", constants)
            h = self._extract_readability_numeric_option(options, "h", constants)
            if None in (x, y, w, h):
                continue
            assert x is not None and y is not None and w is not None and h is not None
            new_w = min(max(w, min_size), slide_width)
            new_h = min(max(h, min_size), slide_height)
            new_x = x
            new_y = y
            if new_x < 0:
                new_x = 0.0
            if new_y < 0:
                new_y = 0.0
            if new_x + new_w > slide_width:
                new_x = max(0.0, slide_width - new_w)
            if new_y + new_h > slide_height:
                new_y = max(0.0, slide_height - new_h)
            updates = {"x": new_x, "y": new_y, "w": new_w, "h": new_h}
            if all(abs(updates[key] - value) < 0.0005 for key, value in {"x": x, "y": y, "w": w, "h": h}.items()):
                continue
            updated = options
            for key, value in updates.items():
                updated = self._set_or_replace_numeric_option_expression(updated, key, value)
            if updated != options:
                replacements.append((start_index, end_index, updated))
        for start_index, end_index, replacement in sorted(replacements, reverse=True):
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

        for call_name, options, shape_kind, start_index, _ in self._iter_geometry_option_blocks_with_ranges(code):
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
                if self._allow_dynamic_geometry():
                    # Repeated cards/TOCs commonly use variables such as card1Y.
                    # Keep those legal, but fail early for JS that would crash at runtime.
                    self._validate_dynamic_geometry_references(code, raw_values, start_index, call_name)
                    continue
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

    @classmethod
    def _validate_dynamic_geometry_references(
        cls,
        code: str,
        raw_values: dict[str, str | None],
        option_start_index: int,
        call_name: str,
    ) -> None:
        declaration_positions = cls._js_declaration_positions(code)
        scoped_parameters = cls._js_function_parameters_in_scope(code, option_start_index)

        for key, raw_value in raw_values.items():
            if raw_value is None or re.fullmatch(cls._geometry_number_pattern(), raw_value):
                continue
            for identifier in cls._js_identifiers_in_expression(raw_value):
                if identifier in scoped_parameters:
                    continue
                positions = declaration_positions.get(identifier, [])
                if any(position < option_start_index for position in positions):
                    continue
                if any(position > option_start_index for position in positions):
                    raise ValueError(
                        f"检测到 {call_name} 的动态几何参数在声明前使用变量：{key}={raw_value}，"
                        f"`{identifier}` 必须先声明再用于 x/y/w/h。"
                    )
                raise ValueError(
                    f"检测到 {call_name} 的动态几何参数引用了未声明变量：{key}={raw_value}，"
                    f"`{identifier}` 未在当前 PPT 代码中定义。"
                )

    @classmethod
    def _js_declaration_positions(cls, code: str) -> dict[str, list[int]]:
        declaration_positions: dict[str, list[int]] = {}
        declaration_pattern = re.compile(r"\b(?:const|let|var)\s+([^;\n]+)")
        identifier_pattern = re.compile(r"(?:^|,)\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*(?==|,|$)")
        for declaration in declaration_pattern.finditer(code):
            segment = declaration.group(1)
            for identifier in identifier_pattern.finditer(segment):
                name = identifier.group(1)
                declaration_positions.setdefault(name, []).append(declaration.start(1) + identifier.start(1))
        return declaration_positions

    @classmethod
    def _js_function_parameters_in_scope(cls, code: str, option_start_index: int) -> set[str]:
        parameters: set[str] = set()
        function_pattern = re.compile(
            r"\bfunction(?:\s+[A-Za-z_$][A-Za-z0-9_$]*)?\s*\(([^)]*)\)\s*\{",
            flags=re.DOTALL,
        )
        for match in function_pattern.finditer(code):
            parameters.update(
                cls._parameters_from_block_scope(
                    code,
                    option_start_index,
                    match.end() - 1,
                    match.group(1),
                )
            )

        arrow_patterns = [
            re.compile(r"\(([^()]*)\)\s*=>\s*\{", flags=re.DOTALL),
            re.compile(r"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*=>\s*\{"),
        ]
        for pattern in arrow_patterns:
            for match in pattern.finditer(code):
                parameter_text = match.group(1)
                parameters.update(
                    cls._parameters_from_block_scope(
                        code,
                        option_start_index,
                        match.end() - 1,
                        parameter_text,
                    )
                )
        return parameters

    @classmethod
    def _parameters_from_block_scope(
        cls,
        code: str,
        option_start_index: int,
        open_brace_index: int,
        parameter_text: str,
    ) -> set[str]:
        open_brace = code.find("{", open_brace_index)
        if open_brace < 0:
            return set()
        block = cls._scan_balanced_segment(code, open_brace, "{", "}")
        if not block:
            return set()
        _, block_end = block
        if not (open_brace < option_start_index < block_end):
            return set()
        return cls._js_parameter_names(parameter_text)

    @staticmethod
    def _js_parameter_names(parameter_text: str) -> set[str]:
        names: set[str] = set()
        for part in parameter_text.split(","):
            name = part.split("=", 1)[0].strip()
            if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name):
                names.add(name)
        return names

    @staticmethod
    def _allow_dynamic_geometry() -> bool:
        raw = os.getenv("DIRECTIONAI_PPT_ALLOW_DYNAMIC_GEOMETRY", "").strip().lower()
        if raw in {"0", "false", "no", "off"}:
            return False
        return True

    @staticmethod
    def _js_identifiers_in_expression(expression: str) -> list[str]:
        expression = re.sub(r"([\"'`])(?:\\.|(?!\1).)*\1", "", expression)
        ignored = {
            "Infinity",
            "Math",
            "NaN",
            "Number",
            "false",
            "null",
            "parseFloat",
            "parseInt",
            "true",
            "undefined",
        }
        identifiers: list[str] = []
        for match in re.finditer(r"\b[A-Za-z_$][A-Za-z0-9_$]*\b", expression):
            name = match.group(0)
            if name in ignored:
                continue
            if match.start() > 0 and expression[match.start() - 1] == ".":
                continue
            identifiers.append(name)
        return list(dict.fromkeys(identifiers))

    def _validate_generated_slide_code(
        self,
        code: str,
        image_path: str | None,
        slide: SlideOutline | None = None,
        content_requirements: str = "",
    ) -> None:
        """
        约束图片使用策略：
        - 无图片资产时，禁止 addImage / 远程 URL / preencoded 占位图
        - 有图片资产时，只允许引用该本地图片，不允许远程 URL 或伪图片
        """
        if self._book_ppt_qa_enabled():
            self._validate_book_chapter_label(code, slide, content_requirements)
            self._validate_book_source_page_labels(code, slide, content_requirements)
        if self._strict_book_ppt_qa_enabled():
            self._validate_book_visible_code_terms(code, slide, content_requirements)
            self._validate_book_visible_example_anchors(code, slide, content_requirements)

        forbidden_markers = [
            "https://",
            "http://",
            "data:image",
            "images.unsplash.com",
            "preencoded.png",
            ".svg",
        ]
        comment_stripped_code = self._strip_js_comments(code)
        resource_scan_code = self._resource_reference_text_for_scan(comment_stripped_code)
        lowered_code = comment_stripped_code.lower()
        lowered_resources = resource_scan_code.lower()

        if image_path is None:
            if "addimage(" in lowered_code:
                raise ValueError(self._no_image_addimage_error_template.strip())
            for marker in forbidden_markers:
                if marker in lowered_resources:
                    raise ValueError(
                        self._no_image_resource_error_template.format(marker=marker).strip()
                    )
            self._validate_addtext_rich_text_arrays(code)
            self._validate_geometry_constraints(code)
            self._validate_book_readability_layout(code)
            return

        for marker in forbidden_markers:
            if marker in lowered_resources:
                raise ValueError(
                    self._illegal_image_reference_error_template.format(marker=marker).strip()
                )

        path_literals = re.findall(r'addImage\s*\(\s*\{[^}]*?\bpath\s*:\s*["\']([^"\']+)["\']', code, re.DOTALL)
        invalid = [p for p in path_literals if p != image_path]
        if invalid:
            raise ValueError(
                self._unauthorized_image_path_error_template.format(image_path=invalid[0]).strip()
            )
        if self._require_image_asset_usage() and image_path not in path_literals:
            message = f"已提供本地图片资产，必须使用 addImage({{ path: '{image_path}' }}) 引用该图片。"
            if self._strict_image_asset_usage_required():
                raise ValueError(message)
            slide_index = slide.slide_index if slide is not None else "?"
            logger.warning("[Planner] 第 %s 页未采用本地图片资产：%s", slide_index, image_path)
            print(f"[Planner] 第 {slide_index} 页未采用本地图片资产，继续生成。")

        self._validate_addtext_rich_text_arrays(code)
        self._validate_geometry_constraints(code)
        self._validate_book_readability_layout(code)

    @staticmethod
    def _strip_js_comments(code: str) -> str:
        result: list[str] = []
        i = 0
        in_string = False
        quote = ""
        escape = False
        while i < len(code):
            ch = code[i]
            nxt = code[i + 1] if i + 1 < len(code) else ""
            if in_string:
                result.append(ch)
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == quote:
                    in_string = False
                    quote = ""
                i += 1
                continue
            if ch in {'"', "'", "`"}:
                in_string = True
                quote = ch
                result.append(ch)
                i += 1
                continue
            if ch == "/" and nxt == "/":
                while i < len(code) and code[i] not in "\r\n":
                    i += 1
                result.append("\n")
                continue
            if ch == "/" and nxt == "*":
                i += 2
                while i + 1 < len(code) and not (code[i] == "*" and code[i + 1] == "/"):
                    i += 1
                i += 2
                result.append(" ")
                continue
            result.append(ch)
            i += 1
        return "".join(result)

    @classmethod
    def _strip_visible_addtext_text_for_resource_scan(cls, code: str) -> str:
        """Remove visible addText strings before scanning for image resources.

        Teaching slides may legitimately show URLs as examples, especially in
        programming lessons about slicing, HTTP, or paths. Those URLs are not
        image resources. Keep URLs in non-visible code positions so remote asset
        references still fail in no-image mode.
        """
        text = cls._replace_addtext_calls_for_resource_scan(code)
        text = re.sub(
            r"(\btext\s*:\s*)([\"'`])((?:\\.|(?!\2).)*?)\2",
            r"\1\2__VISIBLE_TEXT__\2",
            text,
            flags=re.DOTALL,
        )
        text = re.sub(
            r"(\bcontent\s*:\s*)([\"'`])((?:\\.|(?!\2).)*?)\2",
            r"\1\2__VISIBLE_TEXT__\2",
            text,
            flags=re.DOTALL,
        )
        return text

    @classmethod
    def _resource_reference_text_for_scan(cls, code: str) -> str:
        """Return only strings that plausibly point to external media resources."""
        text = cls._strip_visible_addtext_text_for_resource_scan(code)
        parts: list[str] = []

        for match in re.finditer(r"\.addImage\s*\(", text, flags=re.IGNORECASE):
            open_paren = text.find("(", match.start())
            segment = cls._scan_balanced_segment(text, open_paren, "(", ")")
            if segment:
                parts.append(segment[0])

        resource_property_pattern = re.compile(
            r"\b(?:path|src|href|imageUrl|imgUrl|backgroundImage)\s*:\s*"
            r"([\"'`])((?:\\.|(?!\1).)*?)\1",
            flags=re.DOTALL | re.IGNORECASE,
        )
        parts.extend(match.group(2) for match in resource_property_pattern.finditer(text))

        for marker in ("data:image", "images.unsplash.com", "preencoded.png"):
            if marker in text.lower():
                parts.append(marker)
        return "\n".join(parts)

    @classmethod
    def _replace_addtext_calls_for_resource_scan(cls, code: str) -> str:
        replacements: list[tuple[int, int, str]] = []
        for match in re.finditer(r"\.addText\s*\(", code):
            open_paren = code.find("(", match.start())
            segment = cls._scan_balanced_segment(code, open_paren, "(", ")")
            if segment is None:
                continue
            _, end = segment
            replacements.append(
                (match.start(), end, '.addText("__VISIBLE_TEXT__", {})')
            )

        for start, end, replacement in reversed(replacements):
            code = code[:start] + replacement + code[end:]
        return code

    @staticmethod
    def _replace_first_addtext_string_arg(code: str, quote: str) -> str:
        quote_pattern = re.escape(quote)
        pattern = re.compile(
            rf"(\.addText\s*\(\s*){quote_pattern}((?:\\.|(?!{quote_pattern}).)*?){quote_pattern}",
            flags=re.DOTALL,
        )
        return pattern.sub(
            lambda match: f"{match.group(1)}{quote}__VISIBLE_TEXT__{quote}",
            code,
        )

    @classmethod
    def _book_slide_metadata_labels(
        cls,
        slide: SlideOutline | None,
        content_requirements: str,
    ) -> tuple[str, list[str]]:
        if slide is None or not content_requirements:
            return "", []
        section = cls._book_slide_requirement_section(slide, content_requirements)
        section_label = ""
        focus_levels: list[str] = []
        if section:
            section_match = re.search(r"-\s*教材章节标签\s*[:：]\s*([^\n]+)", section)
            if section_match:
                section_label = section_match.group(1).strip()
            focus_match = re.search(r"-\s*考纲层级\s*[:：]\s*([^\n]+)", section)
            if focus_match:
                raw_levels = focus_match.group(1).strip()
                if raw_levels and raw_levels not in {"无", "不标注", "none", "None", "-"}:
                    for item in re.split(r"\s*/\s*|、|，|,|；|;", raw_levels):
                        level = item.strip()
                        if level and level not in focus_levels:
                            focus_levels.append(level)
            if section_label or focus_levels:
                return section_label, focus_levels
        section_label = cls._infer_runtime_book_section_label(slide, content_requirements)
        focus_levels = cls._infer_runtime_study_focus_levels(slide, content_requirements, section_label)
        return section_label, focus_levels

    @staticmethod
    def _normalize_book_metadata_text(text: str) -> str:
        return re.sub(r"[\s，,。．.、；;：:（）()《》<>“”\"'‘’·\-—_/]+", "", str(text or ""))

    @classmethod
    def _book_study_focus_metadata_enabled(cls, content_requirements: str) -> bool:
        if not cls._book_ppt_qa_enabled() or not content_requirements:
            return False
        return bool(
            (
                re.search(r"教材章节标签\s*[:：]", content_requirements)
                and re.search(r"考纲层级\s*[:：]", content_requirements)
            )
            or (
                "本课考纲层次映射" in content_requirements
                and (
                    "本课优先保留的教材目录标题" in content_requirements
                    or "本课必须显式保留的教材目录标题" in content_requirements
                )
            )
        )

    @classmethod
    def _infer_runtime_book_section_label(
        cls,
        slide: SlideOutline,
        content_requirements: str,
    ) -> str:
        candidates = cls._book_runtime_section_candidates(content_requirements)
        if not candidates:
            return ""
        lesson_scope = cls._runtime_lesson_scope_label(candidates)
        title = str(getattr(slide, "topic", "") or "")
        objective = str(getattr(slide, "objective", "") or "")
        layout = str(getattr(getattr(slide, "layout", ""), "value", getattr(slide, "layout", "")) or "").lower()
        if layout in {"cover", "toc", "closing"}:
            return lesson_scope
        if any(marker in title for marker in ("本课学习路径", "本课学习目标", "本课小结", "下一课预告")):
            return lesson_scope
        if "考纲要求" in title and "按知识点" in title:
            return lesson_scope

        haystack = cls._normalize_book_metadata_text("\n".join([title, objective]))
        scored: list[tuple[int, int, int, str]] = []
        for order, item in enumerate(candidates):
            number = str(item.get("number") or "")
            heading = str(item.get("heading") or "")
            label = str(item.get("label") or "")
            description = str(item.get("description") or "")
            score = 0
            if label and cls._normalize_book_metadata_text(label) in haystack:
                score += 260
            if number and cls._normalize_book_metadata_text(number) in haystack:
                score += 220
            if heading and cls._normalize_book_metadata_text(heading) in haystack:
                score += 180 + min(len(heading), 20)
            for term in cls._book_focus_terms(description):
                term_key = cls._normalize_book_metadata_text(term)
                if term_key and term_key in haystack:
                    score += 18 + min(len(term_key), 16)
            if score > 0:
                depth = number.count(".")
                scored.append((score, depth, -order, label))
        if not scored:
            return lesson_scope
        scored.sort(reverse=True)
        return scored[0][3] or lesson_scope

    @classmethod
    def _infer_runtime_study_focus_levels(
        cls,
        slide: SlideOutline,
        content_requirements: str,
        section_label: str,
    ) -> list[str]:
        entries = cls._book_study_focus_entries(content_requirements)
        if not entries:
            return []
        title = str(getattr(slide, "topic", "") or "")
        objective = str(getattr(slide, "objective", "") or "")
        focus_topic = cls._runtime_focus_topic(title, section_label, entries, content_requirements)
        if not focus_topic:
            return []
        topic_entries = entries.get(focus_topic, {})
        if not topic_entries:
            return []
        if "考纲要求" in title and focus_topic in title:
            return cls._ordered_focus_level_names(topic_entries.keys())
        if any(marker in title for marker in ("本课学习路径", "本课学习目标", "本课小结", "下一课预告")):
            return []

        haystack = cls._normalize_book_metadata_text("\n".join([title, objective]))
        matched: list[str] = []
        for level in cls._ordered_focus_level_names(topic_entries.keys()):
            values = topic_entries.get(level, [])
            for value in values:
                terms = cls._book_focus_terms(value)
                if any(cls._normalize_book_metadata_text(term) in haystack for term in terms):
                    matched.append(level)
                    break
        return cls._ordered_focus_level_names(matched)

    @classmethod
    def _runtime_focus_topic(
        cls,
        title: str,
        section_label: str,
        entries: dict[str, dict[str, list[str]]],
        content_requirements: str,
    ) -> str:
        match = re.search(r"考纲要求[:：]\s*([^\n]+)", title)
        if match:
            topic_text = cls._normalize_book_metadata_text(match.group(1))
            for topic in entries:
                if cls._normalize_book_metadata_text(topic) in topic_text or topic_text in cls._normalize_book_metadata_text(topic):
                    return topic

        label_number_match = re.match(r"\s*(\d+(?:\.\d+)*)\s+(.+)", section_label or "")
        if label_number_match:
            number = label_number_match.group(1)
            heading = label_number_match.group(2)
            candidates = cls._book_runtime_section_candidates(content_requirements)
            parts = number.split(".")
            broad_number = ".".join(parts[:2]) if len(parts) >= 2 else number
            for item in candidates:
                if str(item.get("number") or "") == broad_number:
                    broad_heading = str(item.get("heading") or "")
                    for topic in entries:
                        if cls._normalize_book_metadata_text(topic) == cls._normalize_book_metadata_text(broad_heading):
                            return topic
            for topic in entries:
                topic_key = cls._normalize_book_metadata_text(topic)
                if topic_key and (
                    topic_key in cls._normalize_book_metadata_text(heading)
                    or cls._normalize_book_metadata_text(heading) in topic_key
                ):
                    return topic

        title_key = cls._normalize_book_metadata_text(title)
        for topic in entries:
            topic_key = cls._normalize_book_metadata_text(topic)
            if topic_key and topic_key in title_key:
                return topic
        return ""

    @classmethod
    def _book_runtime_section_candidates(cls, content_requirements: str) -> list[dict[str, str]]:
        candidates: list[dict[str, str]] = []
        by_key: dict[str, dict[str, str]] = {}

        def add(number: str, heading: str, description: str = "") -> None:
            number = re.sub(r"\.0$", "", str(number or "").strip())
            heading = re.sub(r"[（(]\s*导入\s*[）)]", "", str(heading or "")).strip()
            heading = re.split(r"[（(]\s*教材页码", heading, maxsplit=1)[0].strip()
            if not number or not heading:
                return
            label = f"{number} {heading}".strip()
            key = cls._normalize_book_metadata_text(label)
            if not key:
                return
            if key in by_key:
                if description:
                    previous = by_key[key].get("description", "")
                    by_key[key]["description"] = "\n".join(part for part in [previous, str(description).strip()] if part)
                return
            item = {
                "number": number,
                "heading": heading,
                "label": label,
                "description": str(description or "").strip(),
            }
            by_key[key] = item
            candidates.append(item)

        required_titles = cls._markdown_section(content_requirements, "本课优先保留的教材目录标题")
        if not required_titles:
            required_titles = cls._markdown_section(content_requirements, "本课必须显式保留的教材目录标题")
        for raw_line in required_titles.splitlines():
            match = re.match(r"-\s*(\d+(?:\.\d+)*)\s+(.+?)\s*$", raw_line.strip())
            if match:
                add(match.group(1), match.group(2))

        toc_section = cls._markdown_section(content_requirements, "本课目录索引")
        current: tuple[str, str, list[str]] | None = None

        def flush_current() -> None:
            nonlocal current
            if current is None:
                return
            number, heading, desc_lines = current
            add(number, heading, "\n".join(desc_lines))
            current = None

        for raw_line in toc_section.splitlines():
            line = raw_line.rstrip()
            match = re.match(r"-\s*(\d+(?:\.\d+)*)\s+(.+?)\s*$", line.strip())
            if match:
                flush_current()
                heading = re.split(r"[（(]\s*教材页码", match.group(2), maxsplit=1)[0].strip()
                current = (match.group(1), heading, [])
                continue
            if current is not None:
                current[2].append(line.strip())
        flush_current()
        return candidates

    @classmethod
    def _runtime_lesson_scope_label(cls, candidates: list[dict[str, str]]) -> str:
        if not candidates:
            return ""
        first = str(candidates[0].get("number") or "").strip()
        last = str(candidates[-1].get("number") or "").strip()
        if first and last and first != last:
            return f"本课范围：{first}-{last}"
        return str(candidates[0].get("label") or "").strip()

    @classmethod
    def _book_study_focus_entries(cls, content_requirements: str) -> dict[str, dict[str, list[str]]]:
        section = cls._markdown_section(content_requirements, "本课考纲层次映射")
        if not section:
            return {}
        entries: dict[str, dict[str, list[str]]] = {}
        current_level = ""
        for raw_line in section.splitlines():
            line = raw_line.strip()
            level_match = re.match(r"([^：:\s]+?)层次[:：]", line)
            if level_match:
                current_level = level_match.group(1).strip()
                continue
            if not current_level:
                continue
            item_match = re.match(r"-\s*([^：:]+?)[:：]\s*(.+)$", line)
            if not item_match:
                continue
            topic = item_match.group(1).strip()
            value = item_match.group(2).strip()
            if not topic or not value:
                continue
            entries.setdefault(topic, {}).setdefault(current_level, []).append(value)
        return entries

    @staticmethod
    def _markdown_section(content: str, heading: str) -> str:
        pattern = rf"(?:^|\n)##\s*{re.escape(heading)}[^\n]*\n(.*?)(?=\n##\s|\Z)"
        match = re.search(pattern, str(content or ""), flags=re.DOTALL)
        return match.group(1).strip() if match else ""

    @classmethod
    def _book_focus_terms(cls, text: str) -> list[str]:
        stopwords = {
            "定义",
            "含义",
            "区别",
            "联系",
            "区别和联系",
            "基本原则",
            "本课",
            "教材",
            "内容",
            "索引摘要",
            "不替代原文",
            "教材页码",
            "内部PDF",
            "内容类型",
        }
        raw_parts = re.split(r"[。；;，,、/（）()《》“”\"'·\s]+", str(text or ""))
        terms: list[str] = []
        for raw in raw_parts:
            piece = raw.strip(" ：:")
            if not piece:
                continue
            subparts = [piece]
            if "的" in piece and len(piece) > 5:
                subparts.extend(part for part in piece.split("的") if part)
            for item in subparts:
                cleaned = re.sub(r"^(如何|能够|可以|以及|并|和|与)", "", item).strip()
                cleaned = re.sub(r"(等|及其)$", "", cleaned).strip()
                if len(cleaned) < 2 or cleaned in stopwords:
                    continue
                if cleaned not in terms:
                    terms.append(cleaned)
        return terms

    @staticmethod
    def _ordered_focus_level_names(levels: Iterable[str]) -> list[str]:
        order = ["识记", "理解", "领会", "应用", "运用", "简单应用", "综合应用"]
        unique = [str(level).strip() for level in levels if str(level).strip()]
        ranked = {name: index for index, name in enumerate(order)}
        return sorted(dict.fromkeys(unique), key=lambda item: (ranked.get(item, len(order)), item))

    @classmethod
    def _validate_book_chapter_label(
        cls,
        code: str,
        slide: SlideOutline | None,
        content_requirements: str = "",
    ) -> None:
        if not cls._book_ppt_qa_enabled():
            return

        expected_number = (os.getenv("DIRECTIONAI_BOOK_CHAPTER_NUMBER") or "").strip()
        if not expected_number:
            return

        normalized = cls._decode_unicode_escape_literals(code)
        expected_labels = cls._expected_chapter_labels(expected_number)
        allowed_labels = set(expected_labels)
        allowed_labels.update(cls._book_slide_allowed_chapter_labels(slide, content_requirements))
        found_labels = {
            re.sub(r"\s+", "", match.group(0))
            for match in re.finditer(r"第\s*([0-9一二三四五六七八九十百两]+)\s*章", normalized)
        }
        wrong_labels = sorted(label for label in found_labels if label not in allowed_labels)
        if wrong_labels:
            raise ValueError(
                "电子书课件章节号漂移："
                f"当前章节应使用 {'/'.join(sorted(expected_labels))}，"
                "只有本页教材证据明确出现的后续章节号可以作为承接引用，"
                f"但页面代码出现 {', '.join(wrong_labels)}。"
            )

        if slide is not None and slide.slide_index == 0 and not any(label in normalized for label in expected_labels):
            raise ValueError(
                "电子书课件封面必须显式展示当前章节号："
                f"{'/'.join(sorted(expected_labels))}。"
            )

    @classmethod
    def _book_slide_allowed_chapter_labels(
        cls,
        slide: SlideOutline | None,
        content_requirements: str,
    ) -> set[str]:
        if slide is None or not content_requirements:
            return set()
        section = cls._book_slide_requirement_section(slide, content_requirements)
        if not section:
            return set()
        return {
            re.sub(r"\s+", "", match.group(0))
            for match in re.finditer(r"第\s*([0-9一二三四五六七八九十百两]+)\s*章", section)
        }

    @classmethod
    def _validate_book_source_page_labels(
        cls,
        code: str,
        slide: SlideOutline | None,
        content_requirements: str,
    ) -> None:
        if not cls._book_ppt_qa_enabled() or slide is None:
            return
        allowed_pages = cls._book_slide_source_pages(slide, content_requirements)
        if not allowed_pages:
            return

        normalized = cls._decode_unicode_escape_literals(cls._strip_js_comments(code))
        cited_pages = {int(match.group(1)) for match in re.finditer(r"\bp\.\s*(\d+)\b", normalized, flags=re.IGNORECASE)}
        if not cited_pages:
            return

        invalid_pages = sorted(page for page in cited_pages if page not in allowed_pages)
        if invalid_pages:
            allowed_label = cls._compact_page_set(allowed_pages)
            invalid_label = ", ".join(f"p.{page}" for page in invalid_pages[:6])
            raise ValueError(
                "电子书课件教材页码漂移："
                f"本页教材依据只能使用 {allowed_label}，但页面代码出现 {invalid_label}。"
                "不要把 PPT 页序误写成教材页码。"
            )

    @classmethod
    def _validate_book_visible_code_terms(
        cls,
        code: str,
        slide: SlideOutline | None,
        content_requirements: str,
    ) -> None:
        if not cls._strict_book_ppt_qa_enabled() or slide is None or not content_requirements:
            return

        visible_text = cls._extract_book_visible_text_literals(code)
        if not visible_text:
            return

        normalized_requirements = cls._normalize_book_code_term_text(content_requirements)
        candidates = cls._visible_code_terms(visible_text)
        if cls._book_should_enforce_visible_code_expressions(slide, content_requirements):
            unsupported_expressions = sorted(
                expression
                for expression in cls._visible_code_expressions(visible_text)
                if cls._normalize_book_code_term_text(expression) not in normalized_requirements
            )
            if unsupported_expressions:
                sample = ", ".join(unsupported_expressions[:6])
                raise ValueError(
                    "电子书课件内容越界："
                    f"页面可见索引/切片表达式 {sample} 没有出现在本页蓝图或章节原文证据中。"
                    "练习题可以留空让学生作答，但不要编造教材外表达式或答案。"
                )

        unsupported = sorted(
            term
            for term in candidates
            if len(term) > 1 and term.lower() not in normalized_requirements
        )
        if unsupported:
            sample = ", ".join(unsupported[:6])
            raise ValueError(
                "电子书课件内容越界："
                f"页面可见代码调用/方法名/API 名 {sample} 没有出现在本页蓝图或章节原文证据中。"
                "练习、案例、方法列表和代码示例只能改写教材证据，不能引入后续章节概念或经验性断言。"
            )

    @classmethod
    def _validate_book_visible_example_anchors(
        cls,
        code: str,
        slide: SlideOutline | None,
        content_requirements: str,
    ) -> None:
        if not cls._strict_book_ppt_qa_enabled() or slide is None or not content_requirements:
            return

        section = cls._book_slide_requirement_section(slide, content_requirements)
        if not section or not cls._book_section_requires_visible_examples(section):
            return

        page_type = cls._book_slide_type(section)
        if page_type not in {"case_analysis", "guided_practice"}:
            return

        groups = cls._book_visible_example_anchor_groups(section)
        if not groups:
            return

        visible_text = cls._extract_book_visible_text_literals(code)
        visible_normalized = cls._normalize_book_anchor_text(visible_text)
        if not visible_normalized:
            raise ValueError(
                "电子书课件证据弱对应：本页教材依据包含可执行原文示例，"
                "但页面没有可见文本承载原文表达式与结果。"
            )

        satisfied = [cls._book_example_group_is_visible(visible_normalized, group) for group in groups]
        if page_type == "case_analysis":
            failed = [group for group, ok in zip(groups, satisfied) if not ok]
            if not failed:
                return
            missing = cls._book_missing_example_anchor_label(visible_normalized, failed[0])
            raise ValueError(
                "电子书课件证据弱对应：案例辨析页必须展示教材原文中的关键表达式与输出，"
                f"不能只概述规则；缺少 {missing}。"
            )

        if any(satisfied):
            return
        missing = cls._book_missing_example_anchor_label(visible_normalized, groups[0])
        raise ValueError(
            "电子书课件证据弱对应：本页教材依据包含可执行原文示例，"
            f"页面必须至少展示一组原文表达式与结果；缺少 {missing}。"
        )

    @staticmethod
    def _book_section_requires_visible_examples(section: str) -> bool:
        return (
            "完整可执行原文证据" in section
            or "可见证明对象必须保留教材原文表达式与输出" in section
        )

    @staticmethod
    def _book_slide_type(section: str) -> str:
        match = re.search(r"-\s*页型[:：]\s*([A-Za-z_]+)", section)
        return match.group(1) if match else ""

    @classmethod
    def _book_visible_example_anchor_groups(cls, section: str) -> list[list[str]]:
        snippets = [
            match.group(1)
            for match in re.finditer(
                r"完整可执行原文证据(?:（[^）]*）)?[:：]\s*(.*?)(?=；\s*完整可执行原文证据|；\s*p\.\s*\d+\s*[:：]|\n-\s|$)",
                section,
                flags=re.DOTALL | re.IGNORECASE,
            )
        ]
        groups: list[list[str]] = []
        for snippet in snippets[:3]:
            anchors = cls._book_example_required_anchors(snippet)
            if anchors:
                groups.append(anchors)
        return groups

    @classmethod
    def _book_example_required_anchors(cls, snippet: str) -> list[str]:
        text = cls._decode_unicode_escape_literals(cls._decode_js_string_fragment(snippet))
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return []

        setup_assignments = cls._book_assignment_anchors(text, require_slice=False)
        slice_assignments = cls._book_assignment_anchors(text, require_slice=True)
        expressions = cls._book_expression_anchors(text)
        results = cls._book_result_anchors(text, setup_assignments)

        anchors: list[str]
        if slice_assignments:
            anchors = [*slice_assignments[:2], *results[:1]]
        elif setup_assignments and expressions:
            anchors = [*setup_assignments[:1], *expressions[:2], *results[:2]]
        elif expressions:
            anchors = [*expressions[:2], *results[:1]]
        else:
            anchors = [*setup_assignments[:1], *results[:1]]

        normalized: list[str] = []
        seen: set[str] = set()
        for anchor in anchors:
            cleaned = cls._clean_book_example_anchor(anchor)
            if not cleaned:
                continue
            key = cls._normalize_book_anchor_text(cleaned)
            if len(key) < 3 or key in seen:
                continue
            normalized.append(cleaned)
            seen.add(key)
        return normalized

    @classmethod
    def _book_assignment_anchors(cls, text: str, *, require_slice: bool) -> list[str]:
        anchors: list[str] = []
        pattern = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\s*=\s*")
        for match in pattern.finditer(text):
            if cls._position_inside_quote(text, match.start()):
                continue
            anchor = cls._consume_book_assignment(text, match.start(), match.end()).strip()
            if not anchor:
                continue
            is_index_or_slice_assignment = bool(
                re.search(r"=\s*[A-Za-z_][A-Za-z0-9_]*\s*\[[^\]]+\]", anchor)
            )
            if require_slice and not is_index_or_slice_assignment:
                continue
            if not require_slice and is_index_or_slice_assignment:
                continue
            anchors.append(anchor)
        return anchors

    @staticmethod
    def _position_inside_quote(text: str, position: int) -> bool:
        quote: str | None = None
        escape = False
        for char in text[:position]:
            if quote:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == quote:
                    quote = None
                continue
            if char in {"'", '"'}:
                quote = char
        return quote is not None

    @classmethod
    def _consume_book_assignment(cls, text: str, start: int, rhs_start: int) -> str:
        index = rhs_start
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            return ""

        if text[index] in {"'", '"'}:
            end = cls._consume_quoted_fragment(text, index)
        elif text[index] in {"[", "("}:
            end = cls._consume_balanced_fragment(text, index, text[index], "]" if text[index] == "[" else ")")
        else:
            rhs = text[index:]
            expr_match = re.match(r"[A-Za-z_][A-Za-z0-9_]*\s*\[[^\]]+\]", rhs)
            call_match = re.match(r"[A-Za-z_][A-Za-z0-9_]*\s*\([^)]*\)", rhs)
            if expr_match:
                end = index + expr_match.end()
            elif call_match:
                end = index + call_match.end()
            else:
                delimiter = re.search(
                    r"\s+(?:>>>|[A-Za-z_][A-Za-z0-9_]*\s*=|print\s*\(|[A-Za-z_][A-Za-z0-9_]*\s*\[)|[。；]",
                    rhs,
                )
                end = index + (delimiter.start() if delimiter else min(len(rhs), 120))
        return text[start:end]

    @staticmethod
    def _consume_quoted_fragment(text: str, start: int) -> int:
        quote = text[start]
        escape = False
        for index in range(start + 1, len(text)):
            char = text[index]
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == quote:
                return index + 1
        return len(text)

    @staticmethod
    def _consume_balanced_fragment(text: str, start: int, opener: str, closer: str) -> int:
        depth = 0
        quote: str | None = None
        escape = False
        for index in range(start, len(text)):
            char = text[index]
            if quote:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == quote:
                    quote = None
                continue
            if char in {"'", '"'}:
                quote = char
                continue
            if char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    return index + 1
        return len(text)

    @classmethod
    def _book_expression_anchors(cls, text: str) -> list[str]:
        anchors: list[str] = []
        for pattern in (
            r"\b[A-Za-z_][A-Za-z0-9_]*\[[^\]]+\]",
            r"\b[A-Za-z_][A-Za-z0-9_]*\([^)]*\)",
        ):
            for match in re.finditer(pattern, text):
                anchor = match.group(0).strip()
                if anchor not in anchors:
                    anchors.append(anchor)
        return anchors

    @classmethod
    def _book_result_anchors(cls, text: str, setup_assignments: list[str]) -> list[str]:
        results: list[str] = []
        for match in re.finditer(r"(['\"])((?:\\.|(?!\1).){2,160})\1", text):
            quote = match.group(1)
            value = match.group(2).strip()
            is_full_setup_literal = any(
                setup.strip().endswith(f"{quote}{value}{quote}")
                for setup in setup_assignments
            )
            if cls._normalize_book_anchor_text(value) and not is_full_setup_literal:
                results.append(value)
        for match in re.finditer(r"\[[^\]]+\]|\([^)]+\)", text):
            value = match.group(0).strip()
            if value:
                results.append(value)
        for match in re.finditer(
            r"(?:[A-Za-z][A-Za-z ]{1,40}|[\u4e00-\u9fff]{2,16})[:：]\s*[A-Za-z0-9_./$:-]+",
            text,
        ):
            results.append(match.group(0).strip())
        return list(dict.fromkeys(results))

    @staticmethod
    def _clean_book_example_anchor(anchor: str) -> str:
        anchor = re.sub(r"^p\.\s*\d+\s*[:：]\s*", "", anchor.strip(), flags=re.IGNORECASE)
        anchor = re.sub(r"\s+", " ", anchor).strip()
        return anchor.rstrip("。；,，")

    @staticmethod
    def _normalize_book_anchor_text(text: str) -> str:
        text = PlannerAgent._decode_unicode_escape_literals(str(text or ""))
        text = PlannerAgent._decode_js_string_fragment(text)
        text = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
        return re.sub(r"\s+", "", text).lower()

    @classmethod
    def _book_example_group_is_visible(cls, visible_normalized: str, anchors: list[str]) -> bool:
        required = [anchor for anchor in anchors if cls._normalize_book_anchor_text(anchor)]
        if not required:
            return False
        return all(cls._normalize_book_anchor_text(anchor) in visible_normalized for anchor in required)

    @classmethod
    def _book_missing_example_anchor_label(cls, visible_normalized: str, anchors: list[str]) -> str:
        for anchor in anchors:
            if cls._normalize_book_anchor_text(anchor) not in visible_normalized:
                return repr(anchor)
        return "原文表达式或输出"

    @classmethod
    def _ensure_book_visible_example_anchors(
        cls,
        code: str,
        slide: SlideOutline | None,
        content_requirements: str,
    ) -> str:
        if not cls._strict_book_ppt_qa_enabled() or slide is None or not content_requirements:
            return code

        section = cls._book_slide_requirement_section(slide, content_requirements)
        if not section or not cls._book_section_requires_visible_examples(section):
            return code

        page_type = cls._book_slide_type(section)
        if page_type not in {"case_analysis", "guided_practice"}:
            return code

        groups = cls._book_visible_example_anchor_groups(section)
        if not groups:
            return code

        visible_normalized = cls._normalize_book_anchor_text(cls._extract_book_visible_text_literals(code))
        missing_groups = [
            group for group in groups if not cls._book_example_group_is_visible(visible_normalized, group)
        ]
        if not missing_groups:
            return code

        patch_text = cls._book_example_anchor_patch_text(missing_groups)
        if not patch_text:
            return code
        return cls._append_book_example_anchor_patch(code, patch_text)

    @staticmethod
    def _book_example_anchor_patch_text(groups: list[list[str]]) -> str:
        lines = ["教材原文依据"]
        for index, group in enumerate(groups[:2], start=1):
            compact = " -> ".join(group[:5])
            lines.append(f"{index}. {compact}")
        return "\n".join(lines)

    @staticmethod
    def _append_book_example_anchor_patch(code: str, text: str) -> str:
        text_literal = json.dumps(text, ensure_ascii=False)
        patch = (
            "\n// Auto-added by book strict QA: preserve source example evidence.\n"
            'slide.addShape("rect", { x: 0.68, y: 6.04, w: 12.0, h: 0.78, '
            'fill: { color: "FFFFFF", transparency: 8 }, '
            'line: { color: "FF6B35", transparency: 25 } });\n'
            f"slide.addText({text_literal}, "
            '{ x: 0.82, y: 6.12, w: 11.68, h: 0.62, fontSize: 7.2, '
            'fontFace: "PingFang SC", color: "1A2332", margin: 0.02, breakLine: false });\n'
        )
        stripped = code.rstrip()
        if stripped.startswith("{") and stripped.endswith("}"):
            insert_at = stripped.rfind("}")
            return stripped[:insert_at].rstrip() + patch + stripped[insert_at:]
        return stripped + patch

    @classmethod
    def _extract_book_visible_text_literals(cls, code: str) -> str:
        text = cls._decode_unicode_escape_literals(cls._strip_js_comments(code))
        values: list[str] = []
        for quote, raw in re.findall(r"(?<![A-Za-z0-9_])(?:addText|text|content)\s*\(\s*([\"'`])((?:\\.|(?!\1).)*?)\1", text, flags=re.DOTALL):
            values.append(cls._decode_js_string_fragment(raw))
        for quote, raw in re.findall(r"\.addText\s*\(\s*\[\s*\{\s*text\s*:\s*([\"'`])((?:\\.|(?!\1).)*?)\1", text, flags=re.DOTALL):
            values.append(cls._decode_js_string_fragment(raw))
        for quote, raw in re.findall(r"\b(?:text|content)\s*:\s*([\"'`])((?:\\.|(?!\1).)*?)\1", text, flags=re.DOTALL):
            values.append(cls._decode_js_string_fragment(raw))
        return "\n".join(values)

    @staticmethod
    def _decode_html_entity_text(text: str) -> str:
        if not text or not re.search(r"&(?:#\d+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]+);|;(?=&#(?:\d+|[xX][0-9A-Fa-f]+);)", text):
            return text
        text = re.sub(r";(?=&#(?:\d+|[xX][0-9A-Fa-f]+);)", "", text)
        for _ in range(3):
            decoded = html.unescape(text)
            if decoded == text:
                break
            text = decoded
        return text

    @classmethod
    def _decode_js_string_fragment(cls, text: str) -> str:
        def replace_unicode_escape(match: re.Match) -> str:
            try:
                return chr(int(match.group(1), 16))
            except ValueError:
                return match.group(0)

        replacements = {
            r"\n": "\n",
            r"\r": "\n",
            r"\t": "\t",
            r"\"": '"',
            r"\'": "'",
            r"\`": "`",
            r"\\": "\\",
        }
        text = re.sub(r"\\u([0-9a-fA-F]{4})", replace_unicode_escape, text)
        for old, new in replacements.items():
            text = text.replace(old, new)
        return cls._decode_html_entity_text(text)

    @classmethod
    def _decode_html_entities_in_visible_addtext_strings(cls, code: str) -> str:
        if not re.search(r"&(?:#\d+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]+);|;(?=&#(?:\d+|[xX][0-9A-Fa-f]+);)", code):
            return code
        replacements: list[tuple[int, int, str]] = []
        for match in re.finditer(r"\.addText\s*\(", code):
            open_paren = code.find("(", match.start())
            if open_paren < 0:
                continue
            call_segment = cls._scan_balanced_segment(code, open_paren, "(", ")")
            if not call_segment:
                continue
            call_body, _ = call_segment
            for literal in re.finditer(r"([\"'`])((?:\\.|(?!\1).)*?)\1", call_body, flags=re.DOTALL):
                quote = literal.group(1)
                raw_text = literal.group(2)
                decoded = cls._decode_js_string_fragment(raw_text)
                if decoded == raw_text:
                    continue
                replacement = f"{quote}{cls._encode_js_string_fragment(decoded, quote)}{quote}"
                start_index = open_paren + literal.start()
                replacements.append((start_index, open_paren + literal.end(), replacement))
        for start_index, end_index, replacement in sorted(replacements, reverse=True):
            code = code[:start_index] + replacement + code[end_index:]
        return code

    @staticmethod
    def _normalize_book_code_term_text(text: str) -> str:
        return re.sub(r"\s+", "", text.lower())

    @classmethod
    def _visible_code_terms(cls, text: str) -> set[str]:
        normalized = cls._decode_unicode_escape_literals(text)
        terms = {
            match.group(1)
            for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", normalized)
        }
        terms.update(
            match.group(1)
            for match in re.finditer(r"\.\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(", normalized)
        )
        terms.update(cls._visible_code_identifier_terms(normalized))
        return {term for term in terms if term not in cls._book_code_term_stopwords()}

    @classmethod
    def _visible_code_expressions(cls, text: str) -> set[str]:
        normalized = cls._decode_unicode_escape_literals(text)
        expressions: set[str] = set()
        pattern = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\s*\[[^\]]*\](?:\[[^\]]*\])*")
        for match in pattern.finditer(normalized):
            expression = re.sub(r"\s+", "", match.group(0))
            if not cls._is_high_risk_visible_code_expression(expression, normalized[match.end():]):
                continue
            expressions.add(expression)
        return expressions

    @classmethod
    def _book_should_enforce_visible_code_expressions(
        cls,
        slide: SlideOutline | None,
        content_requirements: str,
    ) -> bool:
        if slide is None or not content_requirements:
            return False
        section = cls._book_slide_requirement_section(slide, content_requirements)
        page_type = cls._book_slide_type(section)
        return page_type == "case_analysis"

    @staticmethod
    def _is_high_risk_visible_code_expression(expression: str, following_text: str) -> bool:
        if not expression or "[]" in expression:
            return False
        if ":" in expression:
            return True
        if expression.count("[") >= 2:
            return True
        if re.match(r"\s*=", following_text):
            return True
        return False

    @classmethod
    def _redact_unsupported_book_visible_expressions(
        cls,
        code: str,
        slide: SlideOutline | None,
        content_requirements: str,
    ) -> str:
        if not cls._strict_book_ppt_qa_enabled() or slide is None or not content_requirements:
            return code
        section = cls._book_slide_requirement_section(slide, content_requirements)
        page_type = cls._book_slide_type(section)
        if page_type == "case_analysis":
            return code
        if not cls._book_should_enforce_visible_code_expressions(slide, content_requirements):
            return code
        unsupported = cls._unsupported_book_visible_code_expressions(
            cls._extract_book_visible_text_literals(code),
            content_requirements,
        )
        if not unsupported:
            return code
        return cls._replace_js_string_literal_fragments(
            code,
            {expression: cls._book_expression_redaction(expression) for expression in unsupported},
        )

    @classmethod
    def _unsupported_book_visible_code_expressions(
        cls,
        visible_text: str,
        content_requirements: str,
    ) -> list[str]:
        normalized_requirements = cls._normalize_book_code_term_text(content_requirements)
        return sorted(
            expression
            for expression in cls._visible_code_expressions(visible_text)
            if cls._normalize_book_code_term_text(expression) not in normalized_requirements
        )

    @staticmethod
    def _book_expression_redaction(expression: str) -> str:
        if "[" in expression and expression.count("[") >= 2:
            return "表达式：____"
        if ":" in expression:
            return "切片表达式：____"
        return "____"

    @classmethod
    def _replace_js_string_literal_fragments(
        cls,
        code: str,
        replacements: dict[str, str],
    ) -> str:
        if not replacements:
            return code

        def repl(match: re.Match[str]) -> str:
            quote = match.group(1)
            raw = match.group(2)
            decoded = cls._decode_js_string_fragment(raw)
            replaced = decoded
            for old, new in replacements.items():
                replaced = replaced.replace(old, new)
            if replaced == decoded:
                return match.group(0)
            return f"{quote}{cls._encode_js_string_fragment(replaced, quote)}{quote}"

        return re.sub(r"([\"'`])((?:\\.|(?!\1).)*?)\1", repl, code, flags=re.DOTALL)

    @staticmethod
    def _encode_js_string_fragment(text: str, quote: str) -> str:
        text = text.replace("\\", "\\\\")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\n", r"\n").replace("\t", r"\t")
        if quote == '"':
            text = text.replace('"', r"\"")
        elif quote == "'":
            text = text.replace("'", r"\'")
        elif quote == "`":
            text = text.replace("`", r"\`")
        return text

    @classmethod
    def _layout_qa_retry_guidance(cls, err_msg: str) -> list[str]:
        if not cls._layout_qa_enabled() or not cls._is_layout_qa_validation_error(err_msg):
            return []
        guidance = [
            "Layout QA 返修：保留页面内容和表达意图，优先重排版式，不要靠缩小字号、单倍行距、裁切或元素重叠解决。",
            "所有正文/说明/流程节点/表格单元格最低 14pt，并使用 1.25 倍行距；文本框和外层卡片必须同步扩大。",
        ]
        if "slide.addTable" in err_msg or "方框问号" in err_msg:
            guidance.append(
                "中文表格返修：不要使用 slide.addTable 承载中文正文；用 addShape 画单元格，用 addText 写中文，并显式设置中文字体、14-16pt 和 1.25 行距。"
            )
        if "外层形状" in err_msg or "卡片/节点" in err_msg or "间距" in err_msg:
            guidance.append(
                "卡片/节点返修：扩大文字框时同步扩大背景形状，并重新计算卡片间距；多个带说明节点优先改成表格、分栏或上下分区。"
            )
        if "文本框相互重叠" in err_msg or "遮挡" in err_msg or "重叠" in err_msg:
            guidance.append(
                "重叠返修：重新分配标题、标签、正文、页脚位置；长标题分两行或下移内容区，正文块互相压住时改成表格/分栏/上下分区。"
            )
        if "连接线" in err_msg or "斜向" in err_msg:
            guidance.append(
                "连线返修：避免长斜线和穿字线；箭头从节点边缘出发，必要时改成水平/垂直分段线或表格。"
            )
        return guidance

    @classmethod
    def _book_readability_retry_guidance(cls, err_msg: str, attempt: int) -> list[str]:
        if not cls._book_readability_qa_enabled() or "电子书可读性校验失败" not in str(err_msg or ""):
            return []
        guidance = [
            "电子书可读性返修：不要只把原元素微调几像素；请先保留关键内容，改用更稳的表格、分栏、上下分区或拆页承接来重新排版。",
            "正文/说明/流程节点可用 14-16pt + 1.25 行距；卡片/节点标题 18pt；分区标题 20pt；使用 14pt 时必须给足文字框和外层卡片高度。",
            "如果文本框或外层形状空间不足，优先扩大容器、改用表格/分栏/上下分区或由下一页承接；只能压缩重复表述，不能删除教材关键点。",
        ]
        if "外层形状" in err_msg or "卡片/节点" in err_msg or "间距" in err_msg:
            guidance.append(
                "卡片/节点返修：如果是两个以上卡片碰撞或间距不足，不要继续微调卡片坐标；主体内容改成表格式网格（addShape 单元格 + addText 文字）、两列分组或上下分区。扩大文字框时必须同步扩大背景卡片/圆形/节点，并重新计算卡片间距。"
            )
        if any(token in err_msg for token in ("核心结论", "记忆要点", "注意：", "选择依据")):
            guidance.append(
                "总结语返修：不要把“核心结论/记忆要点/注意/选择依据”做成底部大条或相邻小卡片；把它并入同一表格/分区的末行、右侧等高栏或独立短注，并给主体内容保留完整高度。"
            )
        if "装饰形状" in err_msg:
            guidance.append(
                "装饰返修：不要让圆形章节号、编号徽章、可选参考标签和标题/正文共用同一坐标区；把装饰移到独立留白区，或改成左侧窄色条和独立标签行。"
            )
        if "数量过多" in err_msg:
            guidance.append(
                "卡片墙返修：不要继续生成多个小卡片；保留所有关键知识点，改为 2-3 列表格、分栏清单或上下分区。"
            )
        if "slide.addTable" in err_msg or "方框问号" in err_msg:
            guidance.append(
                "中文表格返修：不要再使用 slide.addTable；用 slide.addShape 画表格式单元格背景/边框，用 slide.addText 写入每个单元格中文，并显式设置中文字体、14-16pt 字号和 1.25 行距。"
            )
        if "文本框相互重叠" in err_msg or "标题" in err_msg or "重叠" in err_msg:
            guidance.append(
                "重叠返修：把章节圆点/可选参考标签/页码徽章移出标题区；长标题分两行或下移内容区；如果正文块互相压住，主体内容必须改成表格式网格（addShape 单元格 + addText 文字）、两列分组或上下分区，不允许继续堆叠小卡片。"
            )
        if re.search(r"[=＋+\-−×÷→←↔]|\\bvs\\b", str(err_msg or ""), flags=re.IGNORECASE):
            guidance.append(
                "公式/对比关系返修：把公式、组间对比或箭头关系放进独立的全宽公式区/对照区，下面再用形状网格承载解释文字；不要把公式、说明、记忆点分别做成相邻小卡片，也不要让公式和说明共用同一行。"
            )
        if "→" in err_msg or "箭头" in err_msg:
            guidance.append(
                "长箭头链返修：不要把 3 步以上流程写成一条长箭头句塞进卡片；改成纵向步骤表、编号行或上下分段，每个步骤独占一行并预留 14-16pt + 1.25 行距。"
            )
        if "连接线" in err_msg or "斜向" in err_msg:
            guidance.append(
                "连线返修：放弃长斜线、分叉箭头和穿字线；如果节点带说明文字，直接改成表格或上下分区，不要继续生成连接卡片图。"
            )
        if attempt >= 2:
            guidance.append(
                "这是多次返修后的页面：必须放弃当前拥挤版式，改成稳定布局。保留教材关键点，删除侧边竖牌、装饰圆、连接线、漂浮标签、底部记忆条和底部结论条；只能使用一个全宽形状网格、左右两列等高分区或上中下三段式。总结语放入同一网格末行或等高分区内；每个区域之间至少留 0.18 英寸空隙，不能继续生成多个相邻小卡片。"
            )
        return guidance

    @classmethod
    def _book_strict_retry_guidance(cls, err_msg: str) -> list[str]:
        if not cls._strict_book_ppt_qa_enabled():
            return []
        if "页面可见索引/切片表达式" in err_msg:
            return [
                "电子书严格校验：删除没有出现在教材依据中的具体表达式和答案；练习页可改成“表达式：____ / 结果：____ / 依据：____”，不要自造变量、URL、切片边界或嵌套索引。"
            ]
        return []

    @classmethod
    def _book_provider_safety_retry_guidance(cls, err_msg: str) -> list[str]:
        if not cls._book_ppt_qa_enabled() or not cls._is_provider_safety_filter_error(err_msg):
            return []
        return [
            "电子书生成遇到模型内容安全误判：这是大学课程课件，请保留教材知识点，但把案例和图示改成中性、学术、课堂化表述。避免真实个人身份、隐私细节、责任归因、事故伤害、监控跟踪、攻击性或高风险场景；用“事件前后条件、观察单位、操作角色、记录项、样本间隔”等研究方法语言重写页面。"
        ]

    @classmethod
    def _book_provider_transient_retry_guidance(cls, err_msg: str) -> list[str]:
        if not cls._book_ppt_qa_enabled() or not cls._is_provider_transient_error(err_msg):
            return []
        return [
            "电子书生成遇到模型连接中断或传输不完整：不要改变本页教学内容和版式策略，只重新输出完整、可执行的单页 JavaScript 代码块。"
        ]

    @classmethod
    def _book_readability_qa_enabled(cls) -> bool:
        raw = os.getenv("DIRECTIONAI_BOOK_PPT_READABILITY_QA", "").strip().lower()
        return raw in {"1", "true", "yes", "on"} and cls._book_ppt_qa_enabled()

    @classmethod
    def _visible_code_identifier_terms(cls, text: str) -> set[str]:
        terms: set[str] = set()
        for quoted in re.findall(r"`([^`]{1,80})`", text):
            terms.update(re.findall(r"\b[a-z_][a-z0-9_]{1,}\b", quoted))
        for line in text.splitlines():
            lowered = line.lower()
            if not any(marker in lowered for marker in ("方法", "函数", "api", "调用", "支持", "不支持", "无", "类型", "模块", "编码", "转换")):
                continue
            if not re.search(r"[()/._]|(?:\s[/|、]\s)", line):
                continue
            terms.update(re.findall(r"\b[a-z_][a-z0-9_]{1,}\b", lowered))
        return terms

    @staticmethod
    def _book_code_term_stopwords() -> set[str]:
        return {
            "api",
            "http",
            "https",
            "python",
            "shell",
            "true",
            "false",
            "none",
            "null",
            "vs",
            "and",
            "or",
            "if",
            "else",
            "for",
            "while",
            "return",
            "object",
            "objects",
            "class",
            "type",
            "types",
            "module",
            "func",
            "function",
            "page",
            "raw",
        }

    @classmethod
    def _book_slide_source_pages(cls, slide: SlideOutline, content_requirements: str) -> set[int]:
        section = cls._book_slide_requirement_section(slide, content_requirements)
        if not section:
            return set()
        match = re.search(r"-\s*教材页码[:：]\s*([^\n]+)", section)
        pages = cls._parse_page_list(match.group(1)) if match else set()
        pages.update(cls._parse_prefixed_page_refs(section))
        return pages

    @staticmethod
    def _book_slide_requirement_section(slide: SlideOutline, content_requirements: str) -> str:
        if not content_requirements:
            return ""
        title = (slide.topic or "").strip()
        if not title:
            return ""
        sections = re.split(r"\n###\s*第\s*(\d+)\s*页[:：]", "\n" + content_requirements)
        candidates: list[tuple[int, str]] = []
        visible_slide_no = int(getattr(slide, "slide_index", -1)) + 1
        for index in range(1, len(sections), 2):
            page_no_text = sections[index]
            section = sections[index + 1] if index + 1 < len(sections) else ""
            lines = section.splitlines()
            if not lines:
                continue
            if page_no_text.isdigit() and int(page_no_text) == visible_slide_no:
                candidates.append((20000, section))
            section_title = lines[0].strip()
            if section_title == title:
                candidates.append((10000 + len(section_title), section))
            elif title in section_title:
                candidates.append((8000 + len(section_title), section))
            elif section_title in title:
                candidates.append((5000 + len(section_title), section))
        if not candidates:
            return ""
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    @staticmethod
    def _parse_page_list(text: str) -> set[int]:
        pages: set[int] = set()
        for start, end in re.findall(r"(\d+)\s*[-–—]\s*(\d+)", text or ""):
            left = int(start)
            right = int(end)
            if left <= right and right - left <= 200:
                pages.update(range(left, right + 1))
        for value in re.findall(r"\d+", text or ""):
            pages.add(int(value))
        return pages

    @classmethod
    def _parse_prefixed_page_refs(cls, text: str) -> set[int]:
        pages: set[int] = set()
        for start, end in re.findall(r"\bp\.\s*(\d+)\s*[-–—]\s*(\d+)", text or "", flags=re.IGNORECASE):
            left = int(start)
            right = int(end)
            if left <= right and right - left <= 200:
                pages.update(range(left, right + 1))
        for value in re.findall(r"\bp\.\s*(\d+)\b", text or "", flags=re.IGNORECASE):
            pages.add(int(value))
        return pages

    @staticmethod
    def _compact_page_set(pages: set[int]) -> str:
        ordered = sorted(pages)
        if not ordered:
            return "蓝图教材页码"
        if len(ordered) <= 8:
            return ", ".join(f"p.{page}" for page in ordered)
        return f"p.{ordered[0]}-p.{ordered[-1]}"

    @staticmethod
    def _book_ppt_qa_profile() -> str:
        profile = os.getenv("DIRECTIONAI_BOOK_PPT_QA_PROFILE", "").strip().lower()
        if profile in {"balanced", "strict", "off"}:
            return profile
        if os.getenv("DIRECTIONAI_BOOK_PPT_STRICT_QA", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            return "strict"
        return "off"

    @classmethod
    def _book_ppt_qa_enabled(cls) -> bool:
        return cls._book_ppt_qa_profile() in {"balanced", "strict"}

    @classmethod
    def _strict_book_ppt_qa_enabled(cls) -> bool:
        return cls._book_ppt_qa_profile() == "strict"

    @staticmethod
    def _require_image_asset_usage() -> bool:
        return os.getenv("DIRECTIONAI_REQUIRE_IMAGE_ASSET_USAGE", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
            "warn",
            "strict",
        }

    @staticmethod
    def _strict_image_asset_usage_required() -> bool:
        return os.getenv("DIRECTIONAI_REQUIRE_IMAGE_ASSET_USAGE", "").strip().lower() in {
            "strict",
            "fail",
            "required",
        }

    @staticmethod
    def _is_required_image_asset_usage_error(error: str) -> bool:
        return "已提供本地图片资产" in (error or "") and "必须使用 addImage" in (error or "")

    @staticmethod
    def _decode_unicode_escape_literals(text: str) -> str:
        return re.sub(
            r"\\u([0-9a-fA-F]{4})",
            lambda match: chr(int(match.group(1), 16)),
            text,
        )

    @classmethod
    def _expected_chapter_labels(cls, chapter_number: str) -> set[str]:
        labels = {f"第{chapter_number}章"}
        try:
            number = int(chapter_number)
        except ValueError:
            return labels
        chinese = cls._int_to_chinese_number(number)
        if chinese:
            labels.add(f"第{chinese}章")
        return labels

    @staticmethod
    def _int_to_chinese_number(number: int) -> str:
        digits = "零一二三四五六七八九"
        if 0 < number < 10:
            return digits[number]
        if number == 10:
            return "十"
        if 10 < number < 20:
            return f"十{digits[number % 10]}"
        if 20 <= number < 100:
            tens = number // 10
            ones = number % 10
            return f"{digits[tens]}十{digits[ones] if ones else ''}"
        return str(number)

    @staticmethod
    def _validate_addtext_rich_text_arrays(code: str) -> None:
        for match in re.finditer(r"\.addText\s*\(\s*\[", code):
            bracket_index = code.find("[", match.start())
            content = PlannerAgent._extract_balanced_bracket_content(code, bracket_index)
            if content is None:
                continue
            if PlannerAgent._top_level_array_contains_string_item(content):
                raise ValueError(
                    "检测到 addText 富文本数组包含裸字符串项。"
                    "PptxGenJS 的 rich text 数组每一项都必须是对象，例如 "
                    "`{ text: \"1\", options: { color: \"FFFFFF\" } }`；"
                    "不要写 `slide.addText([\"1\", {...}], options)`。"
                )

    @staticmethod
    def _extract_balanced_bracket_content(text: str, bracket_index: int) -> str | None:
        if bracket_index < 0 or bracket_index >= len(text) or text[bracket_index] != "[":
            return None
        depth = 0
        in_string = False
        quote = ""
        escape = False
        for index in range(bracket_index, len(text)):
            ch = text[index]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == quote:
                    in_string = False
                continue
            if ch in "\"'`":
                in_string = True
                quote = ch
                continue
            if ch == "[":
                depth += 1
                continue
            if ch == "]":
                depth -= 1
                if depth == 0:
                    return text[bracket_index + 1 : index]
        return None

    @staticmethod
    def _top_level_array_contains_string_item(content: str) -> bool:
        brace_depth = 0
        bracket_depth = 0
        paren_depth = 0
        in_string = False
        quote = ""
        escape = False
        at_item_start = True
        for ch in content:
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == quote:
                    in_string = False
                continue

            if ch.isspace():
                continue
            if ch in "\"'`":
                if brace_depth == 0 and bracket_depth == 0 and paren_depth == 0 and at_item_start:
                    return True
                in_string = True
                quote = ch
                at_item_start = False
                continue
            if ch == "{":
                brace_depth += 1
                at_item_start = False
                continue
            if ch == "}":
                brace_depth = max(0, brace_depth - 1)
                continue
            if ch == "[":
                bracket_depth += 1
                at_item_start = False
                continue
            if ch == "]":
                bracket_depth = max(0, bracket_depth - 1)
                continue
            if ch == "(":
                paren_depth += 1
                at_item_start = False
                continue
            if ch == ")":
                paren_depth = max(0, paren_depth - 1)
                continue
            if ch == "," and brace_depth == 0 and bracket_depth == 0 and paren_depth == 0:
                at_item_start = True
                continue
            if brace_depth == 0 and bracket_depth == 0 and paren_depth == 0:
                at_item_start = False
        return False

    def _sanitize_generated_code(
        self,
        code: str,
        *,
        apply_book_readability_normalization: bool = True,
    ) -> str:
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
        code = re.sub(
            r"\bslide\.addBackground\s*\(\s*(\{[^;]*?\})\s*\)\s*;?",
            r"slide.background = \1;",
            code,
            flags=re.DOTALL,
        )
        code = re.sub(
            r"(?<![\w$])(const|let|var)(?=[$_\w\u4e00-\u9fff])([$_\w\u4e00-\u9fff]+)\s*=",
            r"\1 \2 =",
            code,
        )
        code = re.sub(
            r"\b(const|let|var)\s+(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=",
            r"\1 \2 =",
            code,
        )
        code = self._repair_undeclared_foreach_receivers(code)
        code = self._strip_stray_js_bareword_lines(code)
        code = self._escape_problematic_js_string_quotes(code)
        if self._layout_qa_enabled() and apply_book_readability_normalization:
            code = self._decode_html_entities_in_visible_addtext_strings(code)
            code = self._normalize_book_readability_table_geometry(code)
            code = self._repair_book_readability_undefined_geometry_expressions(code)
            code = self._normalize_book_readability_dynamic_geometry_constraints(code)
        code = self._normalize_geometry_constraints(code)
        if self._layout_qa_enabled() and apply_book_readability_normalization:
            code = self._normalize_book_readability_styles(code)
            return self._normalize_book_readability_geometry(code)
        return code

    @staticmethod
    def _repair_undeclared_foreach_receivers(code: str) -> str:
        declared = {
            match.group(1)
            for match in re.finditer(r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=", code)
        }
        if not declared:
            return code
        declared_lower = {name.lower(): name for name in declared}

        def replace_receiver(match: re.Match) -> str:
            receiver = match.group(1)
            suffix = match.group(2)
            if receiver in declared:
                return match.group(0)
            replacement = ""
            if receiver.startswith("var") and len(receiver) > 3:
                candidate = receiver[3:]
                if candidate in declared:
                    replacement = candidate
            if not replacement:
                replacement = declared_lower.get(receiver.lower(), "")
            if not replacement:
                receiver_lower = receiver.lower()
                suffix_matches = [
                    name
                    for name in declared
                    if len(name) >= 4
                    and receiver_lower.endswith(name.lower())
                    and len(receiver) - len(name) <= max(6, len(receiver) // 2)
                ]
                if len(suffix_matches) == 1:
                    replacement = suffix_matches[0]
            if not replacement:
                return match.group(0)
            return replacement + suffix

        repaired = re.sub(r"\b([A-Za-z_$][A-Za-z0-9_$]*)(\s*\.forEach\s*\()", replace_receiver, code)
        for name in declared:
            if name.startswith("var") or f"var{name}" in declared:
                continue
            repaired = re.sub(rf"\bvar{re.escape(name)}\b", name, repaired)
        return repaired

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

        if code[j] == "=":
            # A single equals sign after a quote is much more often prose like
            # `"what" = ...` accidentally placed inside addText text than a
            # valid JavaScript string boundary. Comparisons (`==`, `===`) still
            # count as a real boundary.
            return j + 1 < len(code) and code[j + 1] == "="

        return code[j] in ",:;)}]+-*/%?&|<>"

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
