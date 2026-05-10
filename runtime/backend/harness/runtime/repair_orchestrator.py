from __future__ import annotations

import json
import re
import uuid
from typing import Any

from backend.harness.runtime.runtime_memory import RepairMemoryRecord
from backend.harness.runtime.skill_runtime import SkillRuntime


class RepairOrchestrator:
    PHASE = "visual-production"

    def __init__(
        self,
        skill_runtime: SkillRuntime,
        run_id: str | None = None,
        phase: str | None = None,
    ) -> None:
        self.skill_runtime = skill_runtime
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self.phase = phase or self.PHASE
        self._repair_instruction_map = self._load_repair_instruction_map()

    def classify_error(self, error: str, *, stage: str, image_path: str | None = None) -> str:
        lowered = str(error or "").lower()

        if "无法解析 json" in lowered:
            if stage == "image_prompt_enrichment":
                return "image_prompt_enrichment_json_truncated"
            if stage == "research_synthesis":
                return "research_json_truncated"
            if stage == "content_evaluation":
                return "content_evaluation_json_truncated"
            if stage == "visual_evaluation":
                return "visual_evaluation_json_truncated"
            if stage == "document_summary":
                return "document_summary_json_truncated"
            return "outline_json_truncated"
        if stage == "image_prompt_enrichment" and ("期望 json 数组" in error or "expecting value" in lowered):
            return "image_prompt_enrichment_json_truncated"
        if stage == "research_synthesis":
            if "缺少 bullet_points" in error:
                return "research_missing_bullet_points"
            if "expecting value" in lowered or "line 1 column" in lowered:
                return "research_json_truncated"
        if stage == "content_evaluation":
            if "expecting value" in lowered or "line 1 column" in lowered:
                return "content_evaluation_json_truncated"
            if "detailed_scores" in lowered:
                return "content_evaluation_missing_scores"
        if stage == "visual_evaluation" and ("expecting value" in lowered or "line 1 column" in lowered):
            return "visual_evaluation_json_truncated"
        if stage == "document_summary":
            if "validation error" in lowered:
                return "document_summary_schema_invalid"
            if "expecting value" in lowered or "line 1 column" in lowered:
                return "document_summary_json_truncated"
        if "validation error for outlineplan" in lowered and "layout" in lowered:
            return "outline_missing_layout"
        if "页级大纲规划失败" in error or "第 0 页必须是 cover" in error or "最后一页必须是 closing" in error:
            return "outline_structure_invalid"
        if stage == "asset_generation":
            if "缺少 image_urls" in error:
                return "minimax_missing_image_urls"
            if "缺少 image_base64" in error:
                return "minimax_missing_image_base64"
            if "base64 解码失败" in error:
                return "minimax_invalid_base64"
            if "生图失败" in error:
                return "image_generation_provider_error"
        if stage in {"asset_search", "research_search"} and ("检索" in error or "quota" in lowered or "429" in lowered):
            return "asset_search_backend_failed"
        if "未找到 <code>" in error:
            return "missing_code_block"
        if "代码块被截断" in error or "缺少 </code>" in error or "缺少 ```" in error:
            return "slide_code_truncated"
        if "无图片模式下禁止使用 addimage" in error or "forbidden_addimage_without_asset" in lowered:
            return "forbidden_addimage_without_asset"
        if "非法图片引用" in error or "remote" in lowered and "image" in lowered:
            return "forbidden_remote_image"
        if "未授权图片路径" in error:
            return "unauthorized_image_path"
        if "起始坐标越过页面左上边界" in error or "坐标超出页面左上边界" in error or "negative_origin" in lowered:
            return "geometry_negative_origin"
        if "缺少几何参数" in error or "missing geometry" in lowered:
            return "geometry_missing_coordinate"
        if "动态几何参数" in error or "数字字面量" in error or "dynamic geometry" in lowered:
            return "geometry_dynamic_coordinate"
        if "使用了非正尺寸" in error or "非正尺寸" in error or "non_positive_size" in lowered:
            return "geometry_non_positive_size"
        if "超出页面边界" in error or "overflow" in lowered and ("slide" in lowered or "页面" in error):
            return "geometry_overflow"
        if "shape parameter" in lowered or "addshape" in lowered:
            return "invalid_shape_parameter"
        if "syntaxerror" in lowered:
            if "unexpected end of input" in lowered or "missing )" in lowered:
                return "slide_code_truncated"
            if "unexpected identifier" in lowered or "unexpected token" in lowered:
                return "js_syntax_quote_or_token"
            return "js_syntax_generic"
        if "视觉评分失败" in error or stage == "visual_qa":
            return "visual_low_score_layout"
        if "内容页文字过少" in error or "页面标题可能缺失" in error:
            return "content_layout_underfilled"
        if image_path is None and "addimage" in lowered:
            return "forbidden_addimage_without_asset"
        return "generic_retry"

    def build_repair_instruction(
        self,
        *,
        error_signature: str,
        error: str,
        layout_scope: str,
        visual_mode_scope: str,
    ) -> str:
        base = self._repair_instruction_map.get(
            error_signature,
            self._repair_instruction_map["generic_retry"],
        )
        excerpt = (error or "").strip()
        template = self.skill_runtime.shared_text(
            "repair_instruction_wrapper.txt",
            "{base_instruction} 当前 layout={layout_scope}；当前 visual_mode={visual_mode_scope}{error_excerpt_clause}。",
        )
        return template.format(
            base_instruction=base,
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
            error_excerpt_clause=f"；错误摘录：{excerpt[:180]}" if excerpt else "",
        )

    def _load_repair_instruction_map(self) -> dict[str, str]:
        try:
            raw = self.skill_runtime.load_reference(
                self.phase,
                "repair_instruction_map.json",
            )
        except FileNotFoundError:
            raw = self.skill_runtime.load_reference(
                "evaluation-and-repair",
                "repair_instruction_map.json",
            )
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("repair_instruction_map.json 必须是对象")
        mapping = {str(key): str(value) for key, value in data.items()}
        if "generic_retry" not in mapping:
            raise ValueError("repair_instruction_map.json 缺少 generic_retry")
        return mapping

    def build_retry_feedback(
        self,
        *,
        error: str,
        error_signature: str,
        layout_scope: str,
        visual_mode_scope: str,
    ) -> list[str]:
        return [
            error[:500],
            self.build_repair_instruction(
                error_signature=error_signature,
                error=error,
                layout_scope=layout_scope,
                visual_mode_scope=visual_mode_scope,
            ),
        ]

    def prevention_section(
        self,
        *,
        trigger_stage: str,
        layout_scope: str,
        visual_mode_scope: str,
        max_items: int | None = None,
    ) -> str:
        records = self.prevention_matches(
            trigger_stage=trigger_stage,
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
            max_items=max_items,
        )
        return self.render_records(
            records,
            heading=self.skill_runtime.shared_heading(
                "runtime_history_prevention_heading.txt",
                "## 历史修复经验（预防）",
            ),
        )

    def prevention_matches(
        self,
        *,
        trigger_stage: str,
        layout_scope: str,
        visual_mode_scope: str,
        max_items: int | None = None,
    ) -> list[RepairMemoryRecord]:
        return self.skill_runtime.match_runtime_memories(
            phase=self.phase,
            trigger_stage=trigger_stage,
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
            max_items=max_items or self.skill_runtime.PREVENTION_MAX_ITEMS,
        )

    def repair_section(
        self,
        *,
        trigger_stage: str,
        error_signature: str,
        layout_scope: str,
        visual_mode_scope: str,
        max_items: int | None = None,
    ) -> str:
        records = self.repair_matches(
            trigger_stage=trigger_stage,
            error_signature=error_signature,
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
            max_items=max_items,
        )
        return self.render_records(
            records,
            heading=self.skill_runtime.shared_heading(
                "runtime_history_repair_heading.txt",
                "## 历史修复经验（强匹配）",
            ),
        )

    def repair_matches(
        self,
        *,
        trigger_stage: str,
        error_signature: str,
        layout_scope: str,
        visual_mode_scope: str,
        max_items: int | None = None,
    ) -> list[RepairMemoryRecord]:
        return self.skill_runtime.match_runtime_memories(
            phase=self.phase,
            trigger_stage=trigger_stage,
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
            error_signature=error_signature,
            max_items=max_items or self.skill_runtime.REPAIR_MAX_ITEMS,
        )

    def remember_success(
        self,
        *,
        trigger_stage: str,
        error_signature: str,
        error: str,
        repair_instruction: str,
        layout_scope: str,
        visual_mode_scope: str,
        audience_scope: str = "*",
        course_type_scope: str = "*",
        provider_scope: str = "*",
        language_scope: str = "*",
        before_pattern: str = "",
        after_pattern: str = "",
        conditions: list[str] | None = None,
    ) -> None:
        self.skill_runtime.remember_runtime_success(
            phase=self.phase,
            trigger_stage=trigger_stage,
            error_signature=error_signature,
            error_excerpt=error[:500],
            repair_instruction=repair_instruction,
            source_run_id=self.run_id,
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
            audience_scope=audience_scope,
            course_type_scope=course_type_scope,
            provider_scope=provider_scope,
            language_scope=language_scope,
            before_pattern=before_pattern,
            after_pattern=after_pattern,
            conditions=conditions,
        )

    def mark_memory_failure(self, memory_id: str) -> None:
        self.skill_runtime.remember_runtime_failure(
            phase=self.phase,
            memory_id=memory_id,
        )

    @staticmethod
    def render_records(records: list[RepairMemoryRecord], *, heading: str) -> str:
        if not records:
            return ""

        lines = [heading]
        for item in records:
            suffix_parts = SkillRuntime._scope_parts(item.layout_scope, item.visual_mode_scope)
            condition_hint = SkillRuntime._condition_hint(item.conditions)
            if condition_hint:
                suffix_parts.append(condition_hint)
            scope_text = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
            lines.append(f"- {SkillRuntime._compact_text(item.repair_instruction)}{scope_text}")
        return "\n".join(lines)

    @staticmethod
    def extract_error_excerpt(stderr: str) -> str:
        lines = [line.strip() for line in str(stderr or "").splitlines() if line.strip()]
        if not lines:
            return ""
        return re.sub(r"\s+", " ", " | ".join(lines[:3]))[:240]
