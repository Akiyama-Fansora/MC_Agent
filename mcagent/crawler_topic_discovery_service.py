from __future__ import annotations

from typing import Any


def _topic_discovery_returncode(value: Any) -> int:
    """Normalize tool return codes without letting malformed metadata pass as success."""
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return 1
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else 1
    if isinstance(value, str):
        text = value.strip()
        if text and (text.isdigit() or (text.startswith("-") and text[1:].isdigit())):
            return int(text)
    return 1


class CrawlerTopicDiscoveryReviewService:
    """Manage objective bookkeeping around topic discovery review."""

    def should_review(self, *, task_source: str, result: dict[str, Any]) -> bool:
        if task_source != "topic_discovery" or not isinstance(result, dict):
            return False
        return _topic_discovery_returncode(result.get("returncode")) == 0

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
        expansions = plan.get("discovery_expansions")
        if not isinstance(expansions, list):
            expansions = []
            plan["discovery_expansions"] = expansions
        expansions.append(entry)
        return entry
