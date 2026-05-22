from __future__ import annotations

from typing import Any


class CrawlerRuntimeStepService:
    """Apply a CrawlerAgent reflection decision to the pending task queue.

    The service executes the already chosen action shape. It does not decide
    whether CrawlerAgent should replan, finish, or switch source.
    """

    def reflection_entry(self, *, index: int, reflection: dict[str, Any]) -> dict[str, Any]:
        return {
            "at_index": index,
            "action": reflection.get("action"),
            "selected_index": reflection.get("selected_index"),
            "reason": reflection.get("reason"),
            "planner": reflection.get("planner"),
            "tasks": reflection.get("tasks") or [],
            "contract": reflection.get("contract") or {},
        }

    def apply_action(
        self,
        *,
        tasks: list[dict[str, Any]],
        index: int,
        reflection: dict[str, Any],
        max_total_tasks: int,
        materialized_tasks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        action = str(reflection.get("action") or "execute_pending")
        new_tasks = [task for task in list(materialized_tasks if materialized_tasks is not None else reflection.get("tasks") or []) if isinstance(task, dict)]

        if action in {"add_tasks", "replan"} and new_tasks:
            inserted = self._insert_unique(tasks, index, new_tasks, max_total_tasks)
            if inserted:
                return {"continue_loop": True, "finished": False, "inserted_tasks": inserted, "selected_offset": 0}

        if action == "finish":
            return {
                "continue_loop": False,
                "finished": True,
                "finish_reason": str(reflection.get("done_summary") or reflection.get("reason") or ""),
                "inserted_tasks": [],
                "selected_offset": 0,
            }

        selected_offset = self._safe_int(reflection.get("selected_index"), 0)
        if selected_offset > 0 and index + selected_offset < len(tasks):
            tasks[index], tasks[index + selected_offset] = tasks[index + selected_offset], tasks[index]
            return {"continue_loop": False, "finished": False, "inserted_tasks": [], "selected_offset": selected_offset}
        return {"continue_loop": False, "finished": False, "inserted_tasks": [], "selected_offset": 0}

    def _insert_unique(self, tasks: list[dict[str, Any]], index: int, new_tasks: list[dict[str, Any]], max_total_tasks: int) -> list[dict[str, Any]]:
        seen_identities = {self.task_identity(task) for task in tasks}
        inserted: list[dict[str, Any]] = []
        for new_task in new_tasks:
            identity = self.task_identity(new_task)
            if identity in seen_identities or len(tasks) + len(inserted) >= max_total_tasks:
                continue
            inserted.append(new_task)
            seen_identities.add(identity)
        if inserted:
            tasks[index:index] = inserted
        return inserted

    @staticmethod
    def task_identity(task: dict[str, Any]) -> tuple[str, str]:
        source = str(task.get("source") or "").strip().lower()
        query = str(task.get("query") or task.get("start_url") or "").strip().lower()
        return source, query

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            return default
