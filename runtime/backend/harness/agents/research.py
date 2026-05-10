import json
import re
import asyncio
import logging
import uuid
from typing import Callable
from openai import AsyncOpenAI
from backend.models.schemas import SlideSpec, SlideLayout, TextElement
from backend.tools.openai_compat import async_stream_chat_completion_text, build_chat_completion_kwargs
from backend.tools.search_backend import SearchBackend
from backend.harness.runtime import (
    HarnessTrace,
    PromptComposer,
    PromptSection,
    RepairOrchestrator,
    SkillContext,
    SkillRuntime,
    merge_prompt_sections,
)
import config
from backend.tools.openai_compat import _extract_text_from_obj

logger = logging.getLogger(__name__)
MAX_RETRIES = 3


class ResearchAgent:
    """用当前配置的检索后端搜索每页主题，再用 LLM 提炼为 PPT 要点。"""

    SKIP_LAYOUTS = {SlideLayout.COVER, SlideLayout.CLOSING, SlideLayout.TOC}

    def __init__(
        self,
        model_provider: str = "minmax",
        thinking_callback: Callable[[str], None] | None = None,
        search_callback: Callable[[dict], None] | None = None,
        harness_trace: HarnessTrace | None = None,
    ):
        self._composer = PromptComposer()
        runtime_candidate = getattr(self._composer, "runtime", None)
        self._skill_runtime = runtime_candidate if isinstance(runtime_candidate, SkillRuntime) else SkillRuntime()
        self._repair_orchestrator = RepairOrchestrator(
            self._skill_runtime,
            run_id=uuid.uuid4().hex[:8],
            phase="research-synthesis",
        )
        self.harness_trace = harness_trace
        self.search_backend = SearchBackend()
        if not self.search_backend.enabled:
            self.llm = None
            self.client = None
            self._system_template = ""
            self._user_template = ""
            self.last_reasoning = ""
            self.thinking_callback = thinking_callback
            self.search_callback = search_callback
            print("[Research] 未配置可用检索后端（Tavily/SearXNG），Research 功能已禁用")
            return

        provider_settings = config.get_llm_provider_settings(model_provider)
        self.model_id = provider_settings["model_id"]
        self.llm = AsyncOpenAI(
            api_key=provider_settings["api_key"] or config.RESEARCH_API_KEY,
            base_url=provider_settings["base_url"] or config.RESEARCH_BASE_URL,
        )
        self.client = self.llm
        self.last_reasoning = ""
        self.thinking_callback = thinking_callback
        self.search_callback = search_callback
        self._system_template = self._composer.load_research_synthesis_system_prompt()
        self._user_template = self._composer.load_research_synthesis_user_prompt_template()
        self._budget_system_template = self._composer.load_research_budgeting_system_prompt()
        self._budget_user_template = self._composer.load_research_budgeting_user_prompt_template()
        self._budget_rebalance_system_template = self._composer.load_research_budget_rebalance_system_prompt()
        self._budget_rebalance_user_template = self._composer.load_research_budget_rebalance_user_prompt_template()
        self._degraded_notice_template = self._composer.load_research_degraded_notice_template()

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

    def _handle_reasoning_chunk(self, chunk: str) -> None:
        if not chunk:
            return
        self.last_reasoning += chunk
        if self.thinking_callback:
            self.thinking_callback(chunk)

    async def _decide_search_result_budget(self, slide: SlideSpec, language: str) -> int:
        if not self.client:
            return 5

        hint_lines: list[str] = []
        seen_lines: set[str] = set()
        for item in slide.elements:
            line = str(getattr(item, "content", "") or "").strip()
            if not line or line == (slide.topic or "").strip() or line in seen_lines:
                continue
            seen_lines.add(line)
            hint_lines.append(line)
            if len(hint_lines) >= 6:
                break

        objective = slide.speaker_notes or ""
        user_prompt = self._budget_user_template.format(
            language=language,
            layout=slide.layout.value,
            topic=slide.topic or "",
            objective=objective,
            existing_hints="\n".join(hint_lines) or "无",
        )

        if self.harness_trace:
            self.harness_trace.record(
                stage="research_budgeting",
                payload={
                    "mode": "decision",
                    "topic": slide.topic,
                    "slide_index": slide.slide_index,
                    "layout": slide.layout.value,
                    "prompt_excerpt": user_prompt[:400],
                },
            )

        try:
            response = await self.client.chat.completions.create(
                model=self.model_id,
                messages=[
                    {"role": "system", "content": self._budget_system_template},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=8,
            )
            message = getattr(response.choices[0], "message", None)
            raw_content = getattr(message, "content", "") if message is not None else ""
            content = _extract_text_from_obj(raw_content) or str(raw_content or "")
            match = re.search(r"\d+", content)
            if match:
                budget = max(2, min(10, int(match.group(0))))
                logger.info(
                    "[Research] 预算判定 slide=%s layout=%s budget=%s raw=%s",
                    slide.topic,
                    slide.layout.value,
                    budget,
                    content[:80],
                )
                if self.harness_trace:
                    self.harness_trace.record(
                        stage="research_budgeting",
                        payload={
                            "mode": "decision_result",
                            "topic": slide.topic,
                            "slide_index": slide.slide_index,
                            "layout": slide.layout.value,
                            "budget": budget,
                            "raw_output": content[:120],
                            "model": self.model_id,
                        },
                    )
                return budget
            if self.harness_trace:
                self.harness_trace.record(
                    stage="research_budgeting",
                    payload={
                        "mode": "decision_unparsed",
                        "topic": slide.topic,
                        "slide_index": slide.slide_index,
                        "layout": slide.layout.value,
                        "raw_output": content[:200],
                    },
                )
            logger.warning("[Research] 预算判定未解析到数字 slide=%s raw=%s", slide.topic, content[:120])
        except Exception as exc:
            logger.warning("[Research] 预算判定失败，使用默认值 slide=%s error=%s", slide.topic, exc)
            if self.harness_trace:
                self.harness_trace.record(
                    stage="research_budgeting",
                    payload={
                        "mode": "decision_error",
                        "topic": slide.topic,
                        "slide_index": slide.slide_index,
                        "layout": slide.layout.value,
                        "error": str(exc),
                    },
                )
        return 5

    async def _rebalance_search_result_budgets(
        self,
        slides: list[SlideSpec],
        initial_budgets: list[int],
        language: str,
    ) -> list[int]:
        if not self.client or not slides:
            return initial_budgets

        slide_lines: list[str] = []
        for idx, (slide, budget) in enumerate(zip(slides, initial_budgets), start=1):
            objective = str(slide.speaker_notes or "").strip() or "无"
            hint_lines: list[str] = []
            for item in slide.elements[:4]:
                content = str(getattr(item, "content", "") or "").strip()
                if content and content != (slide.topic or ""):
                    hint_lines.append(content)
            hints = " / ".join(hint_lines[:3]) or "无"
            slide_lines.append(
                f"{idx}. layout={slide.layout.value}; topic={slide.topic}; objective={objective}; hints={hints}; first_budget={budget}"
            )

        system_prompt = self._budget_rebalance_system_template
        user_prompt = self._budget_rebalance_user_template.format(
            language=language,
            slide_count=len(slides),
            slide_lines="\n".join(slide_lines),
        )

        try:
            response = await self.client.chat.completions.create(
                model=self.model_id,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
                max_tokens=min(128, max(32, len(slides) * 8)),
            )
            message = getattr(response.choices[0], "message", None)
            raw_content = getattr(message, "content", "") if message is not None else ""
            content = _extract_text_from_obj(raw_content) or str(raw_content or "")
            parsed = json.loads(content)
            if isinstance(parsed, list) and len(parsed) == len(slides):
                budgets = [max(2, min(10, int(item))) for item in parsed]
                if len(set(budgets)) > 1:
                    logger.info("[Research] deck budget rebalance succeeded budgets=%s", budgets)
                    if self.harness_trace:
                        self.harness_trace.record(
                            stage="research_budgeting",
                            payload={
                                "mode": "deck_rebalance_result",
                                "budgets": budgets,
                                "raw_output": content[:240],
                                "slide_count": len(slides),
                            },
                        )
                    return budgets
            logger.warning("[Research] deck budget rebalance output invalid raw=%s", content[:200])
            if self.harness_trace:
                self.harness_trace.record(
                    stage="research_budgeting",
                    payload={
                        "mode": "deck_rebalance_invalid",
                        "raw_output": content[:240],
                        "slide_count": len(slides),
                    },
                )
        except Exception as exc:
            logger.warning("[Research] deck budget rebalance failed error=%s", exc)
            if self.harness_trace:
                self.harness_trace.record(
                    stage="research_budgeting",
                    payload={
                        "mode": "deck_rebalance_error",
                        "error": str(exc),
                        "slide_count": len(slides),
                    },
                )
        return initial_budgets

    async def _decide_search_result_budgets(
        self,
        slides: list[SlideSpec],
        language: str,
        concurrency: int,
    ) -> list[int]:
        if not slides:
            return []

        sem = asyncio.Semaphore(max(1, concurrency))

        async def _bounded(slide: SlideSpec) -> int:
            async with sem:
                return await self._decide_search_result_budget(slide, language)

        budgets = list(await asyncio.gather(*[_bounded(slide) for slide in slides]))
        if len(slides) <= 1:
            return budgets

        repeated_ratio = max((budgets.count(value) / len(budgets)) for value in set(budgets))
        if len(set(budgets)) == 1 or repeated_ratio >= 0.8:
            logger.info("[Research] initial budgets too uniform=%s, trying deck rebalance", budgets)
            rebalanced = await self._rebalance_search_result_budgets(slides, budgets, language)
            if len(rebalanced) == len(budgets):
                return rebalanced
        return budgets

    async def research_topic(self, topic: str, language: str = "中文") -> dict:
        """
        对整份 PPT 主题做一次前置研究，供 Planner 生成时参考。
        当前主流程还没有“先产出 slides 再逐页 research”的中间态，
        所以这里用一个 synthetic content slide 复用现有 research_slide 逻辑。
        """
        slide = SlideSpec(
            slide_index=0,
            layout=SlideLayout.CONTENT,
            topic=topic,
            elements=[
                TextElement(
                    type="title",
                    content=topic,
                    x=0.5,
                    y=0.3,
                    width=12.0,
                    height=0.9,
                    font_size=32,
                    bold=True,
                    color="#1F3864",
                )
            ],
        )
        result = await self.research_slide(slide, language=language)
        return result or {
            "topic": topic,
            "summary": topic,
            "bullet_points": [],
        }

    async def research_slide(
        self,
        slide: SlideSpec,
        language: str = "中文",
        search_result_budget: int | None = None,
    ) -> dict | None:
        """
        对单页做检索 + LLM 提炼。
        cover / closing / toc 直接返回 None。
        失败时返回默认结构，不让全流程崩溃。
        """
        if slide.layout in self.SKIP_LAYOUTS:
            return None
        if not self.client:
            return {
                "topic": slide.topic,
                "summary": slide.topic,
                "bullet_points": [],
                "key_data": [],
            }

        try:
            trigger_stage = "research_synthesis"
            layout_scope = slide.layout.value
            visual_mode_scope = "text_research"
            search_stage = "research_search"
            search_provider = self.search_backend.provider if self.search_backend else "search-disabled"
            search_error = ""
            search_error_signature: str | None = None
            if search_result_budget is None:
                search_result_budget = await self._decide_search_result_budget(slide, language)
            try:
                search_items = await self.search_backend.search_text_results(
                    slide.topic,
                    max_results=search_result_budget,
                )
            except Exception as exc:
                search_error = f"检索失败: {exc}"
                search_error_signature = self._repair_orchestrator.classify_error(
                    search_error,
                    stage=search_stage,
                )
                search_items = []

            if self.search_callback:
                self.search_callback(
                    {
                        "slide_index": slide.slide_index,
                        "topic": slide.topic,
                        "provider": search_provider,
                        "snippet_count": len(search_items),
                        "items": search_items,
                        "max_results": search_result_budget,
                        "search_error": search_error,
                    }
                )
            snippets = [item.get("summary", "") for item in search_items if item.get("summary")]
            context = "\n\n".join(snippets[: min(4, len(snippets))])

            system_prompt = self._system_template.replace("{language}", language)
            user_prompt = (
                self._user_template
                .replace("{topic}", slide.topic)
                .replace("{context}", context[:2000])
            )
            prompt_context = SkillContext(
                phase="research-synthesis",
                trigger_stage=trigger_stage,
                layout_scope=layout_scope,
                visual_mode_scope=visual_mode_scope,
                provider=self.model_id,
                language=language,
            )
            prevention_bundle = self._skill_runtime.build_prevention_bundle(
                context=prompt_context,
                heading="## 长期技能目录（研究综合）",
                max_items=2,
            )
            self._record_prompt_bundle(
                stage=trigger_stage,
                mode="prevention",
                context=prompt_context,
                bundle=prevention_bundle,
            )
            if search_error:
                user_prompt = merge_prompt_sections(
                    PromptSection(source_type="static_prompt", identifier="research:user", content=user_prompt),
                    PromptSection(
                        source_type="fallback_notice",
                        identifier="research:degraded_notice",
                        content=self._degraded_notice_template
                        .replace("{provider}", search_provider or "unknown")
                        .replace("{error_excerpt}", search_error[:180]),
                    ),
                )

            retry_feedback = ""
            last_error = ""
            last_error_signature: str | None = None
            raw = ""
            for attempt in range(1, MAX_RETRIES + 1):
                loaded_repair_memory_ids: list[str] = []
                attempt_user = merge_prompt_sections(user_prompt, prevention_bundle)
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
                            identifier="research:retry_feedback",
                            content=retry_feedback,
                        ),
                    )

                self.last_reasoning = ""
                raw, reasoning_text = await async_stream_chat_completion_text(
                    self.client,
                    model=self.model_id,
                    max_tokens=config.MAX_TOKENS_RESEARCHER,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": attempt_user},
                    ],
                    on_reasoning_chunk=self._handle_reasoning_chunk,
                    **build_chat_completion_kwargs(self.model_id),
                )
                self.last_reasoning = reasoning_text
                try:
                    data = self._parse_json(raw)
                    if "bullet_points" not in data or not isinstance(data["bullet_points"], list):
                        raise ValueError("缺少 bullet_points 字段")
                    if search_error_signature:
                        repair_instruction = self._repair_orchestrator.build_repair_instruction(
                            error_signature=search_error_signature,
                            error=search_error,
                            layout_scope=search_provider,
                            visual_mode_scope="search",
                        )
                        self._repair_orchestrator.remember_success(
                            trigger_stage=search_stage,
                            error_signature=search_error_signature,
                            error=search_error,
                            repair_instruction=repair_instruction,
                            layout_scope=search_provider,
                            visual_mode_scope="search",
                            provider_scope=search_provider,
                            language_scope=language,
                            before_pattern=slide.topic[:200],
                            after_pattern=f"fallback=llm-only,snippets={len(search_items)}",
                            conditions=[f"provider={search_provider}"],
                        )
                    if last_error_signature:
                        repair_instruction = self._repair_orchestrator.build_repair_instruction(
                            error_signature=last_error_signature,
                            error=last_error,
                            layout_scope=layout_scope,
                            visual_mode_scope=visual_mode_scope,
                        )
                        self._repair_orchestrator.remember_success(
                            trigger_stage=trigger_stage,
                            error_signature=last_error_signature,
                            error=last_error,
                            repair_instruction=repair_instruction,
                            layout_scope=layout_scope,
                            visual_mode_scope=visual_mode_scope,
                            provider_scope=self.model_id,
                            language_scope=language,
                            before_pattern=raw[:400],
                            after_pattern=json.dumps(data, ensure_ascii=False)[:400],
                            conditions=[f"topic={slide.topic[:80]}"],
                        )
                    print(f"[Research] 第 {slide.slide_index} 页完成: {slide.topic}")
                    return data
                except Exception as exc:
                    for memory_id in dict.fromkeys(loaded_repair_memory_ids):
                        self._repair_orchestrator.mark_memory_failure(memory_id)
                    last_error = str(exc)
                    last_error_signature = self._repair_orchestrator.classify_error(
                        last_error,
                        stage=trigger_stage,
                    )
                    retry_feedback = "\n".join(
                        self._repair_orchestrator.build_retry_feedback(
                            error=last_error,
                            error_signature=last_error_signature,
                            layout_scope=layout_scope,
                            visual_mode_scope=visual_mode_scope,
                        )
                    )
                    if attempt == MAX_RETRIES:
                        raise

        except Exception as e:
            logger.warning(f"[Research] 第 {slide.slide_index} 页失败: {e}")
            print(f"[Research] 第 {slide.slide_index} 页失败，使用默认内容: {e}")
            return {
                "topic": slide.topic,
                "summary": slide.topic,
                "bullet_points": [],
                "key_data": [],
            }

    async def research_all(
        self, slides: list[SlideSpec], language: str = "中文", concurrency: int = 3
    ) -> list[dict | None]:
        """并发研究所有页面，返回列表长度与 slides 一致。"""
        sem = asyncio.Semaphore(concurrency)
        budgets = await self._decide_search_result_budgets(slides, language, concurrency)

        async def _bounded(slide: SlideSpec, budget: int):
            async with sem:
                return await self.research_slide(slide, language, search_result_budget=budget)

        results = await asyncio.gather(*[_bounded(slide, budgets[index] if index < len(budgets) else 5) for index, slide in enumerate(slides)])
        return list(results)

    def _parse_json(self, raw: str) -> dict:
        cleaned = raw.strip()
        # 去掉 ```json ... ``` 围栏
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

        # 智能替换 JSON 字符串值内的中文引号
        def fix_quotes(text: str) -> str:
            result = []
            in_string = False
            i = 0
            while i < len(text):
                ch = text[i]

                # 处理转义
                if ch == '\\' and i + 1 < len(text):
                    result.append(ch)
                    result.append(text[i + 1])
                    i += 2
                    continue

                # 英文双引号：切换字符串状态
                if ch == '"':
                    in_string = not in_string
                    result.append(ch)
                # 在字符串内：替换中文引号和单引号
                elif in_string:
                    if ch in '""':
                        result.append('\\"')
                    elif ch in '\u2018\u2019':  # 中文单引号
                        result.append("'")
                    else:
                        result.append(ch)
                else:
                    result.append(ch)

                i += 1
            return ''.join(result)

        cleaned = fix_quotes(cleaned)

        # 尝试直接解析
        try:
            result = json.loads(cleaned, strict=False)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError as e:
            logger.debug(f"直接解析失败: {e}")

        # fallback：找最外层的 { 到匹配的 }
        first_brace = cleaned.find("{")
        if first_brace >= 0:
            depth = 0
            in_string = False
            escape = False

            for i in range(first_brace, len(cleaned)):
                char = cleaned[i]

                if escape:
                    escape = False
                    continue

                if char == '\\':
                    escape = True
                    continue

                if char == '"' and not escape:
                    in_string = not in_string
                    continue

                if not in_string:
                    if char == '{':
                        depth += 1
                    elif char == '}':
                        depth -= 1
                        if depth == 0:
                            candidate = cleaned[first_brace:i + 1]
                            try:
                                result = json.loads(candidate, strict=False)
                                if isinstance(result, dict):
                                    return result
                            except json.JSONDecodeError:
                                pass
                            break

        raise ValueError(f"无法从响应中提取 JSON dict，原始内容前200字：{cleaned[:200]}")
