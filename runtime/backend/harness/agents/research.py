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
DEFAULT_SEARCH_RESULT_BUDGET = 5
MIN_SEARCH_RESULT_BUDGET = 2
MAX_SEARCH_RESULT_BUDGET = 10
OUTLINE_CONTEXT_PREFIXES = ("页面位置：", "上一页主题：", "下一页主题：", "邻近页面脉络：")


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

    def _slide_search_context(self, slide: SlideSpec, hint_limit: int = 8) -> dict:
        topic = str(slide.topic or "").strip()
        hint_lines: list[str] = []
        seen_lines: set[str] = set()
        core_descriptions = self._core_slide_descriptions(slide, limit=4)
        deck_context = self._deck_context_lines(slide)
        for item in slide.elements:
            line = str(getattr(item, "content", "") or "").strip()
            if not line or line == topic or line in seen_lines:
                continue
            seen_lines.add(line)
            item_type = str(getattr(item, "type", "") or "body").strip() or "body"
            hint_lines.append(f"{item_type}: {line[:180]}")
            if len(hint_lines) >= hint_limit:
                break

        return {
            "slide_index": slide.slide_index,
            "layout": slide.layout.value,
            "topic": topic,
            "objective": "\n".join(core_descriptions),
            "deck_context": deck_context,
            "text_hints": hint_lines,
            "fallback_query": self._fallback_search_query(slide),
        }

    def _is_outline_context_line(self, line: str) -> bool:
        return any(line.startswith(prefix) for prefix in OUTLINE_CONTEXT_PREFIXES)

    def _deck_context_lines(self, slide: SlideSpec) -> list[str]:
        lines = []
        for line in str(slide.speaker_notes or "").splitlines():
            cleaned = line.strip()
            if cleaned and self._is_outline_context_line(cleaned):
                lines.append(cleaned)
        return lines

    def _core_slide_descriptions(self, slide: SlideSpec, limit: int = 3) -> list[str]:
        topic = str(slide.topic or "").strip()
        descriptions: list[str] = []
        seen: set[str] = set()

        def add_line(raw: str) -> None:
            line = re.sub(r"\s+", " ", str(raw or "").strip())
            if (
                not line
                or line == topic
                or line in seen
                or self._is_outline_context_line(line)
            ):
                return
            seen.add(line)
            descriptions.append(line)

        for line in str(slide.speaker_notes or "").splitlines():
            add_line(line)
        for item in slide.elements:
            add_line(getattr(item, "content", "") or "")
        return descriptions[:limit]

    def _fallback_search_query(self, slide: SlideSpec) -> str:
        parts = [str(slide.topic or "").strip()]
        parts.extend(self._core_slide_descriptions(slide, limit=2))
        query = " ".join(part for part in parts if part)
        return re.sub(r"\s+", " ", query).strip()[:120]

    def _is_title_only_query(self, query: str, slide: SlideSpec) -> bool:
        normalized_query = re.sub(r"\s+", "", query or "")
        normalized_topic = re.sub(r"\s+", "", str(slide.topic or ""))
        return bool(normalized_topic) and normalized_query == normalized_topic

    def _fallback_search_plan(self, slide: SlideSpec) -> dict:
        return {
            "slide_index": slide.slide_index,
            "budget": DEFAULT_SEARCH_RESULT_BUDGET,
            "query": self._fallback_search_query(slide),
            "reason": "model-fallback",
        }

    def _strip_json_fence(self, content: str) -> str:
        cleaned = content.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        return cleaned.strip()

    def _parse_json_array(self, content: str) -> list | None:
        cleaned = self._strip_json_fence(content)
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                for key in ("slides", "plans", "items", "data"):
                    value = parsed.get(key)
                    if isinstance(value, list):
                        return value
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass

        start = cleaned.find("[")
        if start < 0:
            return None
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(cleaned)):
            char = cleaned[index]
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(cleaned[start:index + 1])
                        return parsed if isinstance(parsed, list) else None
                    except Exception:
                        return None
        return None

    def _coerce_search_plan(self, raw_plan, slide: SlideSpec) -> dict:
        fallback = self._fallback_search_plan(slide)
        budget_value = None
        query_value = ""
        reason_value = ""

        if isinstance(raw_plan, dict):
            budget_value = (
                raw_plan.get("budget")
                or raw_plan.get("max_results")
                or raw_plan.get("result_count")
                or raw_plan.get("count")
            )
            query_value = str(raw_plan.get("query") or raw_plan.get("search_query") or "").strip()
            reason_value = str(raw_plan.get("reason") or raw_plan.get("rationale") or "").strip()
        else:
            budget_value = raw_plan

        try:
            budget = max(MIN_SEARCH_RESULT_BUDGET, min(MAX_SEARCH_RESULT_BUDGET, int(budget_value)))
        except Exception:
            budget = fallback["budget"]

        if not query_value or self._is_title_only_query(query_value, slide):
            query_value = fallback["query"]

        return {
            "slide_index": slide.slide_index,
            "budget": budget,
            "query": query_value,
            "reason": reason_value or fallback["reason"],
        }

    def _align_search_plans(self, parsed: list, slides: list[SlideSpec]) -> list[dict] | None:
        if len(parsed) != len(slides):
            return None

        indexed: dict[int, object] = {}
        has_slide_indexes = False
        for item in parsed:
            if not isinstance(item, dict):
                continue
            raw_index = item.get("slide_index")
            if raw_index is None:
                raw_index = item.get("page")
            if raw_index is None:
                raw_index = item.get("page_index")
            try:
                indexed[int(raw_index)] = item
                has_slide_indexes = True
            except Exception:
                continue

        plans: list[dict] = []
        for position, slide in enumerate(slides):
            raw_plan = indexed.get(slide.slide_index) if has_slide_indexes else parsed[position]
            if raw_plan is None:
                return None
            plans.append(self._coerce_search_plan(raw_plan, slide))
        return plans

    async def _decide_search_plans(self, slides: list[SlideSpec], language: str) -> list[dict]:
        if not slides:
            return []
        fallback_plans = [self._fallback_search_plan(slide) for slide in slides]
        if not self.client:
            return fallback_plans

        slide_contexts = [self._slide_search_context(slide) for slide in slides]
        system_prompt = (
            "你负责直接为整套 PPT 的 research 阶段决定每页网页检索计划。\n"
            "必须根据每页的 topic、objective、text_hints、layout 的真实信息需求独立判断，"
            "不要套用固定规则，不要平均分配，不要把 5 当成默认安全答案。\n"
            "输出只能是 JSON 数组，数组长度必须等于输入页数。每个对象字段："
            "slide_index、budget、query、reason。\n"
            f"budget 必须是 {MIN_SEARCH_RESULT_BUDGET} 到 {MAX_SEARCH_RESULT_BUDGET} 之间的整数；"
            "query 是适合搜索引擎的短查询词，必须结合 topic 和 objective 的核心信息；"
            "不要机械只复制标题，也不要粘贴整段长句，优先输出 8 到 30 个中文字/词的关键词组合；"
            "reason 不超过 20 个中文字符。\n"
            "不要输出 Markdown、代码块或解释。"
        )
        user_prompt = (
            f"语言：{language}\n"
            "请直接判断下面每页需要抓取多少条候选网页资料，并为每页生成检索 query。\n"
            f"页面信息 JSON：\n{json.dumps(slide_contexts, ensure_ascii=False)}"
        )

        if self.harness_trace:
            self.harness_trace.record(
                stage="research_search_planning",
                payload={
                    "mode": "deck_decision",
                    "slide_count": len(slides),
                    "prompt_excerpt": user_prompt[:800],
                },
            )

        try:
            response = await self.client.chat.completions.create(
                model=self.model_id,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
                max_tokens=min(4096, max(512, len(slides) * 160)),
            )
            message = getattr(response.choices[0], "message", None)
            raw_content = getattr(message, "content", "") if message is not None else ""
            content = _extract_text_from_obj(raw_content) or str(raw_content or "")
            parsed = self._parse_json_array(content)
            plans = self._align_search_plans(parsed, slides) if isinstance(parsed, list) else None
            if plans:
                logger.info(
                    "[Research] 搜索计划判定完成 budgets=%s queries=%s",
                    [plan["budget"] for plan in plans],
                    [plan["query"] for plan in plans],
                )
                if self.harness_trace:
                    self.harness_trace.record(
                        stage="research_search_planning",
                        payload={
                            "mode": "deck_decision_result",
                            "plans": plans,
                            "raw_output": content[:800],
                            "model": self.model_id,
                        },
                    )
                return plans
            logger.warning("[Research] 搜索计划未解析，使用默认计划 raw=%s", content[:240])
            if self.harness_trace:
                self.harness_trace.record(
                    stage="research_search_planning",
                    payload={
                        "mode": "deck_decision_invalid",
                        "raw_output": content[:800],
                        "slide_count": len(slides),
                    },
                )
        except Exception as exc:
            logger.warning("[Research] 搜索计划判定失败，使用默认计划 error=%s", exc)
            if self.harness_trace:
                self.harness_trace.record(
                    stage="research_search_planning",
                    payload={
                        "mode": "deck_decision_error",
                        "error": str(exc),
                        "slide_count": len(slides),
                    },
                )
        return fallback_plans

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
        search_query: str | None = None,
        search_budget_reason: str | None = None,
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
            if search_result_budget is None or not search_query:
                plans = await self._decide_search_plans([slide], language)
                plan = plans[0] if plans else self._fallback_search_plan(slide)
                if search_result_budget is None:
                    search_result_budget = int(plan.get("budget") or DEFAULT_SEARCH_RESULT_BUDGET)
                if not search_query:
                    search_query = str(plan.get("query") or slide.topic or "").strip()
                if not search_budget_reason:
                    search_budget_reason = str(plan.get("reason") or "").strip()
            search_query = str(search_query or slide.topic or "").strip()
            try:
                search_items = await self.search_backend.search_text_results(
                    search_query,
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
                        "query": search_query,
                        "budget_reason": search_budget_reason or "",
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
        search_plans = await self._decide_search_plans(slides, language)

        async def _bounded(slide: SlideSpec, plan: dict):
            async with sem:
                return await self.research_slide(
                    slide,
                    language,
                    search_result_budget=int(plan.get("budget") or DEFAULT_SEARCH_RESULT_BUDGET),
                    search_query=str(plan.get("query") or slide.topic or "").strip(),
                    search_budget_reason=str(plan.get("reason") or "").strip(),
                )

        results = await asyncio.gather(*[
            _bounded(
                slide,
                search_plans[index] if index < len(search_plans) else self._fallback_search_plan(slide),
            )
            for index, slide in enumerate(slides)
        ])
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
