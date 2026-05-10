from __future__ import annotations

from pathlib import Path

from backend.harness.runtime.skill_runtime import SkillRuntime
from backend.harness.runtime.skill_loader import SkillLoader
from backend.tools.pptx_skill import skill_paths


class PromptComposer:
    def __init__(self, loader: SkillLoader | None = None):
        self.loader = loader or SkillLoader()
        self.runtime = SkillRuntime(self.loader)
        self._vendor_paths = skill_paths()

    def load_reference(self, skill_name: str, reference_name: str) -> str:
        return self.runtime.load_reference(skill_name, reference_name)

    def load_template(self, skill_name: str, template_name: str) -> str:
        return self.runtime.load_template(skill_name, template_name)

    def load_vendor_pptx_skill(self) -> str:
        return Path(self._vendor_paths["skill_md"]).read_text(encoding="utf-8")

    def load_vendor_pptxgenjs(self) -> str:
        return Path(self._vendor_paths["pptxgenjs_md"]).read_text(encoding="utf-8")

    def compose_local_visual_rules(self) -> str:
        theme = self.load_reference("visual-production", "theme_rules.md").strip()
        layout = self.load_reference("visual-production", "layout_rules.md").strip()
        slide = self.load_reference("visual-production", "slide_rules.md").strip()
        heuristics = self.load_reference("visual-production", "repair_patterns.md").strip()
        return self.load_template("visual-production", "local_visual_rules_wrapper.txt").format(
            theme_rules=theme,
            layout_rules=layout,
            slide_rules=slide,
            repair_patterns=heuristics,
        ).strip()

    def load_outline_system_prompt(self) -> str:
        return self.load_template("outline-planning", "outline_system_prompt.md")

    def load_deck_generation_user_prompt_template(self) -> str:
        return self.load_template("outline-planning", "deck_generation_user.txt")

    def load_research_synthesis_system_prompt(self) -> str:
        return self.load_template("research-synthesis", "research_synthesis_system.md")

    def load_research_synthesis_user_prompt_template(self) -> str:
        return self.load_template("research-synthesis", "research_synthesis_user.txt")

    def load_research_budgeting_system_prompt(self) -> str:
        return self.load_reference("research-synthesis", "research_budgeting_system.md")

    def load_research_budgeting_user_prompt_template(self) -> str:
        return self.load_template("research-synthesis", "research_budgeting_user.txt")

    def load_research_degraded_notice_template(self) -> str:
        return self.load_template("research-synthesis", "research_degraded_notice.txt")

    def load_research_budget_rebalance_system_prompt(self) -> str:
        return self.load_reference("research-synthesis", "research_budget_rebalance_system.md")

    def load_research_budget_rebalance_user_prompt_template(self) -> str:
        return self.load_template("research-synthesis", "research_budget_rebalance_user.txt")

    def load_visual_evaluation_system_prompt(self) -> str:
        return self.load_template("evaluation-and-repair", "visual_evaluation_system.md")

    def load_visual_evaluation_revision_policy(self) -> str:
        return self.load_reference("evaluation-and-repair", "visual_revision_policy.json")

    def load_visual_evaluation_user_prompt_template(self) -> str:
        return self.load_template("evaluation-and-repair", "visual_evaluation_user.txt")

    def load_visual_evaluation_failed_issue_template(self) -> str:
        return self.load_template("evaluation-and-repair", "visual_evaluation_failed_issue.txt")

    def load_visual_evaluation_failed_suggestion_primary_template(self) -> str:
        return self.load_template("evaluation-and-repair", "visual_evaluation_failed_suggestion_primary.txt")

    def load_visual_evaluation_failed_suggestion_secondary_template(self) -> str:
        return self.load_template("evaluation-and-repair", "visual_evaluation_failed_suggestion_secondary.txt")

    def load_content_evaluation_system_prompt(self) -> str:
        return self.load_template("evaluation-and-repair", "content_evaluation_system.md")

    def load_content_coherence_policy(self) -> str:
        return self.load_reference("evaluation-and-repair", "content_coherence_policy.json")

    def load_content_evaluation_user_prompt_template(self) -> str:
        return self.load_template("evaluation-and-repair", "content_evaluation_user.txt")

    def load_content_evaluation_retry_feedback_template(self) -> str:
        return self.load_template("evaluation-and-repair", "content_evaluation_retry_feedback.txt")

    def load_content_evaluation_metric_line_template(self) -> str:
        return self.load_template("evaluation-and-repair", "content_evaluation_metric_line.txt")

    def load_content_evaluation_metric_levels_template(self) -> str:
        return self.load_template("evaluation-and-repair", "content_evaluation_metric_levels.txt")

    def load_content_evaluation_default_reason_template(self) -> str:
        return self.load_template("evaluation-and-repair", "content_evaluation_default_reason.txt")

    def load_content_evaluation_default_suggestion_template(self) -> str:
        return self.load_template("evaluation-and-repair", "content_evaluation_default_suggestion.txt")

    def load_content_evaluation_stream_reading_template(self) -> str:
        return self.load_template("evaluation-and-repair", "content_evaluation_stream_reading.txt")

    def load_content_evaluation_stream_scoring_template(self) -> str:
        return self.load_template("evaluation-and-repair", "content_evaluation_stream_scoring.txt")

    def load_content_evaluation_stream_reporting_template(self) -> str:
        return self.load_template("evaluation-and-repair", "content_evaluation_stream_reporting.txt")

    def load_content_evaluation_progress_scoring_template(self) -> str:
        return self.load_template("evaluation-and-repair", "content_evaluation_progress_scoring.txt")

    def load_content_evaluation_progress_reporting_template(self) -> str:
        return self.load_template("evaluation-and-repair", "content_evaluation_progress_reporting.txt")

    def load_content_evaluation_progress_done_template(self) -> str:
        return self.load_template("evaluation-and-repair", "content_evaluation_progress_done.txt")

    def load_content_evaluation_progress_start_template(self) -> str:
        return self.load_template("evaluation-and-repair", "content_evaluation_progress_start.txt")

    def load_deck_coherence_review_system_prompt(self) -> str:
        return self.load_template("evaluation-and-repair", "deck_coherence_review_system.md")

    def load_deck_coherence_review_user_prompt_template(self) -> str:
        return self.load_template("evaluation-and-repair", "deck_coherence_review_user.txt")

    def load_theme_decision_system_prompt(self) -> str:
        return self.load_template("visual-production", "theme_decision_system.md")

    def load_theme_decision_user_prompt_template(self) -> str:
        return self.load_template("visual-production", "theme_decision_user.txt")

    def load_theme_decision_retry_prompt_template(self) -> str:
        return self.load_template("visual-production", "theme_decision_retry.txt")

    def load_theme_style_auto_template(self) -> str:
        return self.load_template("visual-production", "theme_style_auto.txt")

    def load_theme_style_preference_template(self) -> str:
        return self.load_template("visual-production", "theme_style_preference.txt")

    def load_image_prompt_enrichment_system_prompt(self) -> str:
        return self.load_template("visual-production", "image_prompt_enrichment_system.md")

    def load_image_prompt_enrichment_user_prompt_template(self) -> str:
        return self.load_template("visual-production", "image_prompt_enrichment_user.txt")

    def load_deck_generation_system_template(self) -> str:
        return self.load_template("visual-production", "deck_generation_system.md")

    def load_slide_generation_system_template(self) -> str:
        return self.load_template("visual-production", "slide_generation_system.md")

    def load_slide_generation_user_prompt_template(self) -> str:
        return self.load_template("visual-production", "slide_user.txt")

    def load_theme_section_template(self) -> str:
        return self.load_template("visual-production", "theme_section.txt")

    def load_page_info_section_template(self) -> str:
        return self.load_template("visual-production", "page_info_section.txt")

    def load_outline_planning_user_prompt_template(self) -> str:
        return self.load_template("outline-planning", "outline_planning_user.txt")

    def load_audience_profile_template(self) -> str:
        return self.load_template("outline-planning", "audience_profile.txt")

    def load_outline_default_style_text_template(self) -> str:
        return self.load_template("outline-planning", "default_style_text.txt")

    def load_outline_extra_requirements_template(self) -> str:
        return self.load_template("outline-planning", "extra_requirements.txt")

    def load_consistency_brief_template(self) -> str:
        return self.load_template("visual-production", "consistency_brief.txt")

    def load_layout_intent_template(self) -> str:
        return self.load_template("visual-production", "layout_intent.txt")

    def load_document_summary_user_prompt_template(self) -> str:
        return self.load_template("document-understanding", "document_summary_user.txt")

    def load_document_summary_retry_feedback_template(self) -> str:
        return self.load_template("document-understanding", "document_summary_retry_feedback.txt")

    def load_document_summary_truncation_notice_template(self) -> str:
        return self.load_template("document-understanding", "document_summary_truncation_notice.txt")

    def load_document_summary_system_wrapper_template(self) -> str:
        return self.load_template("document-understanding", "document_summary_system_wrapper.txt")

    def load_document_planner_context_header_template(self) -> str:
        return self.load_template("document-understanding", "planner_context_header.txt")

    def load_document_planner_context_section_template(self) -> str:
        return self.load_template("document-understanding", "planner_context_section.txt")

    def load_document_planner_context_table_template(self) -> str:
        return self.load_template("document-understanding", "planner_context_table.txt")

    def load_document_planner_context_hints_template(self) -> str:
        return self.load_template("document-understanding", "planner_context_hints.txt")

    def load_document_summary_tables_header_template(self) -> str:
        return self.load_template("document-understanding", "document_summary_tables_header.txt")

    def load_document_summary_table_heading_template(self) -> str:
        return self.load_template("document-understanding", "document_summary_table_heading.txt")

    def load_document_summary_table_truncation_template(self) -> str:
        return self.load_template("document-understanding", "document_summary_table_truncation.txt")

    def load_document_summary_source_file_line_template(self) -> str:
        return self.load_template("document-understanding", "document_summary_source_file_line.txt")

    def load_document_summary_page_count_line_template(self) -> str:
        return self.load_template("document-understanding", "document_summary_page_count_line.txt")

    def load_image_generation_prompt_template(self) -> str:
        return self.load_template("visual-production", "image_generation_prompt.txt")

    def load_outline_context_template(self) -> str:
        return self.load_template("visual-production", "outline_context.txt")

    def load_research_context_template(self) -> str:
        return self.load_template("visual-production", "research_context.txt")

    def load_image_context_template(self) -> str:
        return self.load_template("visual-production", "image_context.txt")

    def load_default_outline_context_template(self) -> str:
        return self.load_template("visual-production", "default_outline_context.txt")

    def load_default_research_context_template(self) -> str:
        return self.load_template("visual-production", "default_research_context.txt")

    def load_default_image_context_template(self) -> str:
        return self.load_template("visual-production", "default_image_context.txt")

    def load_revision_issue_line_template(self) -> str:
        return self.load_template("visual-production", "revision_issue_line.txt")

    def load_revision_suggestion_line_template(self) -> str:
        return self.load_template("visual-production", "revision_suggestion_line.txt")

    def load_shape_parameter_fix_template(self) -> str:
        return self.load_template("visual-production", "shape_parameter_fix.txt")

    def load_no_image_addimage_error_template(self) -> str:
        return self.load_template("visual-production", "no_image_addimage_error.txt")

    def load_no_image_resource_error_template(self) -> str:
        return self.load_template("visual-production", "no_image_resource_error.txt")

    def load_illegal_image_reference_error_template(self) -> str:
        return self.load_template("visual-production", "illegal_image_reference_error.txt")

    def load_unauthorized_image_path_error_template(self) -> str:
        return self.load_template("visual-production", "unauthorized_image_path_error.txt")
