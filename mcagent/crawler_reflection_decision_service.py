from __future__ import annotations

from typing import Any


VALID_REFLECTION_ACTIONS = {"execute_pending", "add_tasks", "replan", "finish"}


class CrawlerReflectionDecisionService:
    """Normalize CrawlerAgent reflection output into an executor contract.

    This service does not choose the next action. It only validates the LLM's
    chosen action and records whether the executor needs another LLM pass to
    turn an incomplete decision into executable tool tasks.
    """

    def normalize(
        self,
        raw: dict[str, Any],
        *,
        pending_count: int,
        normalized_tasks: list[dict[str, Any]] | None = None,
        planner: str = "",
    ) -> dict[str, Any]:
        issues: list[str] = []
        raw_action = str(raw.get("action") or "").strip().lower()
        action = raw_action
        if action not in VALID_REFLECTION_ACTIONS:
            issues.append("invalid_or_missing_action")
            action = "execute_pending" if pending_count > 0 else "finish"

        selected_index = self._safe_int(raw.get("selected_index"), 0)
        if pending_count <= 0:
            if action == "execute_pending":
                issues.append("no_pending_task_to_execute")
            selected_index = 0
        elif action == "execute_pending" and (selected_index < 0 or selected_index >= pending_count):
            issues.append("selected_index_out_of_range")
            selected_index = 0
        elif action in {"add_tasks", "replan"}:
            selected_index = 0

        tasks = list(normalized_tasks or [])
        if action == "execute_pending" and tasks:
            issues.append("tasks_returned_for_execute_pending")
            tasks = []
        if action == "finish" and tasks:
            issues.append("tasks_returned_for_finish")
            tasks = []
        if action in {"add_tasks", "replan"} and not tasks:
            issues.append("missing_executable_tasks_for_replan")

        reason = str(raw.get("reason") or "").strip()
        if not reason:
            issues.append("missing_reason")
            reason = "CrawlerAgent selected the next action."

        done_summary = str(raw.get("done_summary") or "").strip()
        if action == "finish" and not done_summary:
            done_summary = reason

        return {
            "action": action,
            "selected_index": selected_index,
            "reason": reason[:500],
            "tasks": tasks,
            "done_summary": done_summary[:800],
            "planner": planner,
            "contract": {
                "valid": not issues,
                "issues": issues,
                "requires_llm_task_materialization": action in {"add_tasks", "replan"} and not tasks,
                "pending_count": max(0, pending_count),
            },
        }

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            return default
