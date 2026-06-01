from __future__ import annotations

from typing import Any

from .crawler_self_audit_service import CrawlerSelfAuditService
from .crawler_task_materialization_service import CrawlerTaskMaterializationService


class CrawlerJobFinalizationService:
    """Build the final job update payload for a crawler run."""

    def build(
        self,
        *,
        stop_requested: bool,
        success_count: int,
        candidate_count: int,
        failure_count: int,
        replan_count: int,
        needs_ingest: bool,
        task_results: list[dict[str, Any]],
        planned_tasks: list[dict[str, Any]],
        plan: dict[str, Any],
        collection_summary: dict[str, Any],
    ) -> dict[str, Any]:
        status = "stopped" if stop_requested else ("succeeded" if success_count else "failed")
        summary = f"资料补库完成：资料成功 {success_count}，候选发现 {candidate_count}，失败 {failure_count}。"
        agent_finish_reason = str(plan.get("agent_finish_reason") or "").strip()
        stopped_before_tools = bool(agent_finish_reason and not stop_requested and success_count <= 0 and not task_results)
        if stopped_before_tools:
            summary = f"CrawlerAgent stopped before tool execution: {agent_finish_reason}"
        if stop_requested:
            summary = f"Crawler 任务已停止：已完成 {len(task_results)}/{len(planned_tasks)} 个任务，资料成功 {success_count}，候选发现 {candidate_count}，失败 {failure_count}。"
        elif needs_ingest:
            summary += " 已启动后台入库。"
        display_planned_tasks, blocked_planned_tasks = CrawlerTaskMaterializationService().split_displayable_planned_tasks(planned_tasks)

        result = {
            "source": "planner",
            "success_count": success_count,
            "candidate_count": candidate_count,
            "failure_count": failure_count,
            "replan_count": replan_count,
            "ingest": None,
            "ingest_error": "",
            "ingest_background": bool(needs_ingest and not stop_requested),
            "tasks": task_results,
            "planned_tasks": display_planned_tasks,
            "blocked_planned_tasks": blocked_planned_tasks,
            "plan": plan,
            "collection_summary": collection_summary,
            "loop": [
                {"phase": "understand", "status": "done"},
                {"phase": "plan", "status": "done"},
                {
                    "phase": "act",
                    "status": "blocked" if stopped_before_tools else "done",
                    "note": agent_finish_reason if stopped_before_tools else f"Succeeded {success_count}; failed {failure_count}",
                },
                {
                    "phase": "ingest",
                    "status": "running" if needs_ingest and not stop_requested else "skipped",
                    "note": "Background ingest started." if needs_ingest and not stop_requested else "No new records require ingest.",
                },
                {
                    "phase": "verify",
                    "status": "done" if success_count else ("stopped" if stop_requested else "failed"),
                    "note": "Task results keep records/skipped/errors for the next retry.",
                },
            ],
        }
        result["self_audit"] = CrawlerSelfAuditService().build(task_results, result)
        return {
            "status": status,
            "summary": summary,
            "error": None if success_count or stop_requested else (agent_finish_reason or "all crawler sources failed"),
            "result": result,
        }
