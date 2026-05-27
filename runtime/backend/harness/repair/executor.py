from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from backend.harness.repair.models import (
    RepairAction,
    RepairActionType,
    RepairAttempt,
    RepairPlan,
    RepairResult,
    stable_repair_id,
    utc_now_iso,
)
from backend.harness.repair.policies import RepairPolicy
from backend.harness.repair.safety import sanitize_repair_mapping, sanitize_repair_text

logger = logging.getLogger(__name__)

RepairActionHandler = Callable[[RepairAction], RepairAttempt]


class RepairExecutor:
    def __init__(
        self,
        handlers: dict[RepairActionType, RepairActionHandler] | None = None,
        trace: Any | None = None,
        policy: RepairPolicy | None = None,
    ):
        self.handlers = handlers or {}
        self.trace = trace
        self.policy = policy or RepairPolicy()

    def execute_plan(self, plan: RepairPlan) -> RepairResult:
        self._record(
            "repair.started",
            {
                "run_id": plan.run_id,
                "plan_id": plan.plan_id,
                "issue_count": len(plan.issues),
                "action_count": len(plan.actions),
                "status": "started",
            },
        )
        attempts = [self._execute_action(plan, action) for action in plan.actions]
        status = _result_status(attempts)
        summary = _attempt_summary(attempts)
        all_issue_ids = {issue.issue_id for issue in plan.issues}
        attempt_issue_ids = {attempt.issue_id for attempt in attempts}
        success_issue_ids = {attempt.issue_id for attempt in attempts if attempt.status == "success"}
        resolved = sorted(success_issue_ids)
        unresolved = sorted((all_issue_ids | attempt_issue_ids) - success_issue_ids)
        result = RepairResult(
            run_id=plan.run_id,
            plan_id=plan.plan_id,
            status=status,
            attempts=attempts,
            resolved_issue_ids=resolved,
            unresolved_issue_ids=unresolved,
            metadata={"issue_count": len(plan.issues), "action_count": len(plan.actions), **summary},
        )
        self._record(
            "repair.finished",
            {
                "run_id": plan.run_id,
                "plan_id": plan.plan_id,
                "issue_count": len(plan.issues),
                "action_count": len(plan.actions),
                "status": result.status,
                "resolved_issue_count": len(resolved),
                "unresolved_issue_count": len(unresolved),
                **summary,
            },
        )
        return result

    def _execute_action(self, plan: RepairPlan, action: RepairAction) -> RepairAttempt:
        started = utc_now_iso()
        if action.metadata.get("auto_execute") is False:
            return RepairAttempt(
                attempt_id=stable_repair_id("attempt", plan.plan_id, action.action_id, "auto_execute_false"),
                plan_id=plan.plan_id,
                action_id=action.action_id,
                issue_id=action.issue_id,
                run_id=plan.run_id,
                status="skipped",
                started_at=started,
                finished_at=utc_now_iso(),
                message="Action marked as non-auto-executable by repair policy",
                metrics={"skip_reason": "auto_execute_false"},
            )
        handler = self.handlers.get(action.action_type)
        if handler is None:
            return RepairAttempt(
                attempt_id=stable_repair_id("attempt", plan.plan_id, action.action_id, "skipped"),
                plan_id=plan.plan_id,
                action_id=action.action_id,
                issue_id=action.issue_id,
                run_id=plan.run_id,
                status="skipped",
                started_at=started,
                finished_at=utc_now_iso(),
                message="No handler registered for action type",
                metrics={"skip_reason": "no_handler"},
            )
        try:
            attempt = handler(action)
            return attempt.model_copy(
                update={
                    "attempt_id": attempt.attempt_id or stable_repair_id("attempt", plan.plan_id, action.action_id),
                    "plan_id": plan.plan_id,
                    "action_id": action.action_id,
                    "issue_id": action.issue_id,
                    "run_id": plan.run_id,
                    "started_at": attempt.started_at or started,
                    "finished_at": attempt.finished_at or utc_now_iso(),
                }
            )
        except Exception as exc:
            return RepairAttempt(
                attempt_id=stable_repair_id("attempt", plan.plan_id, action.action_id, "failed"),
                plan_id=plan.plan_id,
                action_id=action.action_id,
                issue_id=action.issue_id,
                run_id=plan.run_id,
                status="failed",
                started_at=started,
                finished_at=utc_now_iso(),
                error_signature=f"repair.executor:{type(exc).__name__}",
                message=sanitize_repair_text(exc, limit=300),
            )

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


def _result_status(attempts: list[RepairAttempt]) -> str:
    if not attempts:
        return "not_executed"
    statuses = {attempt.status for attempt in attempts}
    if statuses == {"success"}:
        return "success"
    if statuses == {"skipped"}:
        return "skipped"
    if statuses == {"failed"}:
        return "failed"
    return "partial"


def _attempt_summary(attempts: list[RepairAttempt]) -> dict[str, Any]:
    attempt_count = len(attempts)
    success_count = sum(1 for attempt in attempts if attempt.status == "success")
    failed_count = sum(1 for attempt in attempts if attempt.status == "failed")
    skipped_count = sum(1 for attempt in attempts if attempt.status == "skipped")
    not_executed_count = sum(1 for attempt in attempts if attempt.status == "not_executed")
    return {
        "attempt_count": attempt_count,
        "success_count": success_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "not_executed_count": not_executed_count,
        "repair_success_rate": success_count / attempt_count if attempt_count else None,
    }
