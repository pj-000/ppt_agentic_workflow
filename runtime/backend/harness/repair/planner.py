from __future__ import annotations

import logging
from typing import Any

from backend.harness.memory.integration import memory_hit_to_trace_payload
from backend.harness.memory.models import MemoryQuery, MemoryType
from backend.harness.memory.namespace import REPAIR_CONTENT, REPAIR_TOOL, REPAIR_VISUAL
from backend.harness.repair.models import (
    RepairAction,
    RepairActionType,
    RepairIssue,
    RepairPlan,
    RepairScope,
    RepairSeverity,
    stable_repair_id,
    utc_now_iso,
)
from backend.harness.repair.policies import RepairPolicy
from backend.harness.repair.safety import sanitize_repair_mapping, sanitize_repair_text

logger = logging.getLogger(__name__)


class RepairPlanner:
    def __init__(
        self,
        *,
        policy: RepairPolicy | None = None,
        memory: Any | None = None,
        legacy_repair: Any | None = None,
        trace: Any | None = None,
    ):
        self.policy = policy or RepairPolicy()
        self.memory = memory
        self.legacy_repair = legacy_repair
        self.trace = trace

    def plan(
        self,
        *,
        run_id: str,
        issues: list[RepairIssue],
        context: dict[str, Any] | None = None,
    ) -> RepairPlan:
        plan_id = stable_repair_id("plan", run_id, len(issues), ",".join(issue.issue_id for issue in issues))
        self._record(
            "repair.started",
            {"run_id": run_id, "plan_id": plan_id, "issue_count": len(issues), "action_count": 0, "status": "started"},
        )
        memory_hits: list[dict[str, Any]] = []
        actions: list[RepairAction] = []

        for issue in issues:
            if len(actions) >= self.policy.max_total_actions:
                break
            hits = self._memory_hits(issue)
            memory_hits.extend(hits)
            issue_actions = self._actions_for_issue(issue, hits=hits, context=context or {})
            for action in issue_actions[: max(self.policy.max_actions_per_issue, 0)]:
                if len(actions) >= self.policy.max_total_actions:
                    break
                actions.append(action)

        status = "empty" if not issues else "planned" if actions else "skipped"
        plan = RepairPlan(
            plan_id=plan_id,
            run_id=run_id,
            status=status,
            issues=issues,
            actions=actions,
            memory_hits=memory_hits,
            prevention_summary=self._legacy_prevention_summary(issues),
            repair_summary=self._legacy_repair_summary(issues),
            created_at=utc_now_iso(),
            metadata=sanitize_repair_mapping({"context": context or {}, "policy": self.policy.model_dump(mode="json")}),
        )
        self._record(
            "repair.finished",
            {
                "run_id": run_id,
                "plan_id": plan_id,
                "issue_count": len(issues),
                "action_count": len(actions),
                "status": plan.status,
            },
        )
        return plan

    def _actions_for_issue(
        self,
        issue: RepairIssue,
        *,
        hits: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> list[RepairAction]:
        del context
        action_type, scope, target_tool, risk_level, instruction, expected = _default_action(issue, self.policy)
        skip_reason = ""
        if not _allowed_by_policy(action_type, self.policy):
            action_type = RepairActionType.MANUAL_REVIEW if self.policy.allow_manual_review else RepairActionType.NO_OP
            scope = issue.scope
            target_tool = issue.tool_name
            risk_level = "low"
            instruction = "Review this issue manually; configured policy disabled the automatic action."
            expected = "Human review decides the next safe repair step."
            skip_reason = "policy_disabled_action"

        legacy_instruction = self._legacy_instruction(issue)
        if legacy_instruction:
            instruction = f"{instruction}\n{legacy_instruction}" if instruction else legacy_instruction
        if hits:
            refs = ", ".join(hit["memory_id"] for hit in hits if hit.get("memory_id"))
            memory_refs = [str(hit["memory_id"]) for hit in hits if hit.get("memory_id")]
        else:
            refs = ""
            memory_refs = []

        auto_execute = True
        if self.policy.low_risk_only and action_type.value in set(self.policy.high_risk_action_types):
            auto_execute = False
            skip_reason = skip_reason or "high_risk_action"
        if _is_dependency_missing(issue):
            auto_execute = False
            skip_reason = "environment_dependency_missing"
        if issue.issue_type == "missing_metric" and issue.severity == RepairSeverity.INFO:
            auto_execute = False
            skip_reason = "informational_missing_metric"
        action_id = stable_repair_id("action", issue.issue_id, action_type.value, issue.slide_index, issue.tool_name)
        return [
            RepairAction(
                action_id=action_id,
                issue_id=issue.issue_id,
                action_type=action_type,
                scope=scope,
                target_slide_index=issue.slide_index,
                target_tool=target_tool,
                instruction=instruction,
                memory_refs=memory_refs,
                risk_level=risk_level,
                expected_effect=expected,
                metadata={
                    "auto_execute": auto_execute,
                    "memory_ref_summary": refs,
                    "source_issue_type": issue.issue_type,
                    "skip_reason": skip_reason,
                },
            )
        ]

    def _memory_hits(self, issue: RepairIssue) -> list[dict[str, Any]]:
        if not self.memory:
            return []
        namespace = _memory_namespace(issue)
        query_text = issue.error_signature or issue.issue_type or issue.message
        try:
            hits = self.memory.query(
                MemoryQuery(
                    namespace=namespace,
                    query=query_text,
                    memory_type=MemoryType.PROCEDURAL,
                    top_k=3,
                )
            )
        except Exception as exc:
            logger.warning("[Repair] Memory query failed; continuing: %s", exc)
            return []
        return [memory_hit_to_trace_payload(hit) for hit in hits]

    def _legacy_instruction(self, issue: RepairIssue) -> str:
        if not self.legacy_repair:
            return ""
        method = getattr(self.legacy_repair, "build_repair_instruction", None)
        if not callable(method):
            return ""
        try:
            return sanitize_repair_text(
                method(
                    error_signature=issue.error_signature or issue.issue_type or "generic_retry",
                    error=issue.message,
                    layout_scope="*",
                    visual_mode_scope="*",
                ),
                limit=1000,
            )
        except Exception as exc:
            logger.warning("[Repair] Legacy repair instruction failed; continuing: %s", exc)
            return ""

    def _legacy_prevention_summary(self, issues: list[RepairIssue]) -> str:
        if not self.legacy_repair or not issues:
            return ""
        method = getattr(self.legacy_repair, "prevention_section", None)
        if not callable(method):
            return ""
        try:
            return sanitize_repair_text(
                method(trigger_stage=issues[0].trigger_stage or "repair", layout_scope="*", visual_mode_scope="*", max_items=3),
                limit=1200,
            )
        except Exception as exc:
            logger.warning("[Repair] Legacy prevention summary failed; continuing: %s", exc)
            return ""

    def _legacy_repair_summary(self, issues: list[RepairIssue]) -> str:
        if not self.legacy_repair or not issues:
            return ""
        method = getattr(self.legacy_repair, "repair_section", None)
        if not callable(method):
            return ""
        first = issues[0]
        try:
            return sanitize_repair_text(
                method(
                    trigger_stage=first.trigger_stage or "repair",
                    error_signature=first.error_signature or first.issue_type or "generic_retry",
                    layout_scope="*",
                    visual_mode_scope="*",
                    max_items=3,
                ),
                limit=1200,
            )
        except Exception as exc:
            logger.warning("[Repair] Legacy repair summary failed; continuing: %s", exc)
            return ""

    def _record(self, stage: str, payload: dict[str, Any]) -> None:
        if not self.trace:
            return
        record = getattr(self.trace, "record", None)
        if not callable(record):
            return
        try:
            record(stage=stage, payload=sanitize_repair_mapping(payload))
        except Exception as exc:
            logger.warning("[Repair] Trace recording failed; continuing: %s", exc)


def _default_action(
    issue: RepairIssue,
    policy: RepairPolicy,
) -> tuple[RepairActionType, RepairScope, str | None, str, str, str]:
    text = " ".join(str(value or "") for value in (issue.issue_type, issue.error_signature, issue.message)).lower()
    if issue.issue_type == "missing_metric" and issue.severity == RepairSeverity.INFO:
        return (
            RepairActionType.NO_OP,
            issue.scope,
            issue.tool_name,
            "low",
            "Missing metric is informational; no automatic repair action is required.",
            "Keep missing metric visible without triggering misleading repair.",
        )
    if "dependencymissing" in text or "dependency missing" in text:
        return (
            RepairActionType.MANUAL_REVIEW,
            RepairScope.TOOL,
            issue.tool_name or "ppt.render_preview",
            "low",
            "Preview dependency is missing; install the dependency or skip visual QA in this environment.",
            "Avoid treating environment capability gaps as slide content failures.",
        )
    if "pptx_missing" in text or "pptxartifactmissing" in text or "ppt.run_pptxgenjs" in text:
        return (
            RepairActionType.RETRY_TOOL,
            RepairScope.TOOL,
            issue.tool_name or "ppt.run_pptxgenjs",
            "medium",
            "Retry PPTX generation after checking slide code and output artifact path.",
            "Recover missing PPTX artifact.",
        )
    if "preview" in text or "render_preview" in text:
        return (
            RepairActionType.RERENDER_PREVIEW,
            RepairScope.TOOL,
            issue.tool_name or "ppt.render_preview",
            "low",
            "Rerender slide preview and capture renderer diagnostics if it still fails.",
            "Restore preview artifacts for visual QA.",
        )
    if "content" in text or issue.scope == RepairScope.CONTENT:
        return (
            RepairActionType.CONTENT_REWRITE,
            RepairScope.CONTENT,
            None,
            "high",
            "Rewrite concise slide content to address content QA findings.",
            "Reduce content issues.",
        )
    if "asset" in text or "image" in text or issue.scope == RepairScope.ASSET:
        action_type = RepairActionType.FALLBACK_NO_IMAGE if policy.allow_disable_images else RepairActionType.MANUAL_REVIEW
        return (
            action_type,
            RepairScope.ASSET,
            issue.tool_name,
            "low",
            "Fallback to no-image layout or disable optional images for this slide.",
            "Keep deck generation usable when asset acquisition fails.",
        )
    if "visual" in text or issue.scope == RepairScope.VISUAL:
        return (
            RepairActionType.ADJUST_LAYOUT,
            RepairScope.VISUAL,
            None,
            "low",
            "Adjust layout density, contrast, alignment, and spacing for the affected slide.",
            "Improve visual score without changing core content.",
        )
    return (
        RepairActionType.MANUAL_REVIEW,
        issue.scope,
        issue.tool_name,
        "low",
        "Review the issue manually and decide the least risky repair action.",
        "Avoid unsafe automatic edits for unknown issues.",
    )


def _allowed_by_policy(action_type: RepairActionType, policy: RepairPolicy) -> bool:
    if action_type in {RepairActionType.RETRY_TOOL, RepairActionType.RERENDER_PREVIEW, RepairActionType.RESEARCH_RETRY}:
        return policy.allow_tool_retry
    if action_type in {RepairActionType.REGENERATE_SLIDE, RepairActionType.REVISE_SLIDE_CODE, RepairActionType.ADJUST_LAYOUT}:
        return policy.allow_slide_regeneration
    if action_type == RepairActionType.CONTENT_REWRITE:
        return policy.allow_content_rewrite
    if action_type in {RepairActionType.DISABLE_IMAGES, RepairActionType.FALLBACK_NO_IMAGE}:
        return policy.allow_disable_images
    if action_type == RepairActionType.MANUAL_REVIEW:
        return policy.allow_manual_review
    return True


def _memory_namespace(issue: RepairIssue) -> str:
    if issue.scope == RepairScope.CONTENT:
        return REPAIR_CONTENT
    if issue.scope == RepairScope.VISUAL:
        return REPAIR_VISUAL
    return REPAIR_TOOL


def _is_dependency_missing(issue: RepairIssue) -> bool:
    text = " ".join(str(value or "") for value in (issue.error_signature, issue.message, issue.issue_type)).lower()
    return any(
        marker in text
        for marker in (
            "dependencymissing",
            "dependency missing",
            "soffice_not_found",
            "pdftoppm_not_found",
        )
    )
