import json
import time
import os
import asyncio
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import cached_property
from typing import Any
from typing import Callable
from backend.harness.agents.asset import AssetAgent
from backend.harness.agents.planner import PlannerAgent
from backend.harness.agents.research import ResearchAgent
from backend.harness.agents.visual_eval import EvaluatorAgent
from backend.harness.runtime import (
    HarnessTrace,
    PromptComposer,
    PromptSection,
    RepairOrchestrator,
    SkillContext,
    HarnessRunState,
    merge_prompt_sections,
)
from backend.tools.pptx_skill import read_pptx, pptx_to_images
from backend.tools.openai_compat import build_chat_completion_kwargs, stream_chat_completion_text
import config
from backend.models.schemas import SlideEvalResult


@dataclass
class OrchestratorRunArtifacts:
    topic: str
    output_path: str
    outline: object
    slide_codes: list[str]
    theme: dict
    research_results: list[dict | None] | None = None
    image_paths: list[str | None] | None = None
    content_issues: list[dict] = field(default_factory=list)
    visual_eval_results: list[SlideEvalResult] = field(default_factory=list)
    preview_images: list[str] = field(default_factory=list)
    extracted_text: str = ""
    harness_trace: dict[str, Any] = field(default_factory=dict)
    phase_results: dict[str, Any] = field(default_factory=dict)


