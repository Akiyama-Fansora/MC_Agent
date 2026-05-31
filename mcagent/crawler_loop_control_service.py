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

    def should_finish_after_useful_low_yield(
        self,
        *,
        source: str,
        success_count: int,
        bad_streak: int,
        executed_count: int,
        min_executed: int = 6,
        bad_streak_threshold: int = 4,
    ) -> bool:
        return (
            source in PLANNER_SOURCES
            and success_count > 0
            and executed_count >= min_executed
            and bad_streak >= bad_streak_threshold
        )

    def should_finish_after_no_success_low_yield(
        self,
        *,
        source: str,
        success_count: int,
        bad_streak: int,
        executed_count: int,
        min_executed: int = 6,
        bad_streak_threshold: int = 3,
    ) -> bool:
        return (
            source in PLANNER_SOURCES
            and success_count <= 0
            and executed_count >= min_executed
            and bad_streak >= bad_streak_threshold
        )

    def should_finish_after_enough_success(
        self,
        *,
        source: str,
        success_count: int,
        executed_count: int,
        task_count: int,
        max_total_tasks: int,
        min_success: int = 3,
        min_executed: int = 8,
    ) -> bool:
        return (
            source in PLANNER_SOURCES
            and success_count >= min_success
            and executed_count >= min_executed
            and task_count >= max_total_tasks
        )
