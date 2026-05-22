from __future__ import annotations

from typing import Any


class CrawlerTopicDiscoveryReviewService:
    """Manage objective bookkeeping around topic discovery review."""

    def should_review(self, *, task_source: str, result: dict[str, Any]) -> bool:
        return task_source == "topic_discovery" and int(result.get("returncode") or 0) == 0

    def remaining_slots(self, *, max_total_tasks: int, current_task_count: int) -> int:
        return max(0, max_total_tasks - current_task_count)

    def record_review(
        self,
        *,
        plan: dict[str, Any],
        result: dict[str, Any],
        task_results_count: int,
        discovered_tasks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        entry = {
            "at_result_count": task_results_count,
            "source_query": result.get("query"),
            "reviewer": "Crawler LLM",
            "new_tasks": discovered_tasks,
        }
        if not discovered_tasks and result.get("topic_discovery_review_error"):
            entry["error"] = result.get("topic_discovery_review_error")
        plan.setdefault("discovery_expansions", []).append(entry)
        return entry