class OrchestratorAgent:
    """
    主控 Agent。
    逐页生成 PptxGenJS 代码 → 组装执行 → 视觉 QA 循环 → 产出 .pptx
    """

    def __init__(
        self,
        debug_layout: bool = False,
        no_research: bool = False,
        no_images: bool = False,
        image_source: str = "auto",
        model_provider: str = "minmax",
        thinking_callback: Callable[[str], None] | None = None,
        search_callback: Callable[[dict], None] | None = None,
        harness_trace: HarnessTrace | None = None,
    ):
        self.harness_trace = harness_trace or HarnessTrace(run_id=f"run_{int(time.time() * 1000)}")
        self.planner = PlannerAgent(
            model_provider=model_provider,
            thinking_callback=thinking_callback,
            harness_trace=self.harness_trace,
        )
        self.researcher = ResearchAgent(
            model_provider=model_provider,
            thinking_callback=thinking_callback,
            search_callback=search_callback,
            harness_trace=self.harness_trace,
        )
        self.asset_agent = AssetAgent(image_source=image_source, harness_trace=self.harness_trace)
        self.evaluator = EvaluatorAgent(harness_trace=self.harness_trace)
        self._composer = PromptComposer()
        self._skill_runtime = self._composer.runtime
        self._coherence_repair_orchestrator = RepairOrchestrator(
            self._skill_runtime,
            run_id=f"deck_{int(time.time() * 1000)}",
            phase="evaluation-and-repair",
        )
        self._deck_coherence_system_template = self._composer.load_deck_coherence_review_system_prompt()
        self._deck_coherence_user_template = self._composer.load_deck_coherence_review_user_prompt_template()
        self.debug_layout = debug_layout
        self.no_research = no_research
        self.no_images = no_images
        self._run_state = HarnessRunState(
            [
                "outline_planning",
                "research_and_assets",
                "slide_generation",
                "content_and_coherence_qa",
                "visual_qa",
                "finalize",
            ],
            trace=self.harness_trace,
        )

    @cached_property
    def _content_coherence_policy(self) -> dict[str, Any]:
        composer = getattr(self, "_composer", None) or PromptComposer()
        raw = composer.load_content_coherence_policy()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"无法解析 content_coherence_policy.json: {exc}") from exc
        return data if isinstance(data, dict) else {}

    def _content_qa_policy(self) -> dict[str, Any]:
        return self._content_coherence_policy.get("content_qa", {})

    def _deck_coherence_policy(self) -> dict[str, Any]:
        return self._content_coherence_policy.get("deck_coherence", {})

    def generate(
        self,
        topic: str,
        output_filename: str = "output.pptx",
        language: str = "中文",
        min_slides: int = 6,
        max_slides: int = 10,
        style: str = "",
        audience: str = "general",
        content_requirements: str = "",
    ) -> str:
        return self.generate_bundle(
            topic=topic,
            output_filename=output_filename,
            language=language,
            min_slides=min_slides,
            max_slides=max_slides,
            style=style,
            audience=audience,
            content_requirements=content_requirements,
        ).output_path

    def generate_bundle(
        self,
        topic: str,
        output_filename: str = "output.pptx",
        language: str = "中文",
        min_slides: int = 6,
        max_slides: int = 10,
        style: str = "",
        audience: str = "general",
        content_requirements: str = "",
    ) -> OrchestratorRunArtifacts:
        print(f"\n{'='*50}")
        print(f"开始生成 PPT：{topic}")
        print(f"{'='*50}\n")

        os.makedirs(config.OUTPUT_DIR, exist_ok=True)
        output_path = os.path.abspath(os.path.join(config.OUTPUT_DIR, output_filename))

        try:
            # Step 1: 规划大纲
            self._run_state.start("outline_planning")
            outline = self.planner.plan_outline(
                topic,
                min_slides=min_slides,
                max_slides=max_slides,
                style=style,
                audience=audience,
                language=language,
                content_requirements=content_requirements,
            )
            self._run_state.complete(
                "outline_planning",
                details={"slide_count": len(outline.slides), "title": outline.title},
            )

            # Step 2: Research → enrich image_prompt → 获取图片
            research_results = None
            image_paths = None
            self._run_state.start("research_and_assets")
            if not self.no_research and not self.no_images:
                outline, research_results, image_paths = self._research_and_assets(outline, language)
                self._run_state.complete(
                    "research_and_assets",
                    details={
                        "mode": "research_and_assets",
                        "researched_pages": sum(1 for item in research_results or [] if item),
                        "image_pages": sum(1 for item in image_paths or [] if item),
                    },
                )
            elif not self.no_research:
                research_results = self._research_outline(outline, language)
                self._run_state.complete(
                    "research_and_assets",
                    details={
                        "mode": "research_only",
                        "researched_pages": sum(1 for item in research_results or [] if item),
                        "image_pages": 0,
                    },
                )
            elif not self.no_images:
                image_paths = self._fetch_assets(outline, language)
                self._run_state.complete(
                    "research_and_assets",
                    details={
                        "mode": "image_only",
                        "researched_pages": 0,
                        "image_pages": sum(1 for item in image_paths or [] if item),
                    },
                )
            else:
                self._run_state.skip(
                    "research_and_assets",
                    reason="research_disabled_and_images_disabled",
                )

            # Step 3: 逐页生成 + 组装
            t0 = time.time()
            self._run_state.start("slide_generation")
            result_path, slide_codes, theme = self.planner.plan(
                topic,
                output_path=output_path,
                language=language,
                min_slides=min_slides,
                max_slides=max_slides,
                style=style,
                audience=audience,
                content_requirements=content_requirements,
                outline=outline,
                research_results=research_results,
                image_paths=image_paths,
            )
            print(f"[Orchestrator] 逐页生成耗时: {time.time() - t0:.1f}s")
            self._run_state.complete(
                "slide_generation",
                details={
                    "slide_code_count": len(slide_codes),
                    "theme_motif": theme.get("motif_description", ""),
                },
            )

            # Step 4: 内容结构 + deck coherence QA
            self._run_state.start("content_and_coherence_qa")
            content_issues = self._run_content_and_coherence_qa(result_path, outline)
            if content_issues:
                print(f"[Orchestrator] 文本 QA 发现 {len(content_issues)} 个问题，修复中...")
                result_path, slide_codes, theme = self._fix_content_issues(
                    content_issues, slide_codes, theme, outline, research_results, image_paths, output_path
                )
            self._run_state.complete(
                "content_and_coherence_qa",
                details={"issue_page_count": len(content_issues)},
            )

            # Step 5: 视觉 QA 循环
            if self.evaluator.enabled:
                self._run_state.start("visual_qa")
            result_path = self._qa_loop(
                result_path, slide_codes, theme, outline, research_results, image_paths
            )
            if self.evaluator.enabled:
                self._run_state.complete("visual_qa", details={"enabled": True})
            else:
                self._run_state.skip("visual_qa", reason="visual_evaluator_disabled")

            if self.debug_layout:
                self._print_debug(result_path)

            self._run_state.start("finalize")
            extracted_text = read_pptx(result_path)
            preview_images = pptx_to_images(result_path)
            visual_eval_results = []
            if self.evaluator.enabled and preview_images:
                visual_eval_results = self.evaluator.evaluate_all(preview_images, outline)
            self._run_state.complete(
                "finalize",
                details={
                    "preview_count": len(preview_images),
                    "visual_eval_count": len(visual_eval_results),
                    "output_path": result_path,
                },
            )

            print(f"\n{'='*50}")
            print(f"生成完成！文件路径：{result_path}")
            print(f"{'='*50}\n")

            return OrchestratorRunArtifacts(
                topic=topic,
                output_path=result_path,
                outline=outline,
                slide_codes=list(slide_codes),
                theme=dict(theme),
                research_results=list(research_results) if research_results else [],
                image_paths=list(image_paths) if image_paths else [],
                content_issues=list(content_issues),
                visual_eval_results=list(visual_eval_results),
                preview_images=list(preview_images),
                extracted_text=extracted_text,
                harness_trace=self.harness_trace.to_dict(),
                phase_results=self._run_state.export(),
            )

        except Exception as e:
            self._run_state.fail("finalize", error=str(e))
            print(f"\n[Orchestrator] 生成失败: {e}")
            raise

    def _qa_loop(
        self,
        output_path: str,
        slide_codes: list[str],
        theme: dict,
        outline,
        research_results,
        image_paths,
        on_revision_start: Callable[[dict[str, Any]], None] | None = None,
        on_revision_round_complete: Callable[[dict[str, Any]], None] | None = None,
        on_revision_failed: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        """
        视觉 QA 循环：转图片 → 评分 → 修复低分页 → 重新组装。
        按照 SKILL.md 要求，至少完成一轮完整的检查-修复-再验证循环。
        """
        if not self.evaluator.enabled:
            return output_path

        did_fix = False
        repaired_pages_total: set[int] = set()
        repaired_pages_last_round: set[int] = set()
        eval_results = []

        for round_i in range(1, config.EVAL_MAX_ROUNDS + 1):
            print(f"\n[Orchestrator] QA 第 {round_i} 轮：转换幻灯片为图片...")
            images = pptx_to_images(output_path)
            if not images:
                print("[Orchestrator] 图片转换失败，跳过 QA")
                break

            if round_i == 1 or not eval_results:
                eval_results = self.evaluator.evaluate_all(images, outline)
                if not eval_results:
                    break
            else:
                reeval_indices = sorted(repaired_pages_last_round)
                if not reeval_indices:
                    print("[Orchestrator] 没有新增修复页，跳过后续视觉复评")
                    break
                refreshed_results = self.evaluator.evaluate_all(images, outline, slide_indices=reeval_indices)
                if not refreshed_results:
                    break
                refreshed_by_index = {result.slide_index: result for result in refreshed_results}
                eval_results = [
                    refreshed_by_index.get(result.slide_index, result)
                    for result in eval_results
                ]

            hard_fail_candidates = [r for r in eval_results if self.evaluator.is_hard_fail(r)]
            force_issue_candidates = [
                r for r in eval_results
                if not self.evaluator.is_hard_fail(r)
                and self.evaluator.is_force_issue_fail(r)
                and self.evaluator.needs_revision(r)
            ]

            if round_i > 1 and not hard_fail_candidates:
                print("[Orchestrator] 已无低于硬阈值的页面，跳过后续视觉修复轮次")
                break

            revision_candidates = list(hard_fail_candidates)
            revision_candidates.extend(
                item for item in force_issue_candidates
                if item.slide_index not in {result.slide_index for result in revision_candidates}
            )

            remaining_budget = max(0, self.evaluator.max_total_revision_pages - len(repaired_pages_total))
            if remaining_budget <= 0:
                print(
                    f"[Orchestrator] 已达到本次视觉修复总预算 {self.evaluator.max_total_revision_pages} 页，"
                    "停止继续返工"
                )
                break

            low_score = sorted(
                revision_candidates,
                key=self.evaluator.revision_priority,
            )[: min(self.evaluator.max_revision_candidates_per_round, remaining_budget)]

            if not low_score:
                if did_fix:
                    print(f"[Orchestrator] QA 通过，修复后所有页评分达标")
                else:
                    print(f"[Orchestrator] QA 首轮无低分页，所有页评分达标")
                break

            print(
                f"[Orchestrator] 第 {round_i} 轮待修页 {len(revision_candidates)} 页，"
                f"本轮仅修复优先级最高的 {len(low_score)} 页..."
            )
            did_fix = True
            research_results = research_results or []
            image_paths = image_paths or []
            current_round_repaired: set[int] = set()

            for result in low_score:
                idx = result.slide_index
                if idx >= len(outline.slides):
                    continue
                slide = outline.slides[idx]
                if on_revision_start:
                    on_revision_start(
                        {
                            "slide_index": idx,
                            "slide_title": slide.topic,
                            "round": round_i,
                            "overall": float(result.overall),
                        }
                    )
                research = research_results[idx] if idx < len(research_results) else None
                img = image_paths[idx] if idx < len(image_paths) else None
                layout_intent = self.planner._layout_planner.plan_layout_intent(
                    slide,
                    research=research,
                    image_path=img,
                )
                prev_summary = "\n".join(
                    self.planner._summarize_slide_for_continuity(s)
                    for s in outline.slides[:idx]
                )
                print(f"[Orchestrator] 重新生成第 {idx} 页（overall={result.overall:.1f}）...")
                try:
                    new_code = self.planner.plan_slide(
                        slide, theme, research, img, layout_intent,
                        prev_slides_summary=prev_summary,
                        revision_feedback=result,
                        trigger_stage="visual_qa",
                        audience="general",
                    )
                except Exception as exc:
                    if on_revision_failed:
                        on_revision_failed(
                            {
                                "slide_index": idx,
                                "slide_title": slide.topic,
                                "round": round_i,
                                "overall": float(result.overall),
                                "detail": str(exc),
                            }
                        )
                    raise
                if idx < len(slide_codes):
                    slide_codes[idx] = new_code
                repaired_pages_total.add(idx)
                current_round_repaired.add(idx)

            output_path = self.planner.assemble_pptx(slide_codes, output_path, theme)
            if current_round_repaired and on_revision_round_complete:
                on_revision_round_complete(
                    {
                        "round": round_i,
                        "slide_indices": sorted(current_round_repaired),
                        "output_path": output_path,
                    }
                )
            repaired_pages_last_round = current_round_repaired

        return output_path

    def _run_content_and_coherence_qa(self, pptx_path: str, outline) -> list[dict]:
        print("[Orchestrator] 文本 QA：提取 PPTX 文本内容...")
        content = read_pptx(pptx_path)
        if not content:
            print("[Orchestrator] markitdown 不可用，跳过文本 QA")
            return []

        content_issues = self._content_qa_from_text(content, outline)
        coherence_issues = self._deck_coherence_qa_from_text(content, outline)
        llm_coherence_issues = self._deck_coherence_llm_review(content, outline)
        merged = self._merge_issue_groups(content_issues, coherence_issues, llm_coherence_issues)

        if not merged:
            print("[Orchestrator] 文本 QA 通过，无内容问题")

        return merged

    def _content_qa(self, pptx_path: str, outline) -> list[dict]:
        """
        内容结构 QA：检查单页内容完整性与局部主题对齐。
        """
        content = read_pptx(pptx_path)
        if not content:
            return []
        return self._content_qa_from_text(content, outline)

    def _content_qa_from_text(self, content: str, outline) -> list[dict]:
        slide_text_map = self._build_slide_text_map(content)
        issues_list = []
        previous_meaningful_slide: tuple[int, str, str] | None = None
        content_policy = self._content_qa_policy()
        placeholder_patterns = content_policy.get("placeholder_patterns", [])
        placeholder_regex = "|".join(re.escape(str(item)) for item in placeholder_patterns if str(item).strip())
        min_text_length_by_layout = content_policy.get("min_text_length_by_layout", {})
        max_text_length_by_layout = content_policy.get("max_text_length_by_layout", {})
        toc_min_expected_topics = int(content_policy.get("toc_min_expected_topics", 2))
        duplicate_similarity_threshold = float(content_policy.get("duplicate_similarity_threshold", 0.72))
        content_slide_topics = [
            slide.topic
            for slide in outline.slides
            if slide.layout.value in ("content", "two_column") and slide.topic
        ]

        for slide in outline.slides:
            idx = slide.slide_index
            issues = []
            slide_content = slide_text_map.get(idx, "")

            if not slide_content and slide.layout.value not in ("cover", "closing"):
                issues.append(self._policy_issue_message("content_qa", "empty"))
            elif slide_content:
                placeholders = re.findall(placeholder_regex, slide_content, re.IGNORECASE) if placeholder_regex else []
                if placeholders:
                    issues.append(self._policy_issue_message("content_qa", "placeholder", matches=placeholders))

                text_len = len(slide_content.strip())
                min_text_length = int(min_text_length_by_layout.get(slide.layout.value, 0) or 0)
                max_text_length = int(max_text_length_by_layout.get(slide.layout.value, 0) or 0)
                if min_text_length and text_len < min_text_length:
                    issues.append(self._policy_issue_message("content_qa", "too_short", text_len=text_len))
                if max_text_length and text_len > max_text_length:
                    issues.append(self._policy_issue_message("content_qa", "too_dense", text_len=text_len))

                if slide.layout.value == "toc":
                    mentioned_topics = sum(
                        1 for topic in content_slide_topics if self._topic_mentioned(slide_content, topic)
                    )
                    expected_topics = min(toc_min_expected_topics, len(content_slide_topics))
                    if expected_topics and mentioned_topics < expected_topics:
                        issues.append(self._policy_issue_message("content_qa", "toc_coverage"))

                if slide.layout.value not in ("cover", "closing", "toc"):
                    if not self._topic_mentioned(slide_content, slide.topic):
                        issues.append(self._policy_issue_message("content_qa", "missing_title", topic=slide.topic))

                    if not self._content_aligned_with_outline(slide_content, slide.topic, slide.objective):
                        issues.append(self._policy_issue_message("content_qa", "misaligned"))

                if slide.layout.value in ("content", "two_column"):
                    normalized_current = self._normalize_slide_text(slide_content)
                    if previous_meaningful_slide and normalized_current:
                        prev_idx, prev_topic, prev_text = previous_meaningful_slide
                        similarity = SequenceMatcher(None, prev_text, normalized_current).ratio()
                        if similarity >= duplicate_similarity_threshold:
                            issues.append(self._policy_issue_message(
                                "content_qa",
                                "duplicate_progression",
                                prev_idx=prev_idx,
                                prev_topic=prev_topic,
                            ))
                    if normalized_current:
                        previous_meaningful_slide = (idx, slide.topic, normalized_current)

            if issues:
                print(f"[ContentQA] 第 {idx} 页问题：{'; '.join(issues)}")
                issues_list.append(
                    {
                        "slide_index": idx,
                        "issues": issues,
                        "suggestions": self._suggest_content_fixes(issues),
                    }
                )

        return issues_list

    def _deck_coherence_qa_from_text(self, content: str, outline) -> list[dict]:
        """
        Deck-level coherence QA：检查目录导航、跨页推进、结尾收束等全局问题。
        """
        slide_text_map = self._build_slide_text_map(content)
        issues_list: list[dict] = []
        deck_policy = self._deck_coherence_policy()
        summary_cues = tuple(str(item) for item in deck_policy.get("summary_cues", []))
        content_slides = [
            slide for slide in outline.slides
            if slide.layout.value in ("content", "two_column")
        ]

        # 1. 连续内容页开头重复，说明整套 deck 缺乏推进
        for previous, current in zip(content_slides, content_slides[1:]):
            previous_text = slide_text_map.get(previous.slide_index, "")
            current_text = slide_text_map.get(current.slide_index, "")
            if not previous_text or not current_text:
                continue
            previous_lead = self._leading_sentence(previous_text)
            current_lead = self._leading_sentence(current_text)
            if previous_lead and current_lead and previous_lead == current_lead:
                issue = self._policy_issue_message(
                    "deck_coherence",
                    "same_lead",
                    prev_idx=previous.slide_index,
                    prev_topic=previous.topic,
                )
                issues_list.append(
                    {
                        "slide_index": current.slide_index,
                        "issues": [issue],
                        "suggestions": [self._policy_suggestion("deck_coherence", "same_lead")],
                    }
                )

        # 2. 收束页应回扣前文，而不只是“谢谢观看”
        closing_slides = [slide for slide in outline.slides if slide.layout.value == "closing"]
        if closing_slides and content_slides:
            closing_slide = closing_slides[-1]
            closing_text = slide_text_map.get(closing_slide.slide_index, "")
            if closing_text:
                topic_mentions = sum(
                    1 for slide in content_slides if self._topic_mentioned(closing_text, slide.topic)
                )
                has_summary_cue = any(
                    cue in closing_text for cue in summary_cues
                )
                if topic_mentions == 0 and not has_summary_cue:
                    issues_list.append(
                        {
                            "slide_index": closing_slide.slide_index,
                            "issues": [self._policy_issue_message("deck_coherence", "weak_closing")],
                            "suggestions": [self._policy_suggestion("deck_coherence", "weak_closing")],
                        }
                    )

        return issues_list

    def _deck_coherence_llm_review(self, content: str, outline) -> list[dict]:
        """
        LLM-based deck review focused on Content + Coherence.
        Falls back silently when the planner client is unavailable.
        """
        planner = getattr(self, "planner", None)
        planner_client = getattr(planner, "client", None)
        planner_model = getattr(planner, "model", "")
        if planner_client is None or not planner_model:
            return []

        outline_lines = "\n".join(
            f"- 第 {slide.slide_index} 页 | layout={slide.layout.value} | topic={slide.topic} | objective={slide.objective}"
            for slide in outline.slides
        )
        prompt_context = SkillContext(
            phase="evaluation-and-repair",
            trigger_stage="deck_coherence_review",
            layout_scope="deck",
            visual_mode_scope="text_only",
            provider=planner_model,
        )
        prevention_bundle = self._skill_runtime.build_prevention_bundle(
            context=prompt_context,
            heading="## 长期技能目录（整份 deck 连贯性评估）",
            max_items=2,
        )
        if self.harness_trace and prevention_bundle.loaded_records:
            self.harness_trace.record(
                stage="deck_coherence_review",
                payload=prevention_bundle.to_trace_payload(
                    mode="prevention",
                    context=prompt_context,
                ),
            )

        user_prompt = (
            self._deck_coherence_user_template
            .replace("{topic}", outline.topic)
            .replace("{outline_lines}", outline_lines)
            .replace("{deck_text}", content[:12000])
        )
        last_error = ""
        last_error_signature: str | None = None

        for attempt in range(1, 3):
            loaded_repair_memory_ids: list[str] = []
            attempt_user = merge_prompt_sections(
                PromptSection(source_type="static_prompt", identifier="deck_coherence_review:user", content=user_prompt),
                prevention_bundle,
            )
            if last_error_signature:
                repair_bundle = self._skill_runtime.build_repair_bundle(
                    context=prompt_context,
                    error_signature=last_error_signature,
                    max_items=1,
                )
                loaded_repair_memory_ids = list(repair_bundle.runtime_memory_ids)
                if self.harness_trace and repair_bundle.loaded_records:
                    self.harness_trace.record(
                        stage="deck_coherence_review",
                        payload=repair_bundle.to_trace_payload(
                            mode="repair",
                            context=prompt_context,
                            attempt=attempt,
                            error_signature=last_error_signature,
                        ),
                    )
                attempt_user = merge_prompt_sections(
                    attempt_user,
                    repair_bundle,
                    PromptSection(
                        source_type="repair_feedback",
                        identifier="deck_coherence_review:retry_feedback",
                        content="\n".join(
                            self._coherence_repair_orchestrator.build_retry_feedback(
                                error=last_error,
                                error_signature=last_error_signature,
                                layout_scope="deck",
                                visual_mode_scope="text_only",
                            )
                        ),
                    ),
                )
            try:
                raw, _ = stream_chat_completion_text(
                    planner_client,
                    model=planner_model,
                    max_tokens=1200,
                    messages=[
                        {"role": "system", "content": self._deck_coherence_system_template},
                        {"role": "user", "content": attempt_user},
                    ],
                    **build_chat_completion_kwargs(planner_model),
                )
                data = self._extract_json_object(raw)
                normalized = self._normalize_deck_review_issues(
                    data.get("deck_issues"),
                    max_slide_index=max((slide.slide_index for slide in outline.slides), default=-1),
                )
                if last_error_signature:
                    repair_instruction = self._coherence_repair_orchestrator.build_repair_instruction(
                        error_signature=last_error_signature,
                        error=last_error,
                        layout_scope="deck",
                        visual_mode_scope="text_only",
                    )
                    self._coherence_repair_orchestrator.remember_success(
                        trigger_stage="deck_coherence_review",
                        error_signature=last_error_signature,
                        error=last_error,
                        repair_instruction=repair_instruction,
                        layout_scope="deck",
                        visual_mode_scope="text_only",
                        provider_scope=planner_model,
                        before_pattern=raw[:400],
                        after_pattern=json.dumps(normalized, ensure_ascii=False)[:400],
                        conditions=[f"slide_count={len(outline.slides)}"],
                    )
                return normalized
            except Exception as exc:
                for memory_id in dict.fromkeys(loaded_repair_memory_ids):
                    self._coherence_repair_orchestrator.mark_memory_failure(memory_id)
                last_error = str(exc)
                last_error_signature = self._coherence_repair_orchestrator.classify_error(
                    last_error,
                    stage="content_evaluation",
                )
        return []

    @staticmethod
    def _build_slide_text_map(content: str) -> dict[int, str]:
        slide_text_map: dict[int, str] = {}
        for raw in content.split("<!-- Slide number:"):
            stripped = raw.strip()
            if not stripped:
                continue
            match = re.match(r"(\d+)\b", stripped)
            if not match:
                continue
            slide_index = int(match.group(1)) - 1
            slide_text_map[slide_index] = stripped
        return slide_text_map

    @staticmethod
    def _normalize_slide_text(text: str) -> str:
        lowered = re.sub(r"<!--.*?-->", " ", text, flags=re.S).lower()
        lowered = re.sub(r"\s+", " ", lowered)
        lowered = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", lowered)
        return lowered.strip()

    @staticmethod
    def _leading_sentence(text: str) -> str:
        stripped = re.sub(r"<!--.*?-->", " ", text, flags=re.S).strip()
        stripped = re.sub(r"^\d+\s+", "", stripped)
        lines = [line.strip() for line in stripped.splitlines() if line.strip()]
        if not lines:
            return ""
        body = lines[1] if len(lines) > 1 else lines[0]
        sentence = re.split(r"[。！？.!?；;]", body, maxsplit=1)[0]
        sentence = re.sub(r"\s+", " ", sentence).strip()
        return sentence[:80]

    @staticmethod
    def _topic_variants(topic: str) -> list[str]:
        cleaned = (topic or "").strip()
        if not cleaned:
            return []
        parts = re.split(r"[\s/,_，。；：:（）()\-\|]+", cleaned)
        variants = [cleaned]
        variants.extend(part.strip() for part in parts if len(part.strip()) >= 2)
        if len(cleaned) >= 4:
            variants.append(cleaned[:4])
        if len(cleaned) >= 6:
            variants.append(cleaned[:6])
        deduped: list[str] = []
        for item in variants:
            if item and item not in deduped:
                deduped.append(item)
        return deduped

    def _topic_mentioned(self, slide_content: str, topic: str) -> bool:
        return any(token in slide_content for token in self._topic_variants(topic))

    def _content_aligned_with_outline(self, slide_content: str, topic: str, objective: str) -> bool:
        if self._topic_mentioned(slide_content, topic):
            return True
        objective_variants = self._topic_variants(objective)
        return any(token in slide_content for token in objective_variants)

    @staticmethod
    def _merge_issue_groups(*issue_groups: list[dict]) -> list[dict]:
        merged: dict[int, dict[str, object]] = {}
        for group in issue_groups:
            for item in group:
                slide_index = int(item.get("slide_index", -1))
                if slide_index < 0:
                    continue
                bucket = merged.setdefault(
                    slide_index,
                    {"slide_index": slide_index, "issues": [], "suggestions": []},
                )
                for key in ("issues", "suggestions"):
                    for value in item.get(key, []):
                        if value and value not in bucket[key]:
                            bucket[key].append(value)
        return [merged[idx] for idx in sorted(merged)]

    @staticmethod
    def _extract_json_object(raw: str) -> dict[str, Any]:
        cleaned = re.sub(r"^```(?:json)?\s*", "", str(raw or "").strip())
        cleaned = re.sub(r"\s*```$", "", cleaned)
        candidates = [cleaned] if cleaned else []
        if "{" in cleaned and "}" in cleaned:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if end > start:
                fragment = cleaned[start:end + 1]
                if fragment not in candidates:
                    candidates.append(fragment)
        for candidate in candidates:
            try:
                data = json.loads(candidate)
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
        raise ValueError(f"无法解析 deck coherence JSON，原始内容前 200 字：{cleaned[:200]}")

    @staticmethod
    def _coerce_string_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()]

    def _normalize_deck_review_issues(self, deck_issues: Any, *, max_slide_index: int) -> list[dict]:
        if not isinstance(deck_issues, list):
            return []
        normalized: list[dict] = []
        for item in deck_issues:
            if not isinstance(item, dict):
                continue
            try:
                slide_index = int(item.get("slide_index", -1))
            except Exception:
                continue
            if slide_index < 0 or slide_index > max_slide_index:
                continue
            issues = self._coerce_string_list(item.get("issues"))[:3]
            suggestions = self._coerce_string_list(item.get("suggestions"))[:2]
            if not issues:
                continue
            normalized.append(
                {
                    "slide_index": slide_index,
                    "issues": issues,
                    "suggestions": suggestions or self._suggest_content_fixes(issues),
                }
            )
        return normalized

    @staticmethod
    def _dedupe_keep_order(values: list[str]) -> list[str]:
        deduped: list[str] = []
        for item in values:
            if item and item not in deduped:
                deduped.append(item)
        return deduped

    def _policy_issue_message(self, section: str, issue_key: str, **kwargs: Any) -> str:
        if section == "content_qa":
            template = self._content_qa_policy().get("issue_messages", {}).get(issue_key, "")
        else:
            template = self._deck_coherence_policy().get("issue_messages", {}).get(issue_key, "")
        template = str(template or issue_key)
        try:
            return template.format(**kwargs)
        except Exception:
            return template

    def _policy_suggestion(self, section: str, suggestion_key: str) -> str:
        if section == "content_qa":
            fallback = self._content_qa_policy().get("fallback_suggestion", "强化本页的信息目标、标题层级和叙事推进，避免弱信息页。")
            for item in self._content_qa_policy().get("suggestions", []):
                if str(item.get("match", "")).strip() == suggestion_key:
                    return str(item.get("text", fallback)).strip() or str(fallback)
            return str(fallback)
        template = self._deck_coherence_policy().get("suggestions", {}).get(suggestion_key, "")
        return str(template or "").strip()

    def _suggest_content_fixes(self, issues: list[str]) -> list[str]:
        suggestions: list[str] = []
        policy = self._content_qa_policy()
        rule_suggestions = policy.get("suggestions", [])
        fallback = str(policy.get("fallback_suggestion", "强化本页的信息目标、标题层级和叙事推进，避免弱信息页。")).strip()
        for issue in issues:
            matched = False
            for rule in rule_suggestions:
                marker = str(rule.get("match", "")).strip()
                text = str(rule.get("text", "")).strip()
                if marker and marker in issue and text:
                    suggestions.append(text)
                    matched = True
            if not matched and fallback:
                suggestions.append(fallback)

        if not suggestions:
            suggestions.append(fallback)

        return self._dedupe_keep_order(suggestions)[:3]

    def _fix_content_issues(
        self, content_issues, slide_codes, theme, outline, research_results, image_paths, output_path
    ) -> tuple[str, list[str], dict]:
        """针对文本 QA 发现的问题页重新生成。"""
        research_results = research_results or []
        image_paths = image_paths or []

        for issue in content_issues:
            idx = issue["slide_index"]
            if idx >= len(outline.slides) or idx >= len(slide_codes):
                continue
            slide = outline.slides[idx]
            research = research_results[idx] if idx < len(research_results) else None
            img = image_paths[idx] if idx < len(image_paths) else None
            layout_intent = self.planner._layout_planner.plan_layout_intent(
                slide,
                research=research,
                image_path=img,
            )
            prev_summary = "\n".join(
                self.planner._summarize_slide_for_continuity(s)
                for s in outline.slides[:idx]
            )

            # 构造一个简单的 feedback 对象传递文本问题
            from backend.models.schemas import SlideEvalResult
            feedback = SlideEvalResult(
                slide_index=idx,
                layout_score=3.0,
                content_score=1.0,
                design_score=3.0,
                overall=2.0,
                issues=issue["issues"],
                suggestions=issue.get("suggestions") or [
                    "确保页面标题存在",
                    "确保内容要点完整呈现",
                    "不要留下占位符文字",
                ],
            )

            print(f"[Orchestrator] 重新生成第 {idx} 页（文本问题）...")
            new_code = self.planner.plan_slide(
                slide, theme, research, img, layout_intent,
                prev_slides_summary=prev_summary,
                revision_feedback=feedback,
                trigger_stage="content_qa",
                audience="general",
            )
            slide_codes[idx] = new_code

        result_path = self.planner.assemble_pptx(slide_codes, output_path, theme)
        return result_path, slide_codes, theme

    def _research_and_assets(self, outline, language: str):
        """Research 先执行，完成后用结果丰富 image_prompt，再获取图片。"""
        import uuid
        job_id = str(uuid.uuid4())[:8]
        slides = self.planner.outline_to_research_slides(outline)

        print("[Orchestrator] ResearchAgent 逐页研究中...")
        try:
            research_results = asyncio.run(self.researcher.research_all(slides, language=language))
        except Exception as e:
            print(f"[Orchestrator] ResearchAgent 失败: {e}")
            research_results = []

        researched = sum(1 for r in research_results if r and r.get("bullet_points"))
        print(f"[Orchestrator] ResearchAgent 完成，{researched} 页拿到研究要点")

        enriched_outline = self.planner.enrich_image_prompts(outline, research_results)

        print("[Orchestrator] AssetAgent 获取图片中...")
        try:
            image_paths = asyncio.run(self.asset_agent.fetch_all(enriched_outline.slides, job_id=job_id))
        except Exception as e:
            print(f"[Orchestrator] AssetAgent 失败: {e}")
            image_paths = []

        fetched = sum(1 for p in image_paths if p)
        print(f"[Orchestrator] AssetAgent 完成，{fetched} 页获取到图片")

        return enriched_outline, research_results, image_paths

    def _research_outline(self, outline, language: str) -> list[dict | None]:
        """仅 research，不获取图片。"""
        print("[Orchestrator] ResearchAgent 逐页研究中...")
        try:
            slides = self.planner.outline_to_research_slides(outline)
            results = asyncio.run(self.researcher.research_all(slides, language=language))
        except Exception as e:
            print(f"[Orchestrator] ResearchAgent 跳过: {e}")
            return []
        researched_pages = sum(1 for r in results if r and r.get("bullet_points"))
        print(f"[Orchestrator] ResearchAgent 完成，{researched_pages} 页拿到研究要点")
        return results

    def _fetch_assets(self, outline, language: str) -> list:
        """仅获取图片，不 research。"""
        import uuid
        job_id = str(uuid.uuid4())[:8]
        print("[Orchestrator] AssetAgent 获取图片中...")
        try:
            paths = asyncio.run(self.asset_agent.fetch_all(outline.slides, job_id=job_id))
        except Exception as e:
            print(f"[Orchestrator] AssetAgent 跳过: {e}")
            return []
        return paths

    def _print_debug(self, pptx_path: str):
        """用 markitdown 提取内容做调试输出"""
        content = read_pptx(pptx_path)
        if content:
            print(f"\n{'─'*50}")
            print("[DEBUG] 提取的文本内容：")
            print(content[:2000])
            print(f"{'─'*50}\n")
        else:
            print("[DEBUG] markitdown 不可用，跳过内容提取")
