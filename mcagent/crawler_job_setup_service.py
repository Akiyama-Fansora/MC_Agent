from __future__ import annotations

from typing import Any


PLANNER_SOURCES = {"planner", "auto", "smart", "orchestrator"}


class CrawlerJobSetupService:
    """Prepare objective crawler job setup values."""

    def is_planner_source(self, source: str) -> bool:
        return source in PLANNER_SOURCES

    def single_source_tasks(self, *, source: str, payload: dict[str, Any], question: str) -> list[dict[str, Any]]:
        return [{"source": source, "query": str(payload.get("query") or question), "reason": "single source request"}]

    def fallback_plan(self, *, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        return {"strategy": "fallback_all_source_tasks", "tasks": tasks}

    def limits(self, *, payload: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, int]:
        max_replans = int(payload.get("max_replans") or 2)
        initial_task_limit = int(payload.get("max_tasks") or len(tasks) or 16)
        max_total_tasks = max(len(tasks), min(32, initial_task_limit + 12))
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
