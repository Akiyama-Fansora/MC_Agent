from __future__ import annotations

from typing import Any


class CrawlerPlannerWaitService:
    """Build objective status payloads while the crawler planner LLM is running."""

    TOPIC_KEYS = ("authoritative_task_goal", "task_goal", "collection_target", "goal", "target", "current_topic")

    def context(self, *, question: str, session_summary: dict[str, Any] | None) -> dict[str, str]:
        planner_topic = question
        handoff_brief = ""
        delivery_target = ""
        if isinstance(session_summary, dict):
            handoff_brief = str(session_summary.get("handoff_brief") or "").strip()
            delivery_target = str(session_summary.get("delivery_target") or "").strip()
            for key in self.TOPIC_KEYS:
                planner_topic = str(session_summary.get(key) or "").strip()
                if planner_topic:
                    break
            planner_topic = planner_topic or question
        return {
            "planner_topic": planner_topic,
            "handoff_brief": handoff_brief,
            "delivery_target": delivery_target,
        }

    def stopped_plan(self, *, planner_topic: str, handoff_brief: str) -> dict[str, Any]:
        return {
            "strategy": "stopped_before_planner_finished",
            "topic": planner_topic,
            "handoff_brief": handoff_brief,
            "tasks": [],
            "stopped": True,
        }

    def waiting_update(self, *, elapsed_seconds: int, planner_topic: str, handoff_brief: str, delivery_target: str) -> dict[str, Any]:
        return {
            "summary": f"CrawlerAgent 正在理解任务并规划采集动作，已思考 {elapsed_seconds} 秒。目标：{planner_topic[:80]}",
            "result": {
                "source": "planner",
                "plan": {
                    "topic": planner_topic,
                    "handoff_brief": handoff_brief,
                    "delivery_target": delivery_target,
                },
                "planned_tasks": [],
                "tasks": [],
                "loop": [
                    {"phase": "understand", "status": "running", "note": "CrawlerAgent is reading the request, memory, and available tools."},
                    {"phase": "plan", "status": "pending"},
                    {"phase": "act", "status": "pending"},
                    {"phase": "verify", "status": "pending"},
                ],
            },
        }
