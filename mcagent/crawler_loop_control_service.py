from __future__ import annotations

from typing import Any

from .agent_runtime import classify_crawler_tool_result


PLANNER_SOURCES = {"planner", "auto", "smart", "orchestrator"}


class CrawlerLoopControlService:
    """Compute objective loop-control signals for CrawlerAgent jobs."""

    def update_bad_streak(self, *, result: dict[str, Any], current_bad_streak: int) -> dict[str, Any]:
        observation = classify_crawler_tool_result(result)
        result.setdefault("observation", observation.to_dict())
        bad_streak = current_bad_streak + 1 if observation.bad else 0
        return {
            "bad": observation.bad,
            "bad_streak": bad_streak,
            "observation": observation.to_dict(),
        }

    def should_replan(
        self,
        *,
        source: str,
        success_count: int,
        bad_streak: int,
        replan_count: int,
        max_replans: int,
        task_count: int,
        max_total_tasks: int,
    ) -> bool:
        return (
            source in PLANNER_SOURCES
            and success_count == 0
            and bad_streak >= 3
            and replan_count < max_replans
            and task_count < max_total_tasks
        )
