from __future__ import annotations

import json
import re
from functools import cached_property

from backend.harness.runtime import SkillRuntime
from backend.models.schemas import (
    EvidenceMode,
    LayoutRegion,
    PageIntent,
    SlideLayout,
    SlideLayoutIntent,
    SlideOutline,
    VisualMode,
    resolve_visual_mode,
)


class LayoutPlanner:
    """Build a deterministic layout intent before asking the model for JS."""

    DEFAULT_COMPARISON_HINTS = (
        "对比", "比较", "差异", "区别", "优劣", "优点", "缺点", "优势", "挑战", "vs", "versus",
    )
    DEFAULT_TIMELINE_HINTS = (
        "流程", "步骤", "阶段", "演进", "发展", "历程", "路线", "路径", "过程", "生命周期", "里程碑",
    )
    DEFAULT_GRID_HINTS = (
        "应用", "场景", "模块", "能力", "特征", "原则", "策略", "维度", "挑战", "问题", "方向",
    )
    DEFAULT_CONCEPT_HEAVY_HINTS = (
        "原理", "机制", "算法", "推导", "目标", "约束", "优化", "损失", "训练", "对齐", "框架", "闭环",
    )

    def __init__(self):
        self._skill_runtime = SkillRuntime()

    @staticmethod
    def _region(name: str, x: float, y: float, width: float, height: float, purpose: str) -> LayoutRegion:
        return LayoutRegion(
            name=name,
            x=x,
            y=y,
            width=width,
            height=height,
            purpose=purpose,
        )

    @staticmethod
    def _contains_any(text: str, hints: tuple[str, ...]) -> bool:
        lowered = text.lower()
        return any(hint.lower() in lowered for hint in hints)

    @staticmethod
    def _topic_blob(slide: SlideOutline, research: dict | None) -> str:
        parts = [slide.topic or "", slide.objective or ""]
        if research:
            parts.extend(str(item) for item in (research.get("bullet_points") or [])[:5])
            parts.extend(str(item) for item in (research.get("key_data") or [])[:3])
        return " ".join(parts)

    @cached_property
    def _intent_rules(self) -> dict:
        try:
            raw = self._skill_runtime.load_reference(
                "visual-production",
                "page_intent_classifier.json",
            )
            data = json.loads(raw)
        except Exception:
            data = {}
        return data if isinstance(data, dict) else {}

    def _hint_list(self, key: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
        hints = self._intent_rules.get(key)
        if isinstance(hints, list) and hints:
            return tuple(str(item) for item in hints)
        return fallback

    def _infer_page_intent(
        self,
        *,
        slide: SlideOutline,
        key_data: list[str],
        comparison_like: bool,
        timeline_like: bool,
        grid_like: bool,
        concept_heavy: bool,
        visual_mode: VisualMode,
        bullets: list[str],
    ) -> PageIntent:
        if slide.layout == SlideLayout.COVER:
            return PageIntent.COVER
        if slide.layout == SlideLayout.TOC:
            return PageIntent.NAVIGATION
        if slide.layout == SlideLayout.CLOSING:
            return PageIntent.SYNTHESIZE
        if comparison_like or slide.layout == SlideLayout.TWO_COLUMN:
            return PageIntent.COMPARE_OPTIONS
        if timeline_like:
            return PageIntent.SHOW_PROCESS
        if key_data:
            return PageIntent.PRESENT_EVIDENCE
        if visual_mode == VisualMode.JS_DIAGRAM:
            return PageIntent.SHOW_STRUCTURE if not concept_heavy else PageIntent.EXPLAIN_MECHANISM
        if grid_like or len(bullets) >= 4:
            return PageIntent.GROUP_INSIGHTS
        if concept_heavy:
            return PageIntent.EXPLAIN_MECHANISM
        if visual_mode == VisualMode.GENERATED_IMAGE:
            return PageIntent.CASE_STUDY
        return PageIntent.EXPLAIN_CONCEPT

    @staticmethod
    def _infer_evidence_mode(
        *,
        page_intent: PageIntent,
        visual_mode: VisualMode,
        has_image: bool,
        key_data: list[str],
        bullets: list[str],
    ) -> EvidenceMode:
        if page_intent in {PageIntent.COVER, PageIntent.NAVIGATION, PageIntent.SYNTHESIZE}:
            return EvidenceMode.HEADLINE
        if page_intent == PageIntent.PRESENT_EVIDENCE or key_data:
            return EvidenceMode.METRIC
        if page_intent == PageIntent.COMPARE_OPTIONS:
            return EvidenceMode.COMPARISON
        if page_intent == PageIntent.SHOW_PROCESS:
            return EvidenceMode.TIMELINE
        if page_intent in {PageIntent.SHOW_STRUCTURE, PageIntent.EXPLAIN_MECHANISM} or visual_mode == VisualMode.JS_DIAGRAM:
            return EvidenceMode.DIAGRAM
        if page_intent == PageIntent.GROUP_INSIGHTS:
            return EvidenceMode.GRID
        if page_intent == PageIntent.CASE_STUDY and has_image and visual_mode == VisualMode.GENERATED_IMAGE:
            return EvidenceMode.IMAGE
        if bullets:
            return EvidenceMode.BULLETS
        return EvidenceMode.MIXED

    def plan_layout_intent(
        self,
        slide: SlideOutline,
        *,
        research: dict | None = None,
        image_path: str | None = None,
    ) -> SlideLayoutIntent:
        visual_mode = resolve_visual_mode(slide)
        bullets = (research or {}).get("bullet_points") or []
        key_data = (research or {}).get("key_data") or []
        has_image = bool(image_path)
        blob = self._topic_blob(slide, research)
        density = "high" if len(bullets) >= 5 or len(blob) >= 90 else "medium" if len(bullets) >= 3 else "low"
        comparison_like = self._contains_any(
            blob, self._hint_list("comparison_hints", self.DEFAULT_COMPARISON_HINTS)
        )
        timeline_like = self._contains_any(
            blob, self._hint_list("timeline_hints", self.DEFAULT_TIMELINE_HINTS)
        )
        grid_like = self._contains_any(
            blob, self._hint_list("grid_hints", self.DEFAULT_GRID_HINTS)
        )
        concept_heavy = self._contains_any(
            blob, self._hint_list("concept_heavy_hints", self.DEFAULT_CONCEPT_HEAVY_HINTS)
        )
        page_intent = self._infer_page_intent(
            slide=slide,
            key_data=key_data,
            comparison_like=comparison_like,
            timeline_like=timeline_like,
            grid_like=grid_like,
            concept_heavy=concept_heavy,
            visual_mode=visual_mode,
            bullets=bullets,
        )
        evidence_mode = self._infer_evidence_mode(
            page_intent=page_intent,
            visual_mode=visual_mode,
            has_image=has_image,
            key_data=key_data,
            bullets=bullets,
        )

        if slide.layout == SlideLayout.COVER:
            return SlideLayoutIntent(
                slide_index=slide.slide_index,
                archetype="cover-hero",
                page_intent=page_intent,
                evidence_mode=evidence_mode,
                title_region=LayoutRegion(
                    name="title",
                    x=0.8,
                    y=1.0,
                    width=11.6,
                    height=1.5,
                    purpose="封面主标题",
                ),
                body_region=LayoutRegion(
                    name="subtitle",
                    x=0.9,
                    y=2.8,
                    width=8.2,
                    height=1.2,
                    purpose="副标题或引导语",
                ),
                text_density="low",
                required_anchors=["full-bleed background", "strong title hierarchy"],
                forbidden_regions=["center pileup", "tiny title block"],
                rationale="封面需要大标题、足量留白和稳定主视觉。",
                fallback_archetypes=["cover-minimal"],
            )

        if slide.layout == SlideLayout.TOC:
            return SlideLayoutIntent(
                slide_index=slide.slide_index,
                archetype="toc-list",
                page_intent=page_intent,
                evidence_mode=evidence_mode,
                title_region=self._region("title", 0.8, 0.6, 11.0, 0.9, "目录标题"),
                body_region=self._region("body", 0.9, 1.6, 7.1, 4.8, "目录内容"),
                emphasis_region=self._region("accent", 8.4, 1.7, 3.5, 4.4, "目录侧边视觉平衡区"),
                text_density="medium",
                required_anchors=["stable title x", "list rhythm"],
                forbidden_regions=["title overlap", "bullet crowding"],
                rationale="目录页强调清晰列表节奏和视觉平衡。",
                fallback_archetypes=["toc-two-column"],
            )

        if slide.layout == SlideLayout.CLOSING:
            return SlideLayoutIntent(
                slide_index=slide.slide_index,
                archetype="closing-statement",
                page_intent=page_intent,
                evidence_mode=evidence_mode,
                title_region=self._region("title", 1.0, 1.4, 10.6, 1.2, "总结标题"),
                body_region=self._region("body", 1.2, 3.0, 8.5, 1.3, "结束语"),
                text_density="low",
                required_anchors=["paired closing motif", "confident center alignment"],
                forbidden_regions=["dense bullets", "small summary card"],
                rationale="结束页需要和封面呼应，但正文密度要低。",
                fallback_archetypes=["closing-minimal"],
            )

        if key_data:
            return SlideLayoutIntent(
                slide_index=slide.slide_index,
                archetype="stat-callout",
                page_intent=page_intent,
                evidence_mode=evidence_mode,
                title_region=self._region("title", 0.8, 0.5, 11.2, 0.85, "标题区"),
                body_region=self._region("body", 0.9, 4.15, 5.9, 2.15, "解释文本"),
                emphasis_region=self._region("emphasis", 0.9, 1.7, 4.5, 1.95, "关键数据大字区"),
                visual_region=self._region("visual", 5.95, 1.5, 5.85, 4.95, "图表或辅助结构区"),
                text_density=density,
                required_anchors=["title-first", "big-number anchor", "supporting explainer near stat", "balanced right-side structure fill"],
                forbidden_regions=["small stat", "bottom-heavy layout", "oversized stat card with one short line", "blank white panel beside stat"],
                rationale="存在关键数据时应突出统计锚点，但必须配套解释与辅助结构，避免出现大面积空白卡片。",
                fallback_archetypes=["two-column-balanced", "single-column-card"],
            )

        if comparison_like:
            return SlideLayoutIntent(
                slide_index=slide.slide_index,
                archetype="comparison-split",
                page_intent=page_intent,
                evidence_mode=evidence_mode,
                title_region=self._region("title", 0.8, 0.45, 11.2, 0.85, "标题区"),
                body_region=self._region("body", 0.8, 1.65, 5.3, 4.8, "左侧观点或方案 A"),
                visual_region=self._region("visual", 6.5, 1.65, 5.1, 4.8, "右侧观点或方案 B"),
                emphasis_region=self._region("emphasis", 5.8, 1.8, 0.35, 4.5, "中部对比分隔/胜负结论"),
                text_density=density,
                required_anchors=["clear versus framing", "two strong comparison blocks", "single decision takeaway", "balanced column weight", "aligned card top and bottom edges"],
                forbidden_regions=["duplicated columns", "weak comparison labels", "one dense column plus one empty panel", "floating arrows without aligned anchors"],
                rationale="对比类页面应做成结构明确的双侧对照，两侧信息量和视觉重量都要接近，不能一边拥挤一边空洞。",
                fallback_archetypes=["two-column-balanced", "card-grid-insight"],
            )

        if timeline_like:
            return SlideLayoutIntent(
                slide_index=slide.slide_index,
                archetype="timeline-flow",
                page_intent=page_intent,
                evidence_mode=evidence_mode,
                title_region=self._region("title", 0.8, 0.45, 11.2, 0.85, "标题区"),
                body_region=self._region("body", 0.9, 5.25, 11.0, 1.15, "流程总结/关键说明"),
                visual_region=self._region("visual", 0.9, 1.55, 11.2, 3.35, "阶段流程主图区"),
                text_density=density,
                required_anchors=["clear stage sequence", "visible connectors", "numbered progression"],
                forbidden_regions=["tiny timeline labels", "unordered stages"],
                rationale="流程与演进类内容需要明确阶段顺序和连接关系。",
                fallback_archetypes=["diagram-focused", "single-column-card"],
            )

        if (
            has_image
            and visual_mode == VisualMode.GENERATED_IMAGE
            and density != "high"
            and len(bullets) <= 3
            and not comparison_like
            and not timeline_like
            and not concept_heavy
        ):
            return SlideLayoutIntent(
                slide_index=slide.slide_index,
                archetype="visual-hero-split",
                page_intent=page_intent,
                evidence_mode=evidence_mode,
                title_region=self._region("title", 0.8, 0.45, 11.2, 0.85, "标题区"),
                body_region=self._region("body", 0.9, 1.75, 4.3, 4.75, "引导文字与关键要点"),
                visual_region=self._region("visual", 5.55, 1.45, 6.35, 5.25, "主视觉图片区"),
                emphasis_region=self._region("emphasis", 0.95, 5.55, 3.9, 0.75, "一句结论或标签区"),
                text_density=density,
                required_anchors=["hero image dominance", "compact text column", "caption-like takeaway", "image supports topic rather than replacing content"],
                forbidden_regions=["equal-weight image and text", "image squeezed into thumbnail", "concept-heavy page dominated by stock photo", "black-box hero image against light academic layout"],
                rationale="只有在内容密度较低、以氛围和直观感知为主时，才应让主视觉图片占主导，避免用大图掩盖学术内容。",
                fallback_archetypes=["two-column-balanced", "editorial-highlight"],
            )

        if len(bullets) >= 4 or grid_like:
            return SlideLayoutIntent(
                slide_index=slide.slide_index,
                archetype="card-grid-insight",
                page_intent=page_intent,
                evidence_mode=evidence_mode,
                title_region=self._region("title", 0.8, 0.45, 11.2, 0.85, "标题区"),
                body_region=self._region("body", 0.9, 1.55, 11.0, 4.95, "2x2 或 2x3 信息卡片区"),
                emphasis_region=self._region("emphasis", 9.2, 0.55, 2.2, 0.6, "右上角标签/摘要"),
                text_density=density,
                required_anchors=["even card rhythm", "one-line card titles", "distinct card hierarchy"],
                forbidden_regions=["text wall inside cards", "uneven card spacing"],
                rationale="多要点、模块或场景类内容更适合卡片矩阵，而不是一整块文字。",
                fallback_archetypes=["icon-row-insight", "single-column-card"],
            )

        if visual_mode == VisualMode.JS_DIAGRAM:
            return SlideLayoutIntent(
                slide_index=slide.slide_index,
                archetype="diagram-focused",
                page_intent=page_intent,
                evidence_mode=evidence_mode,
                title_region=self._region("title", 0.8, 0.45, 11.2, 0.85, "标题区"),
                body_region=self._region("body", 0.9, 1.55, 3.6, 4.9, "引导文本"),
                visual_region=self._region("visual", 4.9, 1.45, 7.4, 5.2, "结构化示意图区"),
                text_density=density,
                required_anchors=["diagram dominance", "clear title spacing", "aligned node grid", "visual region filled by real structure"],
                forbidden_regions=["fake image", "diagram squeezing text", "large empty panel with only one label", "misaligned nodes and connectors"],
                rationale="结构化关系应由示意图区承载，而且节点、箭头和说明必须对齐成网格，避免出现大片空白和松散连线。",
                fallback_archetypes=["two-column-balanced", "single-column-card"],
            )

        if density == "low":
            return SlideLayoutIntent(
                slide_index=slide.slide_index,
                archetype="editorial-highlight",
                page_intent=page_intent,
                evidence_mode=evidence_mode,
                title_region=self._region("title", 0.9, 0.6, 10.8, 0.95, "标题区"),
                body_region=self._region("body", 1.0, 2.0, 7.0, 2.9, "核心论点与解释区"),
                emphasis_region=self._region("emphasis", 8.4, 1.9, 3.2, 3.2, "引述/标签/重点句区"),
                text_density=density,
                required_anchors=["single strong statement", "clean negative space", "editorial focal point"],
                forbidden_regions=["overfilled body", "tiny decorative fragments"],
                rationale="低密度内容更适合做成编辑感页面，用一句强结论建立高级感。",
                fallback_archetypes=["single-column-card", "two-column-balanced"],
            )

        if has_image or slide.layout == SlideLayout.TWO_COLUMN:
            return SlideLayoutIntent(
                slide_index=slide.slide_index,
                archetype="two-column-balanced",
                page_intent=page_intent,
                evidence_mode=evidence_mode,
                title_region=self._region("title", 0.8, 0.45, 11.2, 0.85, "标题区"),
                body_region=self._region("body", 0.85, 1.6, 5.5, 4.9, "正文区"),
                visual_region=self._region("visual", 6.7, 1.45, 5.6, 5.1, "主视觉区"),
                text_density=density,
                required_anchors=["stable title x", "clear left-right hierarchy", "comparable visual weight across columns"],
                forbidden_regions=["narrow text box", "visual crowding title", "image dominates concept-heavy page", "one side visually empty"],
                rationale="有图或双栏内容时要保证左右两栏都承担明确任务，既不能让图片压过正文，也不能让一侧变成空白占位。",
                fallback_archetypes=["single-column-card", "stat-callout"],
            )

        return SlideLayoutIntent(
            slide_index=slide.slide_index,
            archetype="single-column-card",
            page_intent=page_intent,
            evidence_mode=evidence_mode,
            title_region=self._region("title", 0.8, 0.45, 11.2, 0.85, "标题区"),
            body_region=self._region("body", 0.95, 1.65, 10.9, 4.85, "正文主内容区"),
            emphasis_region=self._region("emphasis", 8.6, 1.75, 2.6, 1.6, "小型强调区"),
            text_density=density,
            required_anchors=["single reading flow", "card rhythm"],
            forbidden_regions=["text wall", "ornament collision"],
            rationale="默认正文页使用单栏卡片骨架，优先保稳定留白和阅读流。",
            fallback_archetypes=["two-column-balanced", "stat-callout"],
        )
