"""
agents/evaluator.py

用 qwen-vl 对生成的 PPT 每页做视觉评分。
图片 base64 编码后发给多模态模型，返回 SlideEvalResult 列表。
如果 QWEN_API_KEY 未配置，evaluate_all() 直接返回空列表，不影响主流程。
"""
import base64
import json
import logging
import os
import re
import uuid
from functools import cached_property
from pathlib import Path

import config
from backend.tools.usage_recorder import estimate_tokens_from_text, record_usage
from backend.harness.runtime import (
    HarnessTrace,
    PromptComposer,
    PromptSection,
    RepairOrchestrator,
    SkillContext,
    merge_prompt_sections,
)
from backend.models.schemas import OutlinePlan, SlideEvalResult
from openai import OpenAI

logger = logging.getLogger(__name__)
MAX_RETRIES = 3


class EvaluatorAgent:
    def __init__(self, harness_trace: HarnessTrace | None = None):
        self._composer = PromptComposer()
        self._skill_runtime = self._composer.runtime
        self._repair_orchestrator = RepairOrchestrator(
            self._skill_runtime,
            run_id=uuid.uuid4().hex[:8],
            phase="evaluation-and-repair",
        )
        self._system_template = self._composer.load_visual_evaluation_system_prompt()
        self._user_template = self._composer.load_visual_evaluation_user_prompt_template()
        self._failed_issue_template = self._composer.load_visual_evaluation_failed_issue_template()
        self._failed_suggestion_primary_template = self._composer.load_visual_evaluation_failed_suggestion_primary_template()
        self._failed_suggestion_secondary_template = self._composer.load_visual_evaluation_failed_suggestion_secondary_template()
        self.harness_trace = harness_trace
        self.enabled = bool(config.QWEN_API_KEY and config.QWEN_BASE_URL)
        if self.enabled:
            self.client = OpenAI(
                api_key=config.QWEN_API_KEY,
                base_url=config.QWEN_BASE_URL,
            )
        else:
            self.client = None
            print("[Evaluator] QWEN_API_KEY 未配置，视觉 QA 已禁用")

    @cached_property
    def _revision_policy(self) -> dict:
        raw = self._composer.load_visual_evaluation_revision_policy()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"无法解析 visual_revision_policy.json: {exc}") from exc
        return data if isinstance(data, dict) else {}

    @property
    def strict_dimension_threshold(self) -> float:
        value = self._revision_policy.get("strict_dimension_threshold", 3.6)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 3.6

    @property
    def force_issue_revision_threshold(self) -> float:
        value = self._revision_policy.get("force_issue_revision_threshold", 3.2)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 3.2

    @property
    def max_revision_candidates_per_round(self) -> int:
        value = os.getenv("EVAL_MAX_REVISION_CANDIDATES_PER_ROUND") or self._revision_policy.get(
            "max_revision_candidates_per_round",
            3,
        )
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 3

    @property
    def max_total_revision_pages(self) -> int:
        value = os.getenv("EVAL_MAX_TOTAL_REVISION_PAGES") or self._revision_policy.get("max_total_revision_pages", 4)
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 4

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

    def evaluate_all(
        self,
        image_paths: list[str],
        outline: OutlinePlan,
        slide_indices: list[int] | None = None,
    ) -> list[SlideEvalResult]:
        """
        逐页视觉评分。image_paths 与 outline.slides 一一对应（长度可能不等，取 min）。
        返回 SlideEvalResult 列表。若单页评分失败，会回落为低分结果，避免 QA 静默跳过。
        """
        if not self.enabled:
            return []

        results = []
        n = min(len(image_paths), len(outline.slides))
        if slide_indices:
            selected_indices = sorted({int(i) for i in slide_indices if 0 <= int(i) < n})
        else:
            selected_indices = list(range(n))

        for i in selected_indices:
            img_path = image_paths[i]
            slide = outline.slides[i]
            if not img_path or not Path(img_path).exists():
                continue
            try:
                result = self._evaluate_slide(img_path, slide.slide_index, slide.topic, slide.layout.value)
                results.append(result)
                print(
                    f"[Evaluator] 第 {slide.slide_index} 页评分: "
                    f"layout={result.layout_score:.1f} content={result.content_score:.1f} "
                    f"design={result.design_score:.1f} overall={result.overall:.1f}"
                )
            except Exception as e:
                logger.warning(f"[Evaluator] 第 {slide.slide_index} 页评分失败: {e}")
                result = self._build_failed_result(slide.slide_index, str(e))
                results.append(result)
                print(
                    f"[Evaluator] 第 {slide.slide_index} 页评分失败，按低分处理: "
                    f"overall={result.overall:.1f}"
                )

        if results:
            avg = sum(r.overall for r in results) / len(results)
            low = [r for r in results if r.overall < config.EVAL_SCORE_THRESHOLD]
            if slide_indices:
                print(
                    f"[Evaluator] 增量评分完成（{len(results)} 页），平均分 {avg:.2f}，"
                    f"{len(low)} 页低于阈值 {config.EVAL_SCORE_THRESHOLD}"
                )
            else:
                print(f"[Evaluator] 评分完成，平均分 {avg:.2f}，{len(low)} 页低于阈值 {config.EVAL_SCORE_THRESHOLD}")

        return results

    def _evaluate_slide(
        self,
        image_path: str,
        slide_index: int,
        topic: str,
        layout: str,
    ) -> SlideEvalResult:
        """单页评分：base64 编码图片，发给 qwen-vl，解析 JSON 结果。"""
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")

        ext = Path(image_path).suffix.lower().lstrip(".")
        mime = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"

        system = self._system_template
        trigger_stage = "visual_evaluation"
        layout_scope = layout
        visual_mode_scope = "visual_qa"
        prompt_context = SkillContext(
            phase="evaluation-and-repair",
            trigger_stage=trigger_stage,
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
            provider=config.QWEN_VL_MODEL,
        )
        prevention_bundle = self._skill_runtime.build_prevention_bundle(
            context=prompt_context,
            heading="## 长期技能目录（视觉评估）",
            max_items=2,
        )
        self._record_prompt_bundle(
            stage=trigger_stage,
            mode="prevention",
            context=prompt_context,
            bundle=prevention_bundle,
        )

        user_text = (
            self._user_template
            .replace("{slide_index}", str(slide_index))
            .replace("{topic}", topic)
            .replace("{layout}", layout)
        )
        user_text = merge_prompt_sections(
            PromptSection(source_type="static_prompt", identifier="visual_evaluation:user", content=user_text),
            prevention_bundle,
        )

        last_error = ""
        last_error_signature: str | None = None
        raw = ""
        for attempt in range(1, MAX_RETRIES + 1):
            loaded_repair_memory_ids: list[str] = []
            current_user_text = user_text
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
                current_user_text = merge_prompt_sections(
                    current_user_text,
                    repair_bundle,
                    PromptSection(
                        source_type="repair_feedback",
                        identifier="visual_evaluation:retry_feedback",
                        content="\n".join(
                            self._repair_orchestrator.build_retry_feedback(
                                error=last_error,
                                error_signature=last_error_signature,
                                layout_scope=layout_scope,
                                visual_mode_scope=visual_mode_scope,
                            )
                        ),
                    ),
                )

            try:
                response = self.client.chat.completions.create(
                    model=config.QWEN_VL_MODEL,
                    max_tokens=640,
                    messages=[
                        {"role": "system", "content": system},
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                                },
                                {"type": "text", "text": current_user_text},
                            ],
                        },
                    ],
                )

                raw = response.choices[0].message.content
                record_usage(
                    component="ppt_visual_qa",
                    operation="evaluate_slide_image",
                    provider=config.QWEN_BASE_URL,
                    model=config.QWEN_VL_MODEL,
                    usage=getattr(response, "usage", None),
                    estimated_input_tokens=estimate_tokens_from_text(system + "\n" + current_user_text),
                    estimated_output_tokens=estimate_tokens_from_text(str(raw or "")),
                    image_count=1,
                    vl_image_count=1,
                    metadata={"attempt": attempt, "image_path": str(image_path)},
                )
                data = self._parse_json(raw)
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
                        provider_scope=config.QWEN_VL_MODEL,
                        before_pattern=raw[:400],
                        after_pattern=json.dumps(data, ensure_ascii=False)[:400],
                        conditions=[f"slide_index={slide_index}"],
                    )
                break
            except Exception as exc:
                for memory_id in dict.fromkeys(loaded_repair_memory_ids):
                    self._repair_orchestrator.mark_memory_failure(memory_id)
                last_error = str(exc)
                last_error_signature = self._repair_orchestrator.classify_error(
                    last_error,
                    stage=trigger_stage,
                )
                if attempt == MAX_RETRIES:
                    raise

        layout_score = self._coerce_score(data.get("layout_score"), 3.0)
        content_score = self._coerce_score(data.get("content_score"), 3.0)
        design_score = self._coerce_score(data.get("design_score"), 3.0)
        # Align the deck QA more closely with presentation-level judging:
        # content adequacy and design quality should weigh at least as much as raw layout neatness.
        overall = layout_score * 0.30 + content_score * 0.35 + design_score * 0.35
        issues = self._coerce_string_list(data.get("issues"))
        suggestions = self._coerce_string_list(data.get("suggestions"))
        layout_score, content_score, design_score, issues, suggestions = self._apply_rubric_adjustments(
            layout_score=layout_score,
            content_score=content_score,
            design_score=design_score,
            issues=issues,
            suggestions=suggestions,
        )
        overall = layout_score * 0.30 + content_score * 0.35 + design_score * 0.35

        return SlideEvalResult(
            slide_index=slide_index,
            layout_score=layout_score,
            content_score=content_score,
            design_score=design_score,
            overall=round(overall, 2),
            issues=issues,
            suggestions=suggestions,
        )

    def needs_revision(self, result: SlideEvalResult) -> bool:
        if result.overall < config.EVAL_SCORE_THRESHOLD:
            return True
        if self._strict_book_dimension_qa_enabled() and self._has_low_dimension_score(result):
            return True
        matched_groups = self._matched_severe_groups(result.issues)
        if not matched_groups:
            return False
        if any(bool(group.get("force_revision", False)) for group in matched_groups):
            return result.overall < self.force_issue_revision_threshold
        return False

    def revision_priority(self, result: SlideEvalResult) -> tuple[int, float, float, int]:
        matched_groups = self._matched_severe_groups(result.issues)
        force_revision = any(bool(group.get("force_revision", False)) for group in matched_groups)
        hard_fail = result.overall < config.EVAL_SCORE_THRESHOLD
        min_dimension = min(result.layout_score, result.content_score, result.design_score)
        strict_dimension_fail = self._strict_book_dimension_qa_enabled() and min_dimension < self.strict_dimension_threshold
        issue_count = len(result.issues or [])
        return (
            0 if hard_fail else 1 if strict_dimension_fail else 2 if force_revision else 3,
            float(result.overall),
            float(min_dimension),
            -issue_count,
        )

    def is_hard_fail(self, result: SlideEvalResult) -> bool:
        return result.overall < config.EVAL_SCORE_THRESHOLD

    def is_strict_dimension_fail(self, result: SlideEvalResult) -> bool:
        return self._strict_book_dimension_qa_enabled() and self._has_low_dimension_score(result)

    @staticmethod
    def _strict_book_ppt_qa_enabled() -> bool:
        return os.getenv("DIRECTIONAI_BOOK_PPT_STRICT_QA", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @staticmethod
    def _strict_book_dimension_qa_enabled() -> bool:
        return os.getenv("DIRECTIONAI_BOOK_PPT_DIMENSION_QA", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def _has_low_dimension_score(self, result: SlideEvalResult) -> bool:
        return min(result.layout_score, result.content_score, result.design_score) < self.strict_dimension_threshold

    def is_force_issue_fail(self, result: SlideEvalResult) -> bool:
        matched_groups = self._matched_severe_groups(result.issues)
        if not matched_groups:
            return False
        return any(bool(group.get("force_revision", False)) for group in matched_groups)

    def _has_severe_issue(self, issues: list[str]) -> bool:
        return bool(self._matched_severe_groups(issues))

    def _matched_severe_groups(self, issues: list[str]) -> list[dict]:
        lowered = " ".join(str(item).lower() for item in issues)
        matched: list[dict] = []
        for group in self._revision_policy.get("severe_issue_groups", []):
            for pattern in group.get("patterns", []):
                if str(pattern).lower() in lowered:
                    matched.append(group)
                    break
        return matched

    @property
    def soft_issue_revision_threshold(self) -> float:
        value = self._revision_policy.get("soft_issue_revision_threshold", 3.2)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 3.2

    def _apply_rubric_adjustments(
        self,
        *,
        layout_score: float,
        content_score: float,
        design_score: float,
        issues: list[str],
        suggestions: list[str],
    ) -> tuple[float, float, float, list[str], list[str]]:
        lowered = " ".join(str(item).lower() for item in issues)

        scores = {
            "layout_score": layout_score,
            "content_score": content_score,
            "design_score": design_score,
        }
        for group in self._revision_policy.get("severe_issue_groups", []):
            patterns = [str(item).lower() for item in group.get("patterns", [])]
            if not any(pattern in lowered for pattern in patterns):
                continue
            for score_name, cap in group.get("score_caps", {}).items():
                if score_name in scores:
                    try:
                        scores[score_name] = min(scores[score_name], float(cap))
                    except (TypeError, ValueError):
                        continue

        if self._strict_book_dimension_qa_enabled():
            low_dimensions = [
                label
                for score_name, label in (
                    ("layout_score", "布局"),
                    ("content_score", "内容"),
                    ("design_score", "设计"),
                )
                if scores[score_name] < self.strict_dimension_threshold
            ]
            if low_dimensions:
                dimension_text = "、".join(low_dimensions)
                issue = f"{dimension_text}单项低于教师课件交付阈值"
                suggestion = (
                    "优先修复低分单项：布局低分先重排阅读路径、对齐和留白；"
                    "内容低分先减少屏幕文字并补课堂任务；设计低分先统一层级、对比度和主视觉。"
                    "深色底上只能放白色或接近白色正文，浅色底上只能放深色正文，禁止低对比灰字、透明正文和文字压线。"
                )
                if issue not in issues:
                    issues.append(issue)
                if suggestion not in suggestions:
                    suggestions.append(suggestion)

        # Keep issues/suggestions compact but make sure hard failures have an action.
        if self._has_severe_issue(issues) and not suggestions:
            fallback = str(
                self._revision_policy.get(
                    "fallback_suggestion_for_severe_issue",
                    "优先修复主视觉失效、越界或不可读问题，再调整风格细节。",
                )
            ).strip()
            if fallback:
                suggestions = [fallback]

        return (
            round(scores["layout_score"], 2),
            round(scores["content_score"], 2),
            round(scores["design_score"], 2),
            issues,
            suggestions,
        )

    @staticmethod
    def _parse_json(raw: str) -> dict:
        cleaned = str(raw or "").strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

        candidates: list[str] = []
        if cleaned:
            candidates.append(cleaned)

        extracted = EvaluatorAgent._extract_outer_json_object(cleaned)
        if extracted and extracted not in candidates:
            candidates.append(extracted)

        sanitized = EvaluatorAgent._sanitize_json_strings(extracted or cleaned)
        if sanitized and sanitized not in candidates:
            candidates.append(sanitized)

        repaired = EvaluatorAgent._repair_missing_json_commas(sanitized)
        if repaired and repaired not in candidates:
            candidates.append(repaired)

        for candidate in candidates:
            try:
                data = json.loads(candidate, strict=False)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                continue

        partial = EvaluatorAgent._extract_partial_result(cleaned)
        if partial:
            return partial

        raise ValueError(f"无法解析评分 JSON，原始内容前200字：{cleaned[:200]}")

    @staticmethod
    def _extract_partial_result(text: str) -> dict:
        def parse_score(field: str):
            match = re.search(rf'"{field}"\s*:\s*(-?\d+(?:\.\d+)?)', text)
            return float(match.group(1)) if match else None

        def extract_array_items(field: str) -> list[str]:
            field_match = re.search(rf'"{field}"\s*:\s*\[', text)
            if not field_match:
                return []

            start = field_match.end()
            end_candidates = []
            next_field = re.search(r',?\s*"(?:suggestions|issues|layout_score|content_score|design_score)"\s*:', text[start:])
            if next_field:
                end_candidates.append(start + next_field.start())
            close_bracket = text.find("]", start)
            if close_bracket >= 0:
                end_candidates.append(close_bracket)
            segment = text[start:min(end_candidates)] if end_candidates else text[start:]

            items: list[str] = []
            i = 0
            while i < len(segment):
                if segment[i] != '"':
                    i += 1
                    continue
                j = i + 1
                escape = False
                buf: list[str] = []
                while j < len(segment):
                    ch = segment[j]
                    if escape:
                        buf.append(ch)
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == '"':
                        items.append("".join(buf))
                        i = j + 1
                        break
                    else:
                        buf.append(ch)
                    j += 1
                else:
                    break
            return [item.strip() for item in items if item.strip()]

        layout_score = parse_score("layout_score")
        content_score = parse_score("content_score")
        design_score = parse_score("design_score")

        if layout_score is None and content_score is None and design_score is None:
            return {}

        issues = extract_array_items("issues")
        suggestions = extract_array_items("suggestions")

        partial = {
            "layout_score": layout_score if layout_score is not None else 3.0,
            "content_score": content_score if content_score is not None else 3.0,
            "design_score": design_score if design_score is not None else 3.0,
            "issues": issues,
            "suggestions": suggestions,
        }
        return partial

    @staticmethod
    def _extract_outer_json_object(text: str) -> str:
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
                if EvaluatorAgent._is_probable_json_string_end(text, i):
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
                end = EvaluatorAgent._consume_json_string(text, i)
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
                end = EvaluatorAgent._consume_json_literal(text, i)
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
    def _is_probable_json_string_end(text: str, quote_index: int) -> bool:
        j = quote_index + 1
        while j < len(text) and text[j].isspace():
            j += 1
        return j >= len(text) or text[j] in "\",:}]"

    @staticmethod
    def _coerce_score(value, default: float) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            match = re.search(r"-?\d+(?:\.\d+)?", value)
            if match:
                return float(match.group(0))
        return float(default)

    @staticmethod
    def _coerce_string_list(value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()]

    def _build_failed_result(self, slide_index: int, error: str) -> SlideEvalResult:
        failed_issue_template = getattr(self, "_failed_issue_template", "视觉评分失败：{error_excerpt}")
        failed_suggestion_primary = getattr(
            self,
            "_failed_suggestion_primary_template",
            "重新检查本页排版与文本复杂度，避免过密内容和难以辨识的标注。",
        )
        failed_suggestion_secondary = getattr(
            self,
            "_failed_suggestion_secondary_template",
            "保留清晰标题层级，减少可能引发评分模型误判的复杂引号或杂乱说明。",
        )
        return SlideEvalResult(
            slide_index=slide_index,
            layout_score=1.5,
            content_score=1.5,
            design_score=1.5,
            overall=1.5,
            issues=[
                failed_issue_template.format(error_excerpt=error[:180]).strip()
            ],
            suggestions=[
                failed_suggestion_primary.strip(),
                failed_suggestion_secondary.strip(),
            ],
        )
