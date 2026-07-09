from __future__ import annotations

from typing import Any


PLANNER_SOURCES = {"planner", "auto", "smart", "orchestrator"}


class CrawlerJobSetupService:
    """Prepare objective crawler job setup values."""

    def is_planner_source(self, source: str) -> bool:
        return source in PLANNER_SOURCES

    def payload_int(
        self,
        payload: dict[str, Any],
        key: str,
        *,
        default: int,
        min_value: int | None = None,
        max_value: int | None = None,
    ) -> int:
        try:
            value = payload.get(key)
            if value is None or str(value).strip() == "":
                parsed = default
            else:
                parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        if min_value is not None:
            parsed = max(min_value, parsed)
        if max_value is not None:
            parsed = min(max_value, parsed)
        return parsed

    def planner_max_tasks(self, payload: dict[str, Any], *, default: int = 16) -> int:
        return self.payload_int(payload, "max_tasks", default=default, min_value=1, max_value=32)

    def single_source_tasks(self, *, source: str, payload: dict[str, Any], question: str) -> list[dict[str, Any]]:
        return [{"source": source, "query": str(payload.get("query") or question), "reason": "single source request"}]

    def fallback_plan(self, *, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        return {"strategy": "fallback_all_source_tasks", "tasks": tasks}

    def limits(self, *, payload: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, int]:
        max_replans = self.payload_int(payload, "max_replans", default=2, min_value=0, max_value=10)
        explicit_max_tasks = "max_tasks" in payload and str(payload.get("max_tasks") or "").strip()
        initial_task_limit = self.payload_int(payload, "max_tasks", default=len(tasks) or 16, min_value=1, max_value=32)
        if explicit_max_tasks:
            max_total_tasks = max(len(tasks), min(32, initial_task_limit))
        else:
            max_total_tasks = max(len(tasks), min(18, initial_task_limit + 6))
        return {
            "max_replans": max_replans,
            "initial_task_limit": initial_task_limit,
            "max_total_tasks": max_total_tasks,
        }

    def stopped_update(self, *, stage: str, plan: dict[str, Any] | None = None, tasks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if stage == "before_plan":
            return {"status": "stopped", "summary": "Crawler 任务已在规划前停止。", "error": None}
        if stage == "planning":
            return {
                "status": "stopped",
                "summary": "Crawler 任务已在规划阶段停止。",
                "error": None,
                "result": {"source": "planner", "plan": plan or {}, "planned_tasks": [], "tasks": []},
            }
        return {
            "status": "stopped",
            "summary": "Crawler 任务已在规划后停止。",
            "error": None,
            "result": {"source": "planner", "plan": plan or {}, "planned_tasks": tasks or [], "tasks": []},
        }
